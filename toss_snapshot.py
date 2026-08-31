# -*- coding: utf-8 -*-
"""토스 시세 브리지 — Supabase 스냅샷 저장소(수집기·서버 공용 단일 출처).

■ 왜 있나 (2026-08-31 실측)
  토스 Open API 는 **호출 IP 화이트리스트**로 막힌다.
    · 노트북(122.35.177.133)  → 토큰발급 OK · 시세 OK
    · Azure VM(20.214.137.83) → 403 Forbidden
    · Render(싱가포르)        → 403 `IP address not allowed`
  그래서 «미리 서버는 토스를 직접 부를 수 없다». 랭킹의 주가 축(price_signal)과
  ④캘린더의 상장예정(IPO/ETF)이 둘 다 멈춰 있었다.
  해법: 허용된 IP 에서 도는 «수집기»가 결과를 Supabase 에 적고, 서버는 직접 호출이
  실패하면 그 스냅샷을 읽어 대체한다. 서버는 토스를 못 불러도 값을 갖는다.

■ 3단 구조
  수집기 tools/toss_bridge.py  →  이 모듈(Supabase 테이블)  →  서버 app.py / 캘린더 배치

■ 신선도(나이)를 반드시 같이 저장한다
  스냅샷은 «언제 찍혔는지»를 모르면 위험하다(어제 종가를 오늘 값으로 착각).
  collected_at(UTC) 를 행마다 저장하고, 읽는 쪽은 age_min 을 계산해 응답에 싣는다.
  임계는 이 파일 상단 상수 «한 곳»에서만 정한다(운영 중 조정은 환경변수).

■ 보안
  · 키/토큰 값은 로그·응답·예외에 절대 넣지 않는다(예외 타입명만).
  · 읽기/쓰기 모두 **service_role** 로만 한다. anon 은 쓰지 않는다 —
    RLS 정책 0개 테이블을 anon 으로 읽으면 PostgREST 가 «에러가 아니라 200 []» 를
    돌려준다. 그러면 「권한 없음」이 「스냅샷 없음」으로 조용히 둔갑한다(치명적 오독).
  · kis-trading 레포는 읽지도 쓰지도 않는다(U7-3 경계).
"""
import datetime as dt
import json
import os
import sys
import threading

try:
    import config
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

TABLE = "toss_bridge_snapshots"
KIND_PRICES = "prices"
KIND_SCHEDULED = "scheduled"

_HTTP_TIMEOUT = 10

# ---------------------------------------------------------------- 임계(상수 한 곳)
# ① 경보 임계 — 「직접 호출 실패」로는 울리지 않는다. 서버는 원래 못 부른다(설계상 정상).
#    스냅샷이 이 시간 이상 낡았을 때«만» = 수집기가 실제로 죽었을 때만 운다.
#    12h 근거: 수집기 정상 주기(노트북 30분)의 24배. 노트북이 밤에 꺼져 있어도
#    오경보 0, 하루를 넘겨 방치되면 반드시 잡힌다.
STALE_ALERT_HOURS = float(os.getenv("MIRI_TOSS_STALE_ALERT_H", "12"))
# ② 같은 경보 재발송 최소 간격(매시간 스팸 방지). 배치가 매 사이클 울지 않게.
STALE_ALERT_MIN_INTERVAL_H = float(os.getenv("MIRI_TOSS_STALE_ALERT_GAP_H", "12"))
# ③ 시세 스냅샷을 «쓸 수 있는» 최대 나이. 넘으면 서버는 값을 쓰지 않고 degrade.
#    72h 근거: change_pct 는 일봉 기준이라 주말(금 종가→월 아침 ≈ 63h)에도 유효하다.
#    72h 를 넘겼다는 건 수집기가 3일 죽었다는 뜻 = 값을 믿으면 안 되는 구간.
PRICE_MAX_AGE_MIN = float(os.getenv("MIRI_TOSS_PRICE_MAX_AGE_MIN", str(72 * 60)))
# ④ 상장예정 스냅샷을 쓸 수 있는 최대 나이(일 단위 데이터라 넉넉히).
SCHEDULED_MAX_AGE_MIN = float(os.getenv("MIRI_TOSS_SCHED_MAX_AGE_MIN", str(14 * 24 * 60)))

# 읽기 캐시(프로세스 내). 요청경로가 매번 Supabase 를 때리지 않게.
_READ_TTL_SEC = float(os.getenv("MIRI_TOSS_SNAPSHOT_TTL_SEC", "120"))
_MEM: dict = {}                      # kind -> {"ts": monotonic, "row": dict|None, "ok": bool}
_LOCK = threading.Lock()

DDL = f"""
create table if not exists public.{TABLE} (
  kind         text primary key,
  collected_at timestamptz not null default now(),
  source_host  text,
  item_count   integer not null default 0,
  payload      jsonb not null default '{{}}'::jsonb,
  meta         jsonb not null default '{{}}'::jsonb
);
comment on table public.{TABLE} is
  'MIRI 토스 시세 브리지: 허용 IP 수집기가 쓰고 서버가 읽는 스냅샷(kind별 1행). collected_at=수집시각 UTC';
alter table public.{TABLE} enable row level security;
-- 정책 0개 = anon/authenticated 전면 차단. service_role 만 RLS 를 우회한다.
""".strip()


def _log(msg):
    print(f"[toss-snapshot] {msg}")


# ---------------------------------------------------------------- 접속
def _rest_conf():
    """(base, headers). service_role 이 없으면 (None, None) — anon 폴백 금지(위 주석 참조)."""
    url = (getattr(config, "SUPABASE_URL", "") or "").strip()
    key = (getattr(config, "SUPABASE_SERVICE_ROLE", "") or "").strip()
    if not (url and key and requests is not None):
        return None, None
    return url.rstrip("/") + "/rest/v1", {
        "apikey": key, "Authorization": "Bearer " + key,
        "Content-Type": "application/json"}


def enabled():
    base, _ = _rest_conf()
    return bool(base)


def _is_table_missing(resp) -> bool:
    if resp is None or resp.status_code != 404:
        return False
    try:
        return (resp.json() or {}).get("code") == "PGRST205"
    except ValueError:
        return False


def ensure_table() -> bool:
    """Management API 로 CREATE TABLE IF NOT EXISTS(멱등). 실패해도 False 만."""
    try:
        import miri_cache
        return bool(miri_cache.ensure_table(DDL))
    except Exception as e:  # noqa: BLE001
        _log(f"ensure_table 실패(무시): {type(e).__name__}")
        return False


# ---------------------------------------------------------------- 나이 계산
def parse_ts(s):
    """timestamptz 문자열 → aware datetime(UTC). 실패 시 None."""
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    # PostgREST 는 마이크로초 6자리 초과를 주지 않지만 방어적으로 자른다.
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        try:
            d = dt.datetime.fromisoformat(t.split(".")[0])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def age_min(row) -> float:
    """행의 나이(분). 파싱 불가/행 없음이면 매우 큰 값(=낡음)으로 본다.
    ★부재를 «0분(신선)» 으로 오독하지 않게 큰 값을 돌린다 — fail-safe 는 «낡음»이다."""
    if not isinstance(row, dict):
        return float("inf")
    d = parse_ts(row.get("collected_at"))
    if d is None:
        return float("inf")
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - d).total_seconds() / 60.0)


# ---------------------------------------------------------------- 쓰기
def put(kind: str, payload, meta=None, source_host=None, collected_at=None):
    """kind 행 1개 upsert. (ok, reason). 예외를 밖으로 내지 않는다."""
    base, headers = _rest_conf()
    if not base:
        return False, "disabled(no SUPABASE_URL/SERVICE_ROLE)"
    now = collected_at or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    try:
        n = len(payload) if hasattr(payload, "__len__") else 0
    except Exception:  # noqa: BLE001
        n = 0
    row = {
        "kind": kind,
        "collected_at": now.isoformat(timespec="seconds"),
        "source_host": (source_host or os.getenv("MIRI_BRIDGE_HOST") or "unknown")[:64],
        "item_count": n,
        "payload": payload if payload is not None else {},
        "meta": meta or {},
    }
    h = dict(headers)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    url = base + "/" + TABLE + "?on_conflict=kind"
    body = json.dumps([row], ensure_ascii=False, allow_nan=False)
    try:
        r = requests.post(url, headers=h, data=body.encode("utf-8"), timeout=_HTTP_TIMEOUT + 15)
        if _is_table_missing(r):
            if not ensure_table():
                _log(f"경고: 테이블 {TABLE} 없음 · Management 토큰 없거나 생성 실패")
                return False, "table_missing"
            r = requests.post(url, headers=h, data=body.encode("utf-8"),
                              timeout=_HTTP_TIMEOUT + 15)
        r.raise_for_status()
        with _LOCK:                       # 방금 쓴 값을 다음 read 가 바로 보게 캐시 무효화
            _MEM.pop(kind, None)
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        _log(f"put({kind}) 실패: {type(e).__name__}")
        return False, type(e).__name__


# ---------------------------------------------------------------- 읽기
def get(kind: str, use_cache=True):
    """kind 행 조회. (ok, row|None). ok=False 는 «조회 실패»(부재와 구분)."""
    import time as _t
    if use_cache:
        with _LOCK:
            ent = _MEM.get(kind)
            if ent and (_t.monotonic() - ent["ts"]) < _READ_TTL_SEC:
                return ent["ok"], ent["row"]
    base, headers = _rest_conf()
    if not base:
        return False, None
    try:
        from urllib.parse import quote as _q
        url = (base + "/" + TABLE + "?select=*&kind=eq." + _q(str(kind), safe="") + "&limit=1")
        r = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        if _is_table_missing(r):
            return False, None
        r.raise_for_status()
        data = r.json()
        row = data[0] if isinstance(data, list) and data else None
        with _LOCK:
            _MEM[kind] = {"ts": _t.monotonic(), "row": row, "ok": True}
        return True, row
    except Exception as e:  # noqa: BLE001
        _log(f"get({kind}) 실패: {type(e).__name__}")
        with _LOCK:                        # 실패도 짧게 기억(요청폭주 시 반복호출 방지)
            _MEM[kind] = {"ts": _t.monotonic(), "row": None, "ok": False}
        return False, None


def _meta(row, reason):
    """읽는 쪽이 응답/로그에 실을 최소 메타(비밀 없음)."""
    return {"reason": reason,
            "age_min": (None if row is None else round(age_min(row), 1)),
            "collected_at": (row or {}).get("collected_at"),
            "source_host": (row or {}).get("source_host"),
            "item_count": (row or {}).get("item_count")}


def _read(kind, limit_min):
    ok, row = get(kind)
    if not ok:
        return None, _meta(None, "unavailable")
    if not row:
        return None, _meta(None, "missing")
    a = age_min(row)
    if a > limit_min:
        return None, _meta(row, "stale")
    return row, _meta(row, "ok")


def prices(max_age_min=None):
    """시세 스냅샷. (by_code: dict|None, meta: dict).

    by_code = {code: {price, prev_close, change_pct, volume}}.
    너무 낡거나 없으면 by_code=None (값을 쓰지 않는다 = degrade). 사유는 meta.reason."""
    row, meta = _read(KIND_PRICES, PRICE_MAX_AGE_MIN if max_age_min is None else float(max_age_min))
    if row is None:
        return None, meta
    pl = row.get("payload")
    if not isinstance(pl, dict) or not pl:
        meta["reason"] = "empty"
        return None, meta
    return pl, meta


def scheduled(max_age_min=None):
    """상장예정 스냅샷. (items: list|None, meta: dict)."""
    row, meta = _read(KIND_SCHEDULED,
                      SCHEDULED_MAX_AGE_MIN if max_age_min is None else float(max_age_min))
    if row is None:
        return None, meta
    pl = row.get("payload")
    if isinstance(pl, dict):
        pl = pl.get("items")
    if not isinstance(pl, list):
        meta["reason"] = "empty"
        return None, meta
    return pl, meta


def is_stale(kind: str, hours=None) -> tuple:
    """경보 판정 전용. (stale: bool, age_min: float|None, reason: str).
    ok=False(조회 실패)도 stale 로 본다 — 「모르겠다」를 「괜찮다」로 읽지 않는다."""
    limit_h = STALE_ALERT_HOURS if hours is None else float(hours)
    ok, row = get(kind, use_cache=False)
    if not ok:
        return True, None, "db_unavailable"
    if not row:
        return True, None, "missing"
    a = age_min(row)
    return (a > limit_h * 60.0), a, ("stale" if a > limit_h * 60.0 else "fresh")


# ---------------------------------------------------------------- CLI(상태 확인)
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"table={TABLE} enabled={enabled()} "
          f"stale_alert_h={STALE_ALERT_HOURS} price_max_age_min={PRICE_MAX_AGE_MIN}")
    for k in (KIND_PRICES, KIND_SCHEDULED):
        ok, row = get(k, use_cache=False)
        if not ok:
            print(f"  {k}: 조회 실패(권한/네트워크/테이블 없음)")
        elif not row:
            print(f"  {k}: 없음")
        else:
            print(f"  {k}: items={row.get('item_count')} age={age_min(row):.1f}min "
                  f"host={row.get('source_host')} collected_at={row.get('collected_at')}")

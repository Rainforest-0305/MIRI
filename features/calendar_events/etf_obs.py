# -*- coding: utf-8 -*-
"""토스 SCHEDULED 관측 로그의 Supabase 영속화 (calendar_etf_observations).

왜: Render 디스크는 재기동·재배포마다 지워져 파일 로그(toss_scheduled_log)가 하루도
못 버틴다. 2주 뒤 「토스가 ETF 를 SCHEDULED 로 잡는가」를 데이터로 판정하려면
사이클마다 관측 결과를 DB 에 남겨야 한다(President 결정 2026-08-29 「그걸로 해」).

테이블(obs_date PK → 하루 여러 사이클 돌아도 1행, upsert):
  obs_date date PK · scheduled_count int · scheduled_symbols jsonb(symbol·name·market·listDate·securityType)
  · market_counts jsonb · etf_count int · stock_count int · fetched_at timestamptz

정책:
  · fail-open — 테이블 없음/네트워크 오류/키 없음 어느 경우에도 예외를 밖으로 내지
    않는다. (ok, reason) 만 돌려주고 배치는 파일 로그를 그대로 유지한다.
  · 테이블 없음(PGRST205) 이면 Management API(SUPABASE_ACCESS_TOKEN)로 1회
    CREATE TABLE IF NOT EXISTS 시도 후 재-upsert. 토큰이 없으면 경고 1줄.
  · 키·토큰은 로그/예외에 절대 노출하지 않는다(예외 타입명만).
  · 아웃바운드: 사이클당 upsert 1회(본문 ≈ 1~5KB). 12h 주기 → 월 ≈ 60회 ≈ 0.3MB.

조회: python -m features.calendar_events.etf_obs --days 14
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

try:
    import config
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)))
    import config

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

TABLE = "calendar_etf_observations"
_HTTP_TIMEOUT = 10
MAX_SYMBOLS = 200          # jsonb 행 상한(토스 SCHEDULED 는 통상 0~수십 건)

DDL = f"""
create table if not exists public.{TABLE} (
  obs_date          date primary key,
  scheduled_count   integer not null default 0,
  etf_count         integer not null default 0,
  stock_count       integer not null default 0,
  scheduled_symbols jsonb not null default '[]'::jsonb,
  market_counts     jsonb not null default '{{}}'::jsonb,
  fetched_at        timestamptz not null default now()
);
comment on table public.{TABLE} is
  'MIRI 캘린더 배치: 토스 Open API SCHEDULED 관측 로그(하루 1행, 사이클마다 upsert). ETF 잡히는지 판정용';
alter table public.{TABLE} enable row level security;
-- 정책 0개 = anon/authenticated 전면 차단. service_role 은 RLS 를 우회하므로 배치(서비스 키)만 접근.
""".strip()


def _log(msg):
    print(f"[cal-events][etf-obs] {msg}")


# ---------------------------------------------------------------- 접속 설정
def _rest_conf():
    url = (getattr(config, "SUPABASE_URL", "") or "").strip()
    key = (getattr(config, "SUPABASE_SERVICE_ROLE", "") or "").strip()
    if not (url and key and requests is not None):
        return None, None
    return url.rstrip("/") + "/rest/v1", {"apikey": key, "Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"}


def _mgmt_conf():
    url = (getattr(config, "SUPABASE_URL", "") or "").strip()
    tok = (getattr(config, "SUPABASE_ACCESS_TOKEN", "") or "").strip()
    if not (url and tok and requests is not None):
        return None, None
    ref = url.split("//")[-1].split(".")[0]
    if not ref:
        return None, None
    return ("https://api.supabase.com/v1/projects/" + ref + "/database/query",
            {"Authorization": "Bearer " + tok, "Content-Type": "application/json"})


def enabled():
    base, _ = _rest_conf()
    return bool(base)


# ---------------------------------------------------------------- 행 생성
def build_row(snap, today: dt.date, fetched_at: dt.datetime = None) -> dict:
    """토스 스냅샷(list[dict]) → 테이블 1행. 순수 함수(외부 호출 0)."""
    snap = list(snap or [])
    fetched_at = fetched_at or dt.datetime.now(dt.timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)
    mkt = {}
    for s in snap:
        k = s.get("market") or "UNKNOWN"
        mkt[k] = mkt.get(k, 0) + 1
    syms = [{"symbol": s.get("symbol"), "name": s.get("name"), "market": s.get("market"),
             "listDate": s.get("listDate"), "securityType": s.get("securityType")}
            for s in snap[:MAX_SYMBOLS]]
    return {
        "obs_date": today.isoformat(),
        "scheduled_count": len(snap),
        "etf_count": sum(1 for s in snap if s.get("securityType") == "ETF"),
        "stock_count": sum(1 for s in snap if s.get("securityType") == "STOCK"),
        "scheduled_symbols": syms,
        "market_counts": mkt,
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- DB 접근
def _is_table_missing(resp) -> bool:
    if resp is None or resp.status_code != 404:
        return False
    try:
        return (resp.json() or {}).get("code") == "PGRST205"
    except ValueError:
        return False


def ensure_table() -> bool:
    """Management API 로 CREATE TABLE IF NOT EXISTS. 토큰 없거나 실패면 False(예외 없음)."""
    q, headers = _mgmt_conf()
    if not q:
        return False
    try:
        r = requests.post(q, headers=headers, data=json.dumps({"query": DDL}), timeout=_HTTP_TIMEOUT + 15)
        r.raise_for_status()
        _log("테이블 생성/확인 완료(Management API)")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"테이블 생성 실패(무시): {type(e).__name__}")
        return False


def _post_upsert(base, headers, row):
    h = dict(headers)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    return requests.post(base + "/" + TABLE + "?on_conflict=obs_date", headers=h,
                         data=json.dumps([row], ensure_ascii=False, allow_nan=False), timeout=_HTTP_TIMEOUT)


def upsert_observation(row: dict):
    """1행 upsert(by obs_date). 반환 (ok: bool, reason: str). 예외를 밖으로 내지 않는다."""
    base, headers = _rest_conf()
    if not base:
        return False, "disabled(no SUPABASE_URL/SERVICE_ROLE)"
    try:
        r = _post_upsert(base, headers, row)
        if _is_table_missing(r):
            if ensure_table():
                r = _post_upsert(base, headers, row)
            else:
                _log(f"경고: 테이블 {TABLE} 없음 — Management 토큰 없거나 생성 실패. 파일 로그만 유지")
                return False, "table_missing"
        r.raise_for_status()
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}"
        _log(f"upsert 실패(무시, 파일 로그 유지): {reason}")
        return False, reason


def record(snap, today: dt.date):
    """배치 훅: 스냅샷 → 행 → upsert. 항상 (ok, reason). 절대 raise 하지 않는다."""
    try:
        return upsert_observation(build_row(snap, today))
    except Exception as e:  # noqa: BLE001  (build_row 방어)
        _log(f"record 실패(무시): {type(e).__name__}")
        return False, type(e).__name__


def fetch_observations(days: int = 14):
    """최근 days 일 행(obs_date 내림차순). (ok, rows). 실패 시 (False, [])."""
    base, headers = _rest_conf()
    if not base:
        return False, []
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    q = base + "/" + TABLE + "?select=*&obs_date=gte." + since + "&order=obs_date.desc&limit=" + str(days + 1)
    try:
        r = requests.get(q, headers=headers, timeout=_HTTP_TIMEOUT)
        if _is_table_missing(r):
            # DDL 직후 PostgREST 스키마 캐시 반영 지연(실측 수 초) → 1회 재시도
            time.sleep(3)
            r = requests.get(q, headers=headers, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return True, r.json()
    except Exception as e:  # noqa: BLE001
        _log(f"조회 실패: {type(e).__name__}")
        return False, []


# ---------------------------------------------------------------- CLI
def format_table(rows) -> str:
    head = f"{'obs_date':<11}{'total':>6}{'etf':>5}{'stock':>7}  {'markets':<28}{'fetched_at(UTC)':<21}etf_symbols"
    lines = [head, "-" * len(head)]
    for r in rows:
        mk = r.get("market_counts") or {}
        mk_s = ",".join(f"{k}={v}" for k, v in sorted(mk.items())) or "-"
        etfs = ",".join(s.get("symbol") or "?" for s in (r.get("scheduled_symbols") or []) if s.get("securityType") == "ETF") or "-"
        lines.append(f"{r.get('obs_date',''):<11}{r.get('scheduled_count',0):>6}{r.get('etf_count',0):>5}"
                     f"{r.get('stock_count',0):>7}  {mk_s:<28}{str(r.get('fetched_at',''))[:19]:<21}{etfs}")
    n_etf_days = sum(1 for r in rows if (r.get("etf_count") or 0) > 0)
    lines.append(f"rows={len(rows)}  days_with_etf={n_etf_days}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="토스 SCHEDULED 관측 로그(Supabase) 조회")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ensure-table", action="store_true", help="Management API 로 테이블 생성(IF NOT EXISTS)")
    a = ap.parse_args(argv)
    if a.ensure_table:
        print("ensure_table:", ensure_table())
    ok, rows = fetch_observations(a.days)
    if not ok:
        print("조회 실패 — Supabase 설정/테이블/네트워크 확인")
        return 1
    print(json.dumps(rows, ensure_ascii=False, indent=1) if a.json else format_table(rows))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

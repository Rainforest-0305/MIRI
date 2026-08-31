# -*- coding: utf-8 -*-
"""토스 시세 브리지 «수집기» — 허용 IP 에서 돌며 Supabase 에 스냅샷을 적는다.

■ 3단 중 1단
     [이 파일: 수집기]  →  Supabase toss_bridge_snapshots  →  미리 서버(app.py/캘린더)
   토스 Open API 는 호출 IP 화이트리스트다. 2026-08-31 실측:
     노트북(122.35.177.133) OK · Azure VM(20.214.137.83) OK(President 가 추가)
     Render(싱가포르) 403 `IP address not allowed`  ← 서버는 영원히 직접 못 부른다
   그래서 허용 IP 쪽에서 «미리 자기 키»로 찍어 DB 에 남기고, 서버는 그걸 읽는다.

■ ★이 파일은 «의도적으로» 저장소 의존이 0이다 (requests 만 필요)
   정상 실행 위치가 **VM 의 별도 폴더**(C:\\Users\\urimk\\miri-bridge\\)이기 때문이다.
   거기엔 gongsi-alert 저장소가 없다. 파일 1개 + .env 1개만 복사하면 돈다.
   → price_adapter.py 와 토큰/GET 로직이 겹치지만 «배포 산출물»이라 감수한다.
     겹치는 것은 read-only GET 2종뿐이고, 계약(엔드포인트·응답 필드)이 바뀌면
     양쪽 다 깨져 드러난다(조용한 불일치가 아니다).
   ⛔ kis-trading 레포는 읽지도 쓰지도 않는다. 키는 미리 자기 .env 만 쓴다.

■ 실행
   python tools/toss_bridge.py --kinds prices,scheduled       # 둘 다 수집
   python tools/toss_bridge.py --kinds prices                 # 장중 주기 실행용
   python tools/toss_bridge.py --show                         # 현재 스냅샷 상태만
   python tools/toss_bridge.py --kinds prices --dry           # 수집만·DB 미기록

■ 필요한 설정(.env 또는 프로세스 환경변수)
   TOSS_APP_KEY · TOSS_SECRET            (미리 전용 키)
   SUPABASE_URL · SUPABASE_SERVICE_ROLE  (service_role 필수. anon 은 RLS 로 못 쓴다)
   SUPABASE_ACCESS_TOKEN                 (선택. 최초 1회 테이블 자동생성용)
   MIRI_API_BASE                         (선택. 기본 https://rimi-s76t.onrender.com)
   MIRI_BRIDGE_HOST                      (선택. 'vm'/'laptop' 라벨. 진단용)
   .env 탐색 순서: 이 파일 폴더 → 그 상위 폴더 → 프로세스 환경변수(환경변수 우선)

■ 보안
   키·토큰 값은 로그/출력/예외에 절대 넣지 않는다(상태코드·예외 타입명만).
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))

TOSS_BASE = "https://openapi.tossinvest.com"
TABLE = "toss_bridge_snapshots"          # ★toss_snapshot.py 와 동일해야 한다(계약)
KIND_PRICES = "prices"
KIND_SCHEDULED = "scheduled"
MARKETS = ("KOSPI", "KOSDAQ", "KR_ETC")

DEFAULT_API_BASE = "https://rimi-s76t.onrender.com"
DEFAULT_MAX_CODES = 50                    # 시세 수집 상한(랭킹 풀 크기와 맞춘다)
_CANDLE_GAP_S = 0.22                      # candles 한도 5 req/s → 여유 두고 간격
_TOKEN_FILE = os.path.join(_HERE, ".toss_token.bridge.json")
_HTTP_TIMEOUT = 15

CALLS = {"token_post": 0, "candles": 0, "stocks_all": 0, "stocks": 0}


def _log(msg):
    print(f"[toss-bridge] {msg}", flush=True)


# ---------------------------------------------------------------- 설정 로드
def _load_env_files():
    """.env 를 «환경변수가 없는 키에 한해» 채운다(환경변수 우선). 값은 출력하지 않는다."""
    seen = []
    for d in (_HERE, os.path.dirname(_HERE)):
        p = os.path.join(d, ".env")
        if not os.path.exists(p):
            continue
        seen.append(p)
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and not os.environ.get(k):
                        os.environ[k] = v
        except OSError as e:
            _log(f".env 읽기 실패(무시) {os.path.basename(d)}: {type(e).__name__}")
    return seen


def _cfg(name, default=""):
    return (os.environ.get(name) or default).strip()


# ---------------------------------------------------------------- 토스 read-only
def _toss_token(force=False):
    """유효 토큰 반환(로컬 캐시 → 발급). 토큰 값은 절대 출력하지 않는다."""
    if not force:
        try:
            if os.path.exists(_TOKEN_FILE):
                d = json.loads(open(_TOKEN_FILE, encoding="utf-8").read())
                if d.get("access_token") and d.get("exp", 0) - 120 > time.time():
                    return d["access_token"]
        except (OSError, ValueError):
            pass
    ak, sk = _cfg("TOSS_APP_KEY"), _cfg("TOSS_SECRET")
    if not ak or not sk:
        raise RuntimeError("TOSS_APP_KEY/TOSS_SECRET 없음(.env 또는 환경변수)")
    CALLS["token_post"] += 1
    r = requests.post(TOSS_BASE + "/oauth2/token",
                      data={"grant_type": "client_credentials",
                            "client_id": ak, "client_secret": sk}, timeout=_HTTP_TIMEOUT)
    if not r.ok:
        # 403 `IP address not allowed` 가 여기서 난다 = 이 호스트 IP 가 미허용.
        _log(f"토큰 발급 {r.status_code}: {r.text[:160]}")
    r.raise_for_status()
    j = r.json()
    j["exp"] = time.time() + int(j.get("expires_in", 3600))
    try:
        tmp = _TOKEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(j, f)
        os.replace(tmp, _TOKEN_FILE)
    except OSError as e:
        _log(f"토큰 캐시 저장 실패(무시): {type(e).__name__}")
    return j["access_token"]


def _get(path, params=None, tries=3):
    """read-only GET. 401/403 → 토큰 재발급 1회, 429 → 백오프. 주문/변경 엔드포인트 없음."""
    last = None
    for i in range(tries):
        h = {"Authorization": "Bearer " + _toss_token(force=(i == 1))}
        r = requests.get(TOSS_BASE + path, params=params or {}, headers=h, timeout=_HTTP_TIMEOUT)
        if r.status_code in (401, 403) and i == 0:
            _log(f"GET {path} -> {r.status_code} · 토큰 재발급 후 재시도")
            continue
        if r.status_code == 429:
            time.sleep(1.2 * (i + 1) * 2)
            last = requests.HTTPError(f"429 {path}")
            continue
        if not r.ok:
            _log(f"GET {path} -> {r.status_code}: {r.text[:160]}")
        r.raise_for_status()
        return r.json()
    raise last or RuntimeError(f"GET {path} 실패")


def _rows(j):
    r = (j or {}).get("result", [])
    if isinstance(r, dict):
        r = r.get("stocks") or r.get("items") or r.get("list") or []
    return r or []


# ---------------------------------------------------------------- 수집: 시세
def fetch_wanted_codes(max_codes=DEFAULT_MAX_CODES):
    """시세를 채워야 할 종목코드. 순서: 라이브 랭킹 API → 직전 스냅샷 → 빈 리스트.

    ★랭킹 풀은 공시 흐름에 따라 매일 바뀐다. 코드를 고정 목록으로 박으면
      「스냅샷은 신선한데 정작 필요한 종목이 없다」가 된다 — 서버가 실제로 순위를
      매기는 그 종목들을 그대로 물어본다(단일 출처).
    라이브 API 는 급등락이 비어 있어도 공시중요도만으로 items 를 돌려주므로
    폴백 상황에서도 코드 목록 확보에 문제가 없다(실측)."""
    base = _cfg("MIRI_API_BASE", DEFAULT_API_BASE).rstrip("/")
    try:
        r = requests.get(f"{base}/api/ranking", params={"top_n": max_codes}, timeout=30)
        r.raise_for_status()
        items = (r.json() or {}).get("items") or []
        codes = []
        for it in items:
            c = str(it.get("stock_code") or "").strip()
            if c and c not in codes:
                codes.append(c)
        if codes:
            _log(f"대상 종목 {len(codes)}개 (출처: 라이브 랭킹 API)")
            return codes[:max_codes], "ranking_api"
    except Exception as e:  # noqa: BLE001
        _log(f"랭킹 API 조회 실패 → 직전 스냅샷 재사용 시도: {type(e).__name__}")
    ok, row = sb_get(KIND_PRICES)
    if ok and row and isinstance(row.get("payload"), dict):
        codes = list(row["payload"].keys())[:max_codes]
        if codes:
            _log(f"대상 종목 {len(codes)}개 (출처: 직전 스냅샷)")
            return codes, "previous_snapshot"
    _log("대상 종목 0개 — 랭킹 API·직전 스냅샷 모두 실패")
    return [], "none"


def candles(symbol, interval="1d"):
    CALLS["candles"] += 1
    res = _get("/api/v1/candles", {"symbol": symbol, "interval": interval})
    out = []
    for c in res.get("result", {}).get("candles", []):
        try:
            out.append({"t": c["timestamp"], "close": float(c["closePrice"]),
                        "volume": float(c["volume"])})
        except (KeyError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: x["t"])
    return out


def collect_prices(codes):
    """codes → ({code: {price, prev_close, change_pct, volume}}, stats).

    change_pct = (최근 종가/직전 종가 - 1)*100 (일봉 2개 필요).
    ★첫 종목부터 실패하면 즉시 중단한다 — 인증/IP 전역장애를 50번 반복해봐야
      «부분 성공처럼 보이는 0건»만 만든다(조용한 실패 금지)."""
    results, errors = {}, []
    degraded = False
    for i, code in enumerate(codes):
        if i:
            time.sleep(_CANDLE_GAP_S)
        try:
            rows = candles(code)
            if len(rows) < 2:
                errors.append(f"{code}: 일봉 부족({len(rows)})")
                continue
            last, prev = rows[-1], rows[-2]
            pc = prev["close"]
            results[code] = {
                "price": round(last["close"], 2),
                "prev_close": round(pc, 2),
                "change_pct": round((last["close"] / pc - 1.0) * 100.0, 2) if pc else None,
                "volume": last["volume"],
            }
        except Exception as e:  # noqa: BLE001
            code_s = getattr(getattr(e, "response", None), "status_code", "")
            errors.append(f"{code}: {type(e).__name__} {code_s}".strip())
            if i == 0:
                degraded = True
                _log(f"첫 호출 실패 → 전역장애로 보고 중단: {errors[-1]}")
                break
    return results, {"requested": len(codes), "resolved": len(results),
                     "errors": errors[:10], "degraded": degraded,
                     "candle_calls": CALLS["candles"], "token_post": CALLS["token_post"]}


# ---------------------------------------------------------------- 수집: 상장예정
def collect_scheduled():
    """3개 시장 SCHEDULED + 상세. features/calendar_events/toss_src.py 와 동일 계약.
    (list[dict(symbol,name,securityType,market,status,listDate)], stats)
    ★어떤 시장이든 실패하면 예외 — 부분 스냅샷을 «0건»으로 오독하면 캘린더가 비어버린다."""
    base = []
    for i, mkt in enumerate(MARKETS):
        if i:
            time.sleep(1.2)               # stocks/all 한도 1 req/s(실측 429)
        CALLS["stocks_all"] += 1
        base += _rows(_get("/api/v1/stocks/all", {"market": mkt, "status": "SCHEDULED"}))
    syms = [x.get("symbol") for x in base if x.get("symbol")]
    detail = {}
    for i in range(0, len(syms), 50):
        if i:
            time.sleep(0.25)
        CALLS["stocks"] += 1
        for d in _rows(_get("/api/v1/stocks", {"symbols": ",".join(syms[i:i + 50])})):
            detail[d.get("symbol")] = d
    out = []
    for x in base:
        d = detail.get(x.get("symbol"), {})
        out.append({"symbol": x.get("symbol"), "name": d.get("name") or x.get("name"),
                    "securityType": x.get("securityType") or d.get("securityType"),
                    "market": d.get("market"), "status": d.get("status"),
                    "listDate": d.get("listDate")})
    return out, {"total": len(out),
                 "etf": sum(1 for s in out if s.get("securityType") == "ETF"),
                 "stock": sum(1 for s in out if s.get("securityType") == "STOCK"),
                 "calls": {"stocks_all": CALLS["stocks_all"], "stocks": CALLS["stocks"]}}


# ---------------------------------------------------------------- Supabase
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
""".strip()


def _rest():
    url, key = _cfg("SUPABASE_URL"), (_cfg("SUPABASE_SERVICE_ROLE")
                                     or _cfg("SUPABASE_SERVICE_ROLE_KEY"))
    if not (url and key):
        return None, None
    return url.rstrip("/") + "/rest/v1", {"apikey": key, "Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"}


def _table_missing(r):
    if r is None or r.status_code != 404:
        return False
    try:
        return (r.json() or {}).get("code") == "PGRST205"
    except ValueError:
        return False


def sb_ensure_table():
    """Management API 로 CREATE TABLE IF NOT EXISTS(멱등). 토큰 없으면 False."""
    url, tok = _cfg("SUPABASE_URL"), _cfg("SUPABASE_ACCESS_TOKEN")
    if not (url and tok):
        return False
    ref = url.split("//")[-1].split(".")[0]
    try:
        r = requests.post(f"https://api.supabase.com/v1/projects/{ref}/database/query",
                          headers={"Authorization": "Bearer " + tok,
                                   "Content-Type": "application/json"},
                          data=json.dumps({"query": DDL}), timeout=30)
        r.raise_for_status()
        _log("테이블 생성/확인 완료")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"테이블 생성 실패: {type(e).__name__}")
        return False


def sb_get(kind):
    base, headers = _rest()
    if not base:
        return False, None
    try:
        r = requests.get(f"{base}/{TABLE}?select=*&kind=eq.{kind}&limit=1",
                         headers=headers, timeout=15)
        if _table_missing(r):
            return False, None
        r.raise_for_status()
        d = r.json()
        return True, (d[0] if isinstance(d, list) and d else None)
    except Exception as e:  # noqa: BLE001
        _log(f"스냅샷 조회 실패({kind}): {type(e).__name__}")
        return False, None


def sb_put(kind, payload, meta, item_count):
    base, headers = _rest()
    if not base:
        return False, "no SUPABASE_URL/SERVICE_ROLE"
    row = {"kind": kind,
           "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "source_host": _cfg("MIRI_BRIDGE_HOST", "unknown")[:64],
           "item_count": int(item_count), "payload": payload, "meta": meta}
    h = dict(headers)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    body = json.dumps([row], ensure_ascii=False, allow_nan=False).encode("utf-8")
    url = f"{base}/{TABLE}?on_conflict=kind"
    try:
        r = requests.post(url, headers=h, data=body, timeout=30)
        if _table_missing(r):
            if not sb_ensure_table():
                return False, "table_missing(관리 토큰 없음/생성 실패)"
            time.sleep(2)                 # PostgREST 스키마 캐시 반영 지연(실측 수 초)
            r = requests.post(url, headers=h, data=body, timeout=30)
        r.raise_for_status()
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        _log(f"스냅샷 기록 실패({kind}): {type(e).__name__}")
        return False, type(e).__name__


# ---------------------------------------------------------------- 사이클
def run_prices(max_codes=DEFAULT_MAX_CODES, dry=False):
    codes, src = fetch_wanted_codes(max_codes)
    if not codes:
        return {"kind": KIND_PRICES, "ok": False, "reason": "no_codes"}
    t0 = time.time()
    results, stats = collect_prices(codes)
    stats["codes_from"] = src
    stats["elapsed_s"] = round(time.time() - t0, 1)
    if not results:
        _log(f"시세 0건 → 기록하지 않는다(직전 스냅샷 유지). {stats.get('errors')[:2]}")
        return {"kind": KIND_PRICES, "ok": False, "reason": "resolved_0", "stats": stats}
    if dry:
        _log(f"[dry] 기록 생략. resolved={len(results)}/{len(codes)} {stats['elapsed_s']}s")
        return {"kind": KIND_PRICES, "ok": True, "dry": True, "stats": stats}
    ok, reason = sb_put(KIND_PRICES, results, stats, len(results))
    _log(f"시세 스냅샷 기록 {'성공' if ok else '실패:' + reason} "
         f"resolved={len(results)}/{len(codes)} {stats['elapsed_s']}s")
    return {"kind": KIND_PRICES, "ok": ok, "reason": reason, "stats": stats}


def run_scheduled(dry=False):
    t0 = time.time()
    try:
        items, stats = collect_scheduled()
    except Exception as e:  # noqa: BLE001
        _log(f"상장예정 수집 실패 → 기록하지 않는다(직전 스냅샷 유지): {type(e).__name__}")
        return {"kind": KIND_SCHEDULED, "ok": False, "reason": type(e).__name__}
    stats["elapsed_s"] = round(time.time() - t0, 1)
    if dry:
        _log(f"[dry] 기록 생략. total={stats['total']} etf={stats['etf']} stock={stats['stock']}")
        return {"kind": KIND_SCHEDULED, "ok": True, "dry": True, "stats": stats}
    ok, reason = sb_put(KIND_SCHEDULED, {"items": items}, stats, len(items))
    _log(f"상장예정 스냅샷 기록 {'성공' if ok else '실패:' + reason} "
         f"total={stats['total']} etf={stats['etf']} stock={stats['stock']}")
    return {"kind": KIND_SCHEDULED, "ok": ok, "reason": reason, "stats": stats}


def show():
    for k in (KIND_PRICES, KIND_SCHEDULED):
        ok, row = sb_get(k)
        if not ok:
            print(f"  {k}: 조회 실패(권한/네트워크/테이블 없음)")
        elif not row:
            print(f"  {k}: 없음")
        else:
            ts = row.get("collected_at")
            try:
                age = (dt.datetime.now(dt.timezone.utc)
                       - dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                       ).total_seconds() / 60.0
                age_s = f"{age:.1f}min"
            except Exception:  # noqa: BLE001
                age_s = "?"
            print(f"  {k}: items={row.get('item_count')} age={age_s} "
                  f"host={row.get('source_host')} collected_at={ts}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="토스 시세 브리지 수집기")
    ap.add_argument("--kinds", default="prices,scheduled",
                    help="수집 대상: prices / scheduled / prices,scheduled")
    ap.add_argument("--max-codes", type=int, default=DEFAULT_MAX_CODES)
    ap.add_argument("--dry", action="store_true", help="수집만 하고 DB 기록 생략")
    ap.add_argument("--show", action="store_true", help="현재 스냅샷 상태만 출력")
    a = ap.parse_args()

    envs = _load_env_files()
    _log(f"설정 로드: {len(envs)}개 .env · SUPABASE={'Y' if _cfg('SUPABASE_URL') else 'N'} "
         f"SERVICE_ROLE={'Y' if (_cfg('SUPABASE_SERVICE_ROLE') or _cfg('SUPABASE_SERVICE_ROLE_KEY')) else 'N'} "
         f"TOSS_KEY={'Y' if _cfg('TOSS_APP_KEY') else 'N'} host={_cfg('MIRI_BRIDGE_HOST', 'unknown')}")

    if a.show:
        show()
        return 0

    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    out = []
    if KIND_PRICES in kinds:
        out.append(run_prices(max_codes=a.max_codes, dry=a.dry))
    if KIND_SCHEDULED in kinds:
        out.append(run_scheduled(dry=a.dry))
    print(json.dumps({"results": out}, ensure_ascii=False))
    return 0 if all(r.get("ok") for r in out) else 1


if __name__ == "__main__":
    sys.exit(main())

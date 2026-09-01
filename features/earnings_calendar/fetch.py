# -*- coding: utf-8 -*-
"""증분 수집: DART list.json 정기공시(pblntf_ty=A) → bench_cache/dart/ 확장.

collect.py 는 «순수 로컬 IO»(DART 0콜)를 유지한다. 라이브 콜은 «이 파일에만» 있다.
build_impact_benchmark.py 는 import 하지 않는다(패키지 격리 락 유지) — 파일명 규칙만
동일하게 재현해 기존 캐시(2021~2026-05)와 그대로 호환된다.

파일명 규칙(기존 캐시와 동일):
    {prefix}{ty}_{bgn_de}_{end_de}.json      prefix: KOSPI(corp_cls=Y)="" · KOSDAQ(K)="K_"
    예) A_20260701_20260930.json  ·  K_A_20260701_20260930.json

★수집 유형을 pblntf_ty=A(정기공시) «하나로» 좁혔다 [실측 근거].
  기존 캐시 342,584행 중 정기보고서 58,860건의 내역: A_ 20,258 + K_A_ 38,494 = 58,752
  (99.82%). B_(주요사항)은 0건, I_(기타)는 108건(0.18%)인데 I_ 는 행수가 A_ 의 수 배라
  콜 비용 대비 이득이 없다. → A 만 수집한다.

콜 예산 [실측 2026-09-01]:
  분기 1창당 total_page = KOSPI 10 · KOSDAQ 21~22 → 약 32콜/분기.
  기본 창(3년=12분기) 전체 재수집 ≈ 384콜. 로컬처럼 캐시가 이미 있으면 «미완결 분기만»
  재수집 → 약 32콜. OpenDART 일 한도 20,000콜 대비 충분히 안전하다.

한도 존중:
  - 페이지 간 0.12s 슬립.
  - status 020(유량초과) → 30s 대기 후 재시도, 창당 최대 _RATE_RETRY(3)회. 무한재시도 없음.
  - 네트워크 예외 → 2s 후 재시도, 페이지당 최대 4회. 초과하면 그 창을 «실패»로 올린다.
  - 실행 전체 콜 상한 max_calls(기본 3000). 넘으면 즉시 중단 — 조용한 폭주 차단.
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402

CACHE_DIR = os.path.join(_ROOT, "bench_cache", "dart")

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
MARKETS = {"Y": "KOSPI", "K": "KOSDAQ"}
PBLNTF_TY = "A"                     # 정기공시만(위 근거)
# 종목코드 숫자 6자리 가정 금지 - 0126Z0 처럼 영문 포함 코드가 실재한다
# (features/calendar_events/dart_src.py 의 CODE_RE 와 동일 규칙).
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")

_PAGE_SLEEP = 0.12
_RATE_SLEEP = 30.0
_RATE_RETRY = 3
_NET_RETRY = 4

CALLS = {"list": 0}


def _log(msg):
    print("[earn-cal][fetch] " + msg, flush=True)


def _key():
    k = getattr(config, "DART_API_KEY", "") or ""
    if not k:
        raise RuntimeError("DART_API_KEY missing")
    return k


# ---------------------------------------------------------------- 분기 창
def _q_start(d):
    """d 가 속한 분기의 1일."""
    return dt.date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


def _q_end(qs):
    """분기시작일 qs 가 속한 분기의 말일."""
    y, m = qs.year, qs.month + 3
    if m > 12:
        y, m = y + 1, m - 12
    return dt.date(y, m, 1) - dt.timedelta(days=1)


def quarter_windows(bgn, end):
    """[bgn,end] 를 달력분기 창으로. (bgn_de, end_de, is_complete) 리스트.

    DART 는 corp_code 없는 조회에서 기간 3개월 이하만 허용하므로 달력분기로 정확히 자른다.
    is_complete = 그 분기가 «이미 끝났고» 창이 분기 전체를 덮는가(=다시 받을 필요 없음).
    """
    out = []
    cur = _q_start(bgn)
    while cur <= end:
        qe = _q_end(cur)
        b = max(cur, bgn)
        e = min(qe, end)
        complete = (b == cur and e == qe)
        out.append((b.strftime("%Y%m%d"), e.strftime("%Y%m%d"), complete))
        cur = qe + dt.timedelta(days=1)
    return out


def _fname(cls, bgn_de, end_de):
    prefix = "" if cls == "Y" else (cls + "_")
    return os.path.join(CACHE_DIR, "%s%s_%s_%s.json" % (prefix, PBLNTF_TY, bgn_de, end_de))


def _stale_partials(cls, q_bgn, keep_end):
    """같은 분기(q_bgn)로 시작하지만 end 가 다른 «미완결 잔재» 파일 경로들.

    2026-04-01~05-15 처럼 과거에 부분 수집된 파일이 남아 있으면, 분기 전체를 새로 받은 뒤
    이 잔재를 지운다. 안 지우면 collect 가 같은 접수를 두 번 읽는다(중복 lag 표본).
    """
    prefix = "" if cls == "Y" else (cls + "_")
    head = "%s%s_%s_" % (prefix, PBLNTF_TY, q_bgn)
    keep = head + keep_end + ".json"
    out = []
    try:
        for n in os.listdir(CACHE_DIR):
            if n.startswith(head) and n.endswith(".json") and n != keep:
                out.append(os.path.join(CACHE_DIR, n))
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 라이브 수집
def _fetch_window(cls, bgn_de, end_de, session, max_calls):
    """한 시장·한 창의 전 페이지. 실패는 예외(호출측이 창 단위로 집계)."""
    items = []
    page, total_page, rate_hits = 1, 1, 0
    while page <= total_page:
        if CALLS["list"] >= max_calls:
            raise RuntimeError("call budget exceeded (%d)" % max_calls)
        params = {"crtfc_key": _key(), "corp_cls": cls, "pblntf_ty": PBLNTF_TY,
                  "bgn_de": bgn_de, "end_de": end_de, "page_no": page, "page_count": 100}
        d = None
        for attempt in range(_NET_RETRY):
            try:
                CALLS["list"] += 1
                d = session.get(LIST_URL, params=params, timeout=30).json()
            except Exception as e:  # noqa: BLE001
                _log("  req err %s %s p%d (%d/%d): %s"
                     % (cls, bgn_de, page, attempt + 1, _NET_RETRY, repr(e)[:80]))
                d = None
                time.sleep(2)
                continue
            st = d.get("status")
            if st == "013":                    # 데이터 없음 = 정상 0건
                return items
            if st == "020":                    # 유량 초과 -> 대기 후 재시도(상한 있음)
                rate_hits += 1
                if rate_hits > _RATE_RETRY:
                    raise RuntimeError("DART rate-limit(020) %d회 - 중단" % rate_hits)
                _log("  rate-limit(020) %s %s p%d -> %.0fs 대기" % (cls, bgn_de, page, _RATE_SLEEP))
                time.sleep(_RATE_SLEEP)
                d = None
                continue
            if st != "000":
                raise RuntimeError("list.json status=%s %s" % (st, d.get("message")))
            break
        if not isinstance(d, dict) or d.get("status") != "000":
            raise RuntimeError("list.json 재시도 소진 %s %s~%s p%d" % (cls, bgn_de, end_de, page))
        total_page = int(d.get("total_page") or 0)
        for it in d.get("list", []):
            if it.get("corp_cls") != cls:
                continue
            sc = (it.get("stock_code") or "").strip().upper()
            if not CODE_RE.match(sc):
                continue
            items.append({
                "rcept_no": it.get("rcept_no"),
                "corp_code": it.get("corp_code"),
                "corp_name": it.get("corp_name"),
                "stock_code": sc,
                "market": MARKETS[cls],
                "report_nm": (it.get("report_nm") or "").strip(),
                "rcept_dt": (it.get("rcept_dt") or "").strip(),
            })
        page += 1
        time.sleep(_PAGE_SLEEP)
    return items


def ensure_cache(years_back=3, today=None, max_calls=3000, force=False):
    """bench_cache/dart 를 오늘까지 확장. 반환 stats dict(예외를 던지지 않는다 - errors 로 보고).

    · 이미 있는 «완결 분기» 파일은 건너뛴다(0콜).
    · 진행 중인 분기(오늘이 속한 분기)는 항상 다시 받는다.
    · 과거 분기인데 부분 파일만 있으면 분기 전체로 다시 받고 잔재를 지운다.
    """
    today = today or dt.date.today()
    bgn = dt.date(today.year - years_back, 1, 1)
    os.makedirs(CACHE_DIR, exist_ok=True)
    CALLS["list"] = 0
    t0 = time.time()
    stats = {"windows": 0, "fetched": 0, "skipped": 0, "rows": 0, "removed_partials": 0,
             "errors": [], "calls": 0, "bgn": bgn.isoformat(), "end": today.isoformat()}
    session = requests.Session()
    for cls in MARKETS:
        for (b, e, complete) in quarter_windows(bgn, today):
            stats["windows"] += 1
            q_bgn = _q_start(dt.date(int(b[:4]), int(b[4:6]), int(b[6:8]))).strftime("%Y%m%d")
            path = _fname(cls, b, e)
            if os.path.exists(path) and complete and not force:
                stats["skipped"] += 1
                continue
            try:
                items = _fetch_window(cls, b, e, session, max_calls)
            except Exception as ex:  # noqa: BLE001
                msg = "%s %s~%s: %s: %s" % (cls, b, e, type(ex).__name__, str(ex)[:140])
                stats["errors"].append(msg)
                _log("  실패(창 스킵) " + msg)
                if "call budget" in str(ex):
                    stats["calls"] = CALLS["list"]
                    return stats
                continue
            tmp = path + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as fh:
                json.dump(items, fh, ensure_ascii=False)
            os.replace(tmp, path)
            for old in _stale_partials(cls, q_bgn, e):
                try:
                    os.remove(old)
                    stats["removed_partials"] += 1
                    _log("  잔재 제거 " + os.path.basename(old))
                except OSError:
                    pass
            stats["fetched"] += 1
            stats["rows"] += len(items)
            _log("  %s %s~%s -> %d행 (누적콜 %d)" % (MARKETS[cls], b, e, len(items), CALLS["list"]))
    stats["calls"] = CALLS["list"]
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    _log("완료 창%d 수집%d 스킵%d 행%d 콜%d 오류%d %ss"
         % (stats["windows"], stats["fetched"], stats["skipped"], stats["rows"],
            stats["calls"], len(stats["errors"]), stats.get("elapsed_sec")))
    return stats


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--max-calls", type=int, default=3000)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    print(json.dumps(ensure_cache(years_back=a.years, max_calls=a.max_calls, force=a.force),
                     ensure_ascii=False, indent=2))

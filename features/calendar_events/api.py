# -*- coding: utf-8 -*-
"""GET /api/calendar/events?ym=YYYY-MM — 캐시 파일 전용(요청경로 외부 호출 0).

응답:
  {as_of, generated_at, ym|null, count, counts_by_type:{dividend,ipo,etf_listing},
   items:[event...], months:[YYYY-MM...], sources:{dart:{ok,checked_at}, toss:{ok,checked_at,etf,scheduled}},
   market_scope, disclaimer}
  ym 없음 → 전달 1일 이후 «전량»(실측 ≈ 수십 KB · gzip 미들웨어 적용).
  어떤 예외에도 500 금지(200 + 빈-정형).

스케줄러(start_scheduler): 데몬 스레드. 기동 90초 후 캐시가 12h 이상 낡았으면 1회, 이후 12h 주기.
  GONGSI_CAL_EVENTS_BATCH=0 이면 비활성(테스트·로컬). 다른 레인의 공통 스케줄러와 «분리된 새 파일».
"""
import datetime as dt
import hashlib
import json
import os
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import Response

from features.calendar_events import build

router = APIRouter()
_MEM = {"ts": 0.0, "mtime": None, "data": None}
_LOCK = threading.Lock()
_TTL = 120.0
_CC = "public, s-maxage=300, stale-while-revalidate=600"
_EMPTY = {"as_of": None, "generated_at": None, "ym": None, "count": 0,
          "counts_by_type": {"dividend": 0, "ipo": 0, "etf_listing": 0}, "items": [], "months": [],
          "sources": {}, "market_scope": "코스피·코스닥", "disclaimer": build.DISCLAIMER}


def _load():
    try:
        mtime = os.path.getmtime(build.CACHE_FILE)
    except OSError:
        return None
    now = time.time()
    with _LOCK:
        if _MEM["data"] is not None and _MEM["mtime"] == mtime and (now - _MEM["ts"]) < _TTL:
            return _MEM["data"]
    d = build.load_cache()
    with _LOCK:
        _MEM.update({"ts": now, "mtime": mtime, "data": d})
    return d


def _payload(ym):
    d = _load()
    if not d or not isinstance(d.get("events"), list):
        return dict(_EMPTY, ym=ym)
    items = d["events"]
    if ym:
        items = [e for e in items if (e.get("date") or "").startswith(ym)]
    src = {}
    for k, v in (d.get("sources") or {}).items():
        src[k] = {kk: vv for kk, vv in v.items() if kk in ("ok", "checked_at", "etf", "scheduled", "stock", "error")}
    return {
        "as_of": d.get("as_of"), "generated_at": d.get("generated_at"), "ym": ym,
        "count": len(items), "counts_by_type": build._counts(items),
        "items": items,
        "months": sorted({(e.get("date") or "")[:7] for e in d["events"] if e.get("date")}),
        "sources": src, "market_scope": "코스피·코스닥", "disclaimer": build.DISCLAIMER,
    }


def _json(request: Request, payload):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    etag = 'W/"' + hashlib.md5(body).hexdigest() + '"'
    headers = {"Cache-Control": _CC, "ETag": etag}
    inm = request.headers.get("if-none-match", "")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/api/calendar/events")
def get_calendar_events(request: Request, ym: str = None):
    ym = (ym or "").strip() or None
    if ym and not (len(ym) == 7 and ym[4] == "-" and ym[:4].isdigit() and ym[5:].isdigit()):
        ym = None
    try:
        return _json(request, _payload(ym))
    except Exception as e:  # noqa: BLE001
        print(f"[cal-events][api] 예외 폴백: {type(e).__name__} {e}")
        return _json(request, dict(_EMPTY, ym=ym))


# ---------------------------------------------------------------- 스케줄러
_PERIOD = float(os.getenv("GONGSI_CAL_EVENTS_PERIOD_SEC", str(12 * 3600)))
_BOOT_DELAY = float(os.getenv("GONGSI_CAL_EVENTS_BOOT_DELAY_SEC", "90"))
_ENABLED = os.getenv("GONGSI_CAL_EVENTS_BATCH", "1").strip().lower() not in ("0", "false", "no", "")
_STARTED = {"v": False}


def _stale(hours=12.0):
    try:
        return (time.time() - os.path.getmtime(build.CACHE_FILE)) > hours * 3600
    except OSError:
        return True


def _loop():
    time.sleep(_BOOT_DELAY)
    while True:
        try:
            if _stale():
                s = build.run()
                print(f"[cal-events][sched] run ok={s['ok']} counts={s.get('counts')} calls={s['calls']}")
            else:
                print("[cal-events][sched] 캐시 신선(12h 미만) — 스킵")
        except Exception as e:  # noqa: BLE001
            print(f"[cal-events][sched] 실패(무시, 다음 주기): {type(e).__name__}: {e}")
        time.sleep(_PERIOD)


def start_scheduler():
    if not _ENABLED or _STARTED["v"]:
        return False
    _STARTED["v"] = True
    threading.Thread(target=_loop, name="cal-events-batch", daemon=True).start()
    print(f"[cal-events][sched] 기동(지연 {_BOOT_DELAY:g}s, 주기 {_PERIOD / 3600:g}h)")
    return True

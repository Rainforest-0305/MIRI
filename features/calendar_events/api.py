# -*- coding: utf-8 -*-
"""GET /api/calendar/events?ym=YYYY-MM — 캐시 파일 전용(요청경로 외부 호출 0).

응답:
  {as_of, generated_at, ym|null, count, counts_by_type:{dividend,ipo,etf_listing},
   items:[event...], months:[YYYY-MM...], sources:{dart:{ok,checked_at}, toss:{ok,checked_at,etf,scheduled}},
   market_scope, disclaimer}
  ym 없음 → 전달 1일 이후 «전량»(실측 ≈ 수십 KB · gzip 미들웨어 적용).
  어떤 예외에도 500 금지(200 + 빈-정형).

스케줄러(start_scheduler): 데몬 스레드. 기동 90초 후 캐시가 12h 이상 낡았으면 1회, 이후 12h 주기.
  ★「낡음」은 파일 mtime 이 아니라 캐시 «내용»의 마지막 배치 시각으로 잰다(_stale 주석 참조).
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


# ---- 신선도 판정 (2026-10-12 수정: 파일 mtime → 캐시 «내용»의 마지막 배치 시각) ----
# 이 가드가 막는 것: 프로세스가 뜰 때마다(재시작·리로드) 배치가 한 번씩 도는 것.
#   배치 1회 = DART 수~수십 콜, 시드에서 따라잡을 때는 약 200콜(실측 10-05 182 · 10-11 203).
#   디스크가 유지되는 호스트(노트북·로컬 개발)에서는 「방금 만든 캐시」를 재시작마다
#   다시 만들지 않게 해 준다 — 그 보호는 그대로 둔다.
# 고친 것: 종전에는 «파일 mtime» 으로 쟀다. Render 는 배포마다 컨테이너를 새로 만들고
#   data/calendar_events.json(.gitignore 예외로 커밋된 시드, Dockerfile `COPY . .`)이 이미지에
#   구워져 들어간다. 그 파일의 mtime 은 «빌드 시각»이라 데이터가 2026-08-29 것이어도
#   12h 동안 「신선」으로 판정됐다 → 배포할 때마다 캘린더가 08-29 시드로 12h 돌아갔다
#   (실측 2026-10-11: 배포 4회 모두 「캐시 신선(12h 미만) — 스킵」, sources.checked_at=08-29).
#   mtime 은 「파일이 언제 놓였나」이지 「데이터가 언제 만들어졌나」가 아니다.
# 고르지 않은 대안: 「기동 후 첫 사이클은 무조건 재생성」 — Render 에서는 결과가 같지만
#   디스크가 유지되는 호스트에서도 재시작마다 돌아 호출이 «더» 늘어난다(위 보호를 없앤다).
# 남는 비용: Render 에서는 부팅 1회당 따라잡기 1회(약 200콜). 부팅 = 배포·재시작이다.
_STALE_HOURS = 12.0
# 캐시의 시각은 tz 없는 «로컬» 시각이다(build.py dt.datetime.now()). 시드는 노트북(KST)에서
# 만들고 Render(UTC)에서 읽으므로 최대 9시간 «미래»로 보일 수 있다 — 그 범위는 정상으로 본다.
_TZ_SKEW_HOURS = 9.0


def _last_batch_at(cache):
    """캐시 «내용»에 적힌 마지막 배치 시각(naive). 없거나 못 읽으면 None.

    generated_at(이벤트 재생성 성공 시) 과 sources.*.checked_at(성공·실패 모두 기록) 중
    가장 늦은 값 = 종전 mtime 이 뜻하던 「마지막으로 배치가 저장한 때」의 내용판."""
    cands = [cache.get("generated_at")]
    for v in (cache.get("sources") or {}).values():
        if isinstance(v, dict):
            cands.append(v.get("checked_at"))
    out = []
    for c in cands:
        try:
            t = dt.datetime.fromisoformat(str(c))
        except (TypeError, ValueError):
            continue
        out.append(t.replace(tzinfo=None))
    return max(out) if out else None


def _stale_info(hours=None, now=None):
    """(stale: bool, last: datetime|None, age_h: float|None). 판정 근거를 같이 돌려준다."""
    hours = _STALE_HOURS if hours is None else float(hours)
    last = _last_batch_at(build.load_cache())      # 파일 없음·손상 → 빈 캐시 → None
    if last is None:
        return True, None, None
    age_h = ((now or dt.datetime.now()) - last).total_seconds() / 3600.0
    if age_h < -(_TZ_SKEW_HOURS + 1.0):
        return True, last, age_h                   # tz 차이로 설명 안 되는 미래 시각 = 믿지 않는다
    return age_h > hours, last, age_h


def _stale(hours=None, now=None):
    return _stale_info(hours, now)[0]


def _loop():
    time.sleep(_BOOT_DELAY)
    while True:
        try:
            stale, last, age_h = _stale_info()
            last_s = last.isoformat(timespec="seconds") if last else "없음"
            age_s = f"{age_h:.1f}h" if age_h is not None else "?"
            if stale:
                print(f"[cal-events][sched] 캐시 낡음(내용 기준 마지막 배치 {last_s} · {age_s} > "
                      f"{_STALE_HOURS:g}h) → 재생성")
                s = build.run()
                print(f"[cal-events][sched] run ok={s['ok']} counts={s.get('counts')} calls={s['calls']}")
            else:
                print(f"[cal-events][sched] 캐시 신선(내용 기준 마지막 배치 {last_s} · {age_s} ≤ "
                      f"{_STALE_HOURS:g}h) — 스킵")
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

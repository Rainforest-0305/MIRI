# -*- coding: utf-8 -*-
"""배치: DART 증분수집 → 정기보고서 추출 → 다음 발표일 예측 → data/earnings_calendar.json.

실행:  python -m features.earnings_calendar.build [--years 3] [--no-fetch] [--dry]

★이 파일이 생긴 이유(2026-09-01 사고):
  이 모듈에는 «재생성 배치가 아예 없었다». 2026-05-15 스냅샷이 그대로 굳어 있었고,
  그 안의 예측 2,593건 중 2,530건이 2026-08 분(반기보고서)이라 8월이 지나며 전량
  만료 → API 응답 13건 → 실적발표 탭 3.5개월 빈 화면. 아무 경보도 없었다.
  (배당·상장은 features/calendar_events/api.py:start_scheduler 로 매일 도는데
   실적발표에는 그 대응물이 없었다. 이 파일 + batch.py 가 그 대응물이다.)

정책(calendar_events/build.py 선례를 그대로 따른다):
  · 원자적 저장(tmp → os.replace).
  · 실패하면 «직전 파일을 유지»한다. 빈 값·급감한 값으로 덮지 않는다.
    ★_MIN_PREDICTIONS 바닥값 — Render 처럼 bench_cache(23MB, gitignore) 가 없는
      환경에서 이 배치가 돌면 collect 가 0건을 내는데, 그걸 그대로 쓰면 시드를
      «빈 파일로 덮어» 사고를 우리가 직접 재현하게 된다. 그래서 바닥 미만이면 쓰지 않는다.
  · fetch 실패해도 기존 캐시로 예측은 계속 시도한다(부분 성공 허용).
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config    # noqa: E402
# ★맨이름 import 금지(predict.py 상단 주석의 실사고 참조) — 패키지 경로로만 부른다.
from features.earnings_calendar import collect   # noqa: E402
from features.earnings_calendar import fetch     # noqa: E402
from features.earnings_calendar import predict   # noqa: E402

CACHE_FILE = config.DATA / "earnings_calendar.json"          # 배포 시드(추적 대상)
OUT_DIR = os.path.join(_HERE, "out")
OUT_FILE = os.path.join(OUT_DIR, "earnings_calendar.json")   # 개발 폴백(app.py 가 2순위로 읽음)
VERSION = 2

# 예측이 이 수 미만이면 «쓰지 않는다»(빈 값 덮어쓰기 사고 차단).
# 근거: 정상 실행 실측 2,651건. 상장사 수가 반토막 날 일은 없으므로 500 은 명백한 고장 신호.
_MIN_PREDICTIONS = int(os.getenv("GONGSI_EARN_MIN_PREDICTIONS", "500"))
_FETCH_YEARS = int(os.getenv("GONGSI_EARN_FETCH_YEARS", "3"))
_MAX_CALLS = int(os.getenv("GONGSI_EARN_MAX_CALLS", "3000"))


def _log(msg):
    print("[earn-cal] " + msg, flush=True)


def load_cache():
    try:
        with io.open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return None


def _save(path, payload, mkdirs=None):
    if mkdirs:
        os.makedirs(mkdirs, exist_ok=True)
    tmp = str(path) + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, str(path))


def run(years=None, do_fetch=True, dry=False, today=None):
    """1사이클. 반환 요약 dict(ok, count, calls, errors, months). 실패 시 캐시 무변경."""
    t0 = time.time()
    today = today or dt.date.today()
    years = _FETCH_YEARS if years is None else years
    out = {"ok": False, "count": 0, "calls": 0, "errors": [], "months": {},
           "as_of": None, "generated_at": None, "dropped_sanity": 0, "skipped": 0}

    # 1) DART 증분수집(라이브). 실패해도 기존 캐시로 계속 간다.
    if do_fetch and not dry:
        try:
            fs = fetch.ensure_cache(years_back=years, today=today, max_calls=_MAX_CALLS)
            out["calls"] = fs.get("calls", 0)
            out["fetch"] = {k: fs.get(k) for k in
                            ("windows", "fetched", "skipped", "rows", "removed_partials")}
            if fs.get("errors"):
                out["errors"] += ["fetch: " + e for e in fs["errors"][:5]]
        except Exception as e:  # noqa: BLE001
            out["errors"].append("fetch: %s: %s" % (type(e).__name__, str(e)[:160]))
            _log("수집 실패(기존 캐시로 계속): " + out["errors"][-1])

    # 2) 로컬 캐시 → 정기보고서 추출 → 예측 (DART 0콜)
    try:
        records, cstats = collect.load_all()
        groups = collect.group_by_corp(records)
        as_of = collect.dataset_as_of(records)
        preds, skipped, dropped = predict.predict_all(groups)
    except Exception as e:  # noqa: BLE001
        out["errors"].append("predict: %s: %s" % (type(e).__name__, str(e)[:160]))
        _log("예측 실패(캐시 유지): " + out["errors"][-1])
        return out

    months = {}
    for p in preds:
        k = p["predicted_date"][:7]
        months[k] = months.get(k, 0) + 1
    out.update({"count": len(preds), "as_of": as_of, "months": months,
                "dropped_sanity": len(dropped), "skipped": skipped,
                "collect": cstats})

    # 3) 바닥값 검사 — 못 넘으면 «쓰지 않는다»
    if len(preds) < _MIN_PREDICTIONS:
        out["errors"].append(
            "예측 %d건 < 바닥값 %d — 기존 파일을 유지한다(빈 값 덮어쓰기 차단). "
            "bench_cache/dart 가 비어 있는 환경일 가능성이 높다(파일 %s개)."
            % (len(preds), _MIN_PREDICTIONS, cstats.get("files")))
        _log("중단: " + out["errors"][-1])
        return out

    generated_at = dt.datetime.now().isoformat(timespec="seconds")
    payload = {
        "version": VERSION,
        "as_of": as_of,                       # DART 데이터 스냅샷 기준일(YYYYMMDD)
        "generated_at": generated_at,         # 이 배치가 «실제로 돈» 시각
        "count": len(preds),
        "months": months,
        "stats": {"corps": len(groups), "skipped": skipped,
                  "dropped_sanity": len(dropped),
                  "files": cstats.get("files"), "periodic_rows": cstats.get("periodic_rows"),
                  "dup_dropped": cstats.get("dup_dropped")},
        "dropped_sample": [{k: d.get(k) for k in
                            ("stock_code", "corp_name", "predicted_date", "dropped_reason")}
                           for d in dropped[:20]],
        "predictions": preds,
    }
    if dry:
        out["ok"] = True
        out["dry"] = True
        _log("dry-run: 저장 생략")
        return out

    try:
        _save(CACHE_FILE, payload)
        _save(OUT_FILE, payload, mkdirs=OUT_DIR)
    except Exception as e:  # noqa: BLE001
        out["errors"].append("save: %s: %s" % (type(e).__name__, str(e)[:160]))
        return out

    out["ok"] = True
    out["generated_at"] = generated_at
    _log("완료 as_of=%s 예측 %d건 (폐기 %d · 콜 %d · %.1fs) 상위월 %s"
         % (as_of, len(preds), len(dropped), out["calls"], time.time() - t0,
            sorted(months.items(), key=lambda kv: -kv[1])[:3]))
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=None)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    r = run(years=a.years, do_fetch=not a.no_fetch, dry=a.dry)
    r.pop("collect", None)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)

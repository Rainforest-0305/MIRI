# -*- coding: utf-8 -*-
"""실적발표 캘린더 재생성 스케줄러(데몬 스레드).

★배당·상장(features/calendar_events/api.py 의 start_scheduler)과 «같은 패턴»이다.
  새 구조를 만들지 않았다 — 기동 지연 후 1회 점검, 이후 주기 점검, 낡았을 때만 실행,
  예외는 삼키고 다음 주기로, 환경변수로 끌 수 있음.

주기 결정 근거 (지시: 「최소 분기 1회, 권장 월 1회」 → 그보다 «촘촘하게» 잡았다):
  1) 분기·월 주기는 이번 사고와 «같은 실패»를 짧게 재현한다. 정기보고서는 웨이브가
     분기당 1회뿐이라, 웨이브가 지나가면 전 종목 예측이 다음 슬롯으로 갈아타야 한다.
     app.py 는 오늘 이전 예측을 버리므로(_build_earnings_calendar), 갈아타기가 늦으면
     그 기간 «탭이 다시 빈다». 월 1회면 최대 30일, 분기 1회면 최대 90일 빈다.
     3일 주기면 최대 3일.
  2) 비용이 사실상 0이다 [실측 2026-09-01]:
     캐시가 있으면 진행 중 분기만 재수집 = **62콜**. 캐시가 비어 있어도(Render 는
     bench_cache 가 .gitignore 라 비어 있다) 3년 전량 ≈ **384콜**.
     OpenDART 일 한도 20,000콜 대비 0.3%~1.9%.
  3) 그래서 «데이터가 3일 이상 낡았을 때만» 실행하고, 점검은 12시간마다 한다
     (calendar_events 의 12h 점검 주기를 그대로 따랐다).
환경변수:
  GONGSI_EARN_BATCH=0            비활성(테스트·로컬)
  GONGSI_EARN_PERIOD_SEC         점검 주기(기본 43200 = 12h)
  GONGSI_EARN_STALE_H            이 시간 이상 낡으면 실행(기본 72 = 3일)
  GONGSI_EARN_BOOT_DELAY_SEC     기동 지연(기본 180)
"""
import datetime as dt
import os
import threading
import time

from features.earnings_calendar import build

_PERIOD = float(os.getenv("GONGSI_EARN_PERIOD_SEC", str(12 * 3600)))
_BOOT_DELAY = float(os.getenv("GONGSI_EARN_BOOT_DELAY_SEC", "180"))
_STALE_H = float(os.getenv("GONGSI_EARN_STALE_H", "72"))
_ENABLED = os.getenv("GONGSI_EARN_BATCH", "1").strip().lower() not in ("0", "false", "no", "")
_STARTED = {"v": False}
STATE = {"last_run_at": None, "last_ok": None, "last_count": None,
         "last_error": None, "runs": 0}


def data_age_hours(now=None):
    """data/earnings_calendar.json 이 «데이터 기준으로» 몇 시간 낡았나.

    파일 mtime 이 아니라 내용(generated_at → 없으면 as_of)을 본다. 배포로 파일이 새로
    복사되면 mtime 은 새것이 되지만 «데이터는 그대로 낡은 채»라, mtime 을 보면 3.5개월
    정지를 또 못 본다(이번 사고의 관측 실패 그대로다).
    """
    now = now or dt.datetime.now()
    d = build.load_cache()
    if not isinstance(d, dict):
        return None
    g = d.get("generated_at")
    if g:
        try:
            return (now - dt.datetime.fromisoformat(str(g))).total_seconds() / 3600.0
        except ValueError:
            pass
    a = str(d.get("as_of") or "")
    if len(a) == 8 and a.isdigit():
        try:
            base = dt.datetime(int(a[:4]), int(a[4:6]), int(a[6:8]))
            return (now - base).total_seconds() / 3600.0
        except ValueError:
            pass
    return None


def _stale():
    age = data_age_hours()
    return True if age is None else (age > _STALE_H)


def _loop():
    time.sleep(_BOOT_DELAY)
    while True:
        try:
            age = data_age_hours()
            if _stale():
                s = build.run()
                STATE.update(last_run_at=dt.datetime.now().isoformat(timespec="seconds"),
                             last_ok=s.get("ok"), last_count=s.get("count"),
                             last_error=(s.get("errors") or [None])[0],
                             runs=STATE["runs"] + 1)
                print("[earn-cal][sched] run ok=%s count=%s calls=%s errors=%s"
                      % (s.get("ok"), s.get("count"), s.get("calls"), len(s.get("errors") or [])))
            else:
                print("[earn-cal][sched] 데이터 신선(%.1fh < %.0fh) - 스킵" % (age, _STALE_H))
        except Exception as e:  # noqa: BLE001
            STATE["last_error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
            print("[earn-cal][sched] 실패(무시, 다음 주기): " + STATE["last_error"])
        time.sleep(_PERIOD)


def start_scheduler():
    if not _ENABLED or _STARTED["v"]:
        return False
    _STARTED["v"] = True
    threading.Thread(target=_loop, name="earn-cal-batch", daemon=True).start()
    print("[earn-cal][sched] 기동(지연 %gs, 주기 %gh, 낡음기준 %gh)"
          % (_BOOT_DELAY, _PERIOD / 3600, _STALE_H))
    return True


def view():
    """/api/health 노출용. 외부 호출 0."""
    age = None
    try:
        age = data_age_hours()
    except Exception:  # noqa: BLE001
        pass
    return {"earnings_batch_enabled": _ENABLED,
            "earnings_data_age_h": (round(age, 1) if age is not None else None),
            "earnings_stale_threshold_h": _STALE_H,
            "earnings_last_run_at": STATE["last_run_at"],
            "earnings_last_ok": STATE["last_ok"],
            "earnings_last_count": STATE["last_count"],
            "earnings_last_error": STATE["last_error"],
            "earnings_runs": STATE["runs"]}

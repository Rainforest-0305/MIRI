# -*- coding: utf-8 -*-
"""MIRI 주간 배치 진입점 — Render Cron Job 용 (top100 → analyst, 순서를 «코드»로 강제).

왜 있는가 (2026-10-03):
  노트북 schtasks(Interactive 로그온 전용)가 토요일 새벽 노트북이 꺼져/로그오프돼 있으면
  조용히 빠졌다 — 09-19·09-26 2회 누락, 라이브 /api/top100·/api/analyst 3주 정체.
  24시간 떠 있는 Render Cron Job 으로 옮기고, 이 파일이 그 단일 진입점이다.

무엇을 하는가:
  1) 환경 점검 — SUPABASE_URL·SUPABASE_SERVICE_ROLE(_KEY) 가 없으면 «시작하지 않는다».
     ★수집기는 Supabase 쓰기에 실패해도 로컬 JSON 만 쓰고 rc=0 으로 끝난다. Render 의
       로컬 파일은 휘발성이고 라이브 API 는 Supabase 만 읽으므로, 그대로 두면
       「성공처럼 보이는 0건 반영」이 된다. 그래서 키 부재는 rc=2 로 즉시 실패시킨다.
  2) 이중 실행 가드 — Supabase 를 읽어 «오늘(KST) 이미 갱신된 단계»는 건너뛴다.
     노트북 schtasks(05:00/05:30)가 아직 살아 있는 이관 기간에 Render(06:00)가
     같은 작업을 한 번 더 하지 않게 한다. --force 로 무시.
  3) top100_collect.py 실행 → Supabase read-back 으로 «오늘자 100행»을 확인.
  4) data/top100.json 보장 — analyst 는 이 파일을 «입력»으로 읽는다(target_codes·
     display_names). top100 을 건너뛰었거나 실패했으면 Supabase 의 최신 스냅샷으로
     채운다(Render 컨테이너엔 이 파일이 없다 — data/* 는 .gitignore).
  5) analyst_collect.py --limit 0 --sleep 1.2 실행 → read-back 으로 top100 코드의
     «오늘자 커버리지»를 확인.
  6) 결과를 운영자 DM 으로 1줄 보고(notify_alert._tg_send — 실유저 채널은 구조적으로 불가).
     실패면 rc=1 → Render 도 실행 실패로 표시한다.

실행:
  python tools/weekly_batch.py            # 정규 실행 (Render Cron 기본 명령)
  python tools/weekly_batch.py --probe    # 읽기 전용 소스 도달성 점검. Supabase 쓰기·텔레그램 0
  python tools/weekly_batch.py --force    # 「오늘 이미 갱신」 가드 무시 (수동 재실행용)

종료코드:
  0 성공(또는 오늘 이미 갱신돼 건너뜀) / 1 단계 실패 / 2 환경 미비 / 4 --probe 실패

★요청 속도는 바꾸지 않는다: analyst --sleep 1.2 (President 지시 — 낮추지 말 것).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import config  # noqa: E402
import miri_cache as mc  # noqa: E402

KST = timezone(timedelta(hours=9))

ANALYST_SLEEP = "1.2"          # ★낮추지 말 것(President 지시). 노트북 schtasks 와 동일값
ANALYST_DEADLINE_MIN = 120     # analyst 자체 안전종료 데드라인(정상 소요 ≈ 8~10분)
TOP100_TIMEOUT_S = 15 * 60     # 하드 타임아웃(초과 시 SIGTERM → 2분 뒤 SIGKILL)
ANALYST_TIMEOUT_S = 150 * 60   # = 데드라인 120분 + 여유 30분
TOP100_MIN_ROWS = 100          # 「오늘자 top100 이 완성됐다」의 기준
ANALYST_OK_COVER = 0.90        # 실행 후 top100 코드 중 오늘자 비율 하한(성공 판정)
ANALYST_SKIP_COVER = 0.95      # 실행 전 이 비율 이상이면 「이미 돌았다」로 보고 건너뜀

_child = {"p": None}
_stop = {"hit": False, "sig": None}


def today_kst():
    return datetime.now(KST).date().isoformat()


def log(msg):
    print(f"[weekly] {msg}", flush=True)


def _where():
    """실행 위치 라벨(경보 문구용). Render 는 RENDER=true 와 서비스명을 주입한다."""
    if os.getenv("RENDER"):
        return "Render:" + (os.getenv("RENDER_SERVICE_NAME") or "?")
    return "local"


# ----------------------------------------------------------------- 경보
def notify(text):
    """운영자 개인 DM(config._TG_PAIRS). 실패해도 배치를 죽이지 않는다."""
    try:
        from notify_alert import _tg_send
        return bool(_tg_send(text))
    except Exception as e:  # noqa: BLE001
        log(f"경보 발송 실패: {type(e).__name__}")
        return False


# ----------------------------------------------------------------- Supabase read-back
def env_missing():
    miss = []
    if not (config.SUPABASE_URL or "").strip():
        miss.append("SUPABASE_URL")
    if not (config.SUPABASE_SERVICE_ROLE or "").strip():
        # anon 키로는 쓰기가 RLS 에 막힌다 — 서비스롤만 인정한다.
        miss.append("SUPABASE_SERVICE_ROLE(또는 _KEY)")
    return miss


def top100_rows():
    """(ok, rows) — market_cap_top100 전량(rank 오름차순). 읽기 전용."""
    return mc.select_columns(
        "market_cap_top100",
        "code,rank,name,market,market_cap,cap_label,updated_at",
        order="rank.asc")


def analyst_dates():
    """(ok, {code: updated_at}) — 읽기 전용."""
    ok, rows = mc.select_columns("analyst_consensus", "code,updated_at")
    return ok, {str(r.get("code")): r.get("updated_at")
                for r in (rows or []) if isinstance(r, dict)}


def count_today(rows, day):
    return sum(1 for r in (rows or [])
               if isinstance(r, dict) and str(r.get("updated_at") or "")[:10] == day)


def cover_today(codes, dates, day):
    """(비율, 적중수). codes 가 비면 (0.0, 0)."""
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0.0, 0
    hit = sum(1 for c in codes if str(dates.get(c) or "")[:10] == day)
    return hit / len(codes), hit


# ----------------------------------------------------------------- top100.json 보장
def ensure_top100_json(rows):
    """analyst 의 입력 파일(data/top100.json)을 보장한다.

    로컬 파일이 없거나 Supabase 스냅샷보다 낡았을 때만 Supabase 행으로 채운다.
    방금 top100_collect 가 성공했다면 로컬이 이미 최신이라 건드리지 않는다.
    반환: "kept" | "hydrated" | "none"(채울 데이터도 없음)."""
    path = config.DATA / "top100.json"
    local = mc.load_json(path, default=None)
    local_items = (local or {}).get("items") if isinstance(local, dict) else None
    local_upd = str((local or {}).get("updated_at") or "") if isinstance(local, dict) else ""
    sb_items = [r for r in (rows or []) if isinstance(r, dict) and r.get("code")]
    sb_upd = max((str(r.get("updated_at") or "") for r in sb_items), default="")
    if local_items and local_upd >= sb_upd:
        return "kept"
    if not sb_items:
        return "kept" if local_items else "none"
    items = [{
        "rank": int(r.get("rank") or 0),
        "code": str(r.get("code")),
        "name": r.get("name"),
        "market": r.get("market"),
        "market_cap": int(r.get("market_cap") or 0),
        "cap_label": r.get("cap_label"),
        "updated_at": r.get("updated_at"),
    } for r in sb_items]
    items.sort(key=lambda x: x["rank"])
    ok = mc.save_json(path, {"updated_at": sb_upd, "count": len(items), "items": items,
                             "_source": "supabase_hydrate"})
    return "hydrated" if ok else "none"


# ----------------------------------------------------------------- 하위 프로세스
def _on_signal(signum, frame):  # noqa: ARG001
    """Render 가 실행을 끊을 때(SIGTERM) 자식에게 그대로 넘긴다.
    analyst_collect 는 SIGTERM 을 받으면 «현재 종목까지 마치고» 안전 저장 후 종료한다."""
    _stop["hit"] = True
    _stop["sig"] = signum
    p = _child.get("p")
    if p is not None and p.poll() is None:
        try:
            p.send_signal(signum)
        except Exception:  # noqa: BLE001
            pass
    log(f"중단 신호(sig={signum}) 수신 — 자식에 전달, 이후 단계는 건너뜀")


def _install_signals():
    for nm in ("SIGTERM", "SIGINT"):
        s = getattr(signal, nm, None)
        if s is None:
            continue
        try:
            signal.signal(s, _on_signal)
        except Exception:  # noqa: BLE001
            pass


def run_step(name, args, timeout_s):
    """수집기를 «별도 프로세스»로 실행한다(노트북 schtasks 와 같은 실행 형태).
    출력은 상속 — Render 로그에 그대로 남는다. 반환: 종료코드(타임아웃=124)."""
    cmd = [sys.executable, "-u", *args]
    log(f"{name} 시작: {' '.join(args)}")
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=str(ROOT))
    _child["p"] = p
    try:
        rc = p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log(f"{name} 하드 타임아웃 {timeout_s}s — SIGTERM 후 최대 120s 대기")
        p.terminate()
        try:
            p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        rc = 124
    finally:
        _child["p"] = None
    log(f"{name} 종료 rc={rc} ({time.time() - t0:.0f}s)")
    return rc


# ----------------------------------------------------------------- 읽기 전용 점검
def probe():
    """Render 데이터센터 IP 에서 외부 소스가 «실제로» 통하는지 본다. 쓰기·텔레그램 0.

    요청 수: 네이버 1 · 한경 1 · 네이버 fchart(FDR) 1 · Supabase 읽기 1 = 총 4회.
    수집기와 «같은 함수·같은 헤더»를 쓴다(따로 흉내 내면 통과해도 의미가 없다)."""
    res = {"where": _where(), "at": datetime.now(KST).isoformat(timespec="seconds")}

    def _err(e):
        return f"{type(e).__name__}: {str(e)[:200]}"

    # 1) 네이버 시총 랭킹 (top100_collect 와 동일 호출)
    try:
        import top100_collect as t1
        rows = t1._fetch_page("KOSPI", 1)
        stock_rows = [r for r in rows
                      if str(r.get("stockEndType") or "").lower() == "stock"
                      and r.get("marketValueRaw") not in (None, "")]
        res["naver_marketValue"] = {"ok": len(stock_rows) >= 50, "rows": len(rows),
                                    "stock_rows": len(stock_rows)}
    except Exception as e:  # noqa: BLE001
        res["naver_marketValue"] = {"ok": False, "err": _err(e)}
    time.sleep(1.2)

    # 2) 한경 컨센서스 (analyst_collect 양성대조와 동일: 005930 코드검색 1회)
    try:
        import analyst_collect as ac
        reps, st = ac.fetch_reports(ac._CANARY_CODE, ac._CANARY_CODE, with_status=True)
        res["hankyung"] = {"ok": st == ac.REPORTS_OK and len(reps) >= ac._CANARY_MIN,
                           "status": st, "reports_005930": len(reps)}
    except Exception as e:  # noqa: BLE001
        res["hankyung"] = {"ok": False, "err": _err(e)}
    time.sleep(1.2)

    # 3) 네이버 fchart (FinanceDataReader → NaverDailyReader, analyst 가격 본줄기)
    try:
        import FinanceDataReader as fdr
        end = datetime.now(KST).date()
        df = fdr.DataReader("005930", (end - timedelta(days=20)).isoformat(), end.isoformat())
        n = 0 if df is None else len(df)
        last = str(df.index[-1])[:10] if n else None
        res["naver_fchart_fdr"] = {"ok": n >= 3 and "Close" in df, "rows": n, "last": last}
    except Exception as e:  # noqa: BLE001
        res["naver_fchart_fdr"] = {"ok": False, "err": _err(e)}

    # 4) Supabase 읽기 (쓰기 권한은 «쓰지 않고는» 확인 불가 — 키 존재만 본다)
    miss = env_missing()
    try:
        ok, rows = mc.select_columns("market_cap_top100", "code,updated_at", limit=1)
        res["supabase_read"] = {"ok": bool(ok and rows) and not miss, "missing_env": miss,
                                "sample_updated_at": (rows[0].get("updated_at") if rows else None)}
    except Exception as e:  # noqa: BLE001
        res["supabase_read"] = {"ok": False, "err": _err(e), "missing_env": miss}

    # 5) 경보 목적지 해석만 (발송하지 않는다)
    res["telegram_pair"] = {"ok": bool(config.TELEGRAM_TOKEN and config.TEST_CHAT_ID),
                            "source": config.TELEGRAM_SOURCE or None,
                            "tried": list(config.TELEGRAM_TRIED)}

    required = ("naver_marketValue", "hankyung", "naver_fchart_fdr", "supabase_read")
    passed = all(res.get(k, {}).get("ok") for k in required)
    res["PASS"] = passed
    print("[weekly][PROBE] " + json.dumps(res, ensure_ascii=False), flush=True)
    return 0 if passed else 4


# ----------------------------------------------------------------- 본 실행
def run(force=False, skip_top100=False, skip_analyst=False, notify_success=True):
    day = today_kst()
    where = _where()
    log(f"시작 day(KST)={day} where={where} force={force}")

    miss = env_missing()
    if miss:
        msg = (f"[MIRI 주간배치 실패] {day} {where}\n환경변수 누락 {miss} — 수집을 시작하지 않았다.\n"
               "(키 없이 돌면 로컬 파일에만 쓰고 rc=0 으로 끝나 «성공처럼» 보인다)")
        log(msg.replace("\n", " | "))
        notify(msg)
        return 2

    _install_signals()
    lines, failed = [], []

    # ---- 1) top100 ----
    ok_t, rows = top100_rows()
    pre_t = count_today(rows, day) if ok_t else 0
    if not ok_t:
        log("top100 사전 조회 실패(Supabase 읽기) — 가드 없이 실행한다")
    if skip_top100:
        lines.append("top100 건너뜀(--skip-top100)")
    elif pre_t >= TOP100_MIN_ROWS and not force:
        lines.append(f"top100 건너뜀: 오늘자 {pre_t}행 이미 있음(이중 실행 방지)")
        log(lines[-1])
    else:
        rc = run_step("top100", ["top100_collect.py"], TOP100_TIMEOUT_S)
        ok_t, rows = top100_rows()
        n_t = count_today(rows, day) if ok_t else 0
        if not ok_t:
            failed.append(f"top100 rc={rc} · Supabase 재확인 «읽기 실패»(반영 여부 불명)")
        elif n_t >= TOP100_MIN_ROWS:
            tag = " ※실행 전부터 오늘자 — 이번 실행분인지 구분 불가" if pre_t >= TOP100_MIN_ROWS else ""
            lines.append(f"top100 rc={rc} · 오늘자 {n_t}행{tag}")
        else:
            failed.append(f"top100 rc={rc} · Supabase 오늘자 {n_t}행 < {TOP100_MIN_ROWS}")

    # ---- analyst 의 입력 보장 ----
    how = ensure_top100_json(rows if ok_t else [])
    log(f"data/top100.json: {how}")
    codes = [str(r.get("code")) for r in (rows or []) if isinstance(r, dict) and r.get("code")]
    if how == "none" and not codes:
        failed.append("analyst 입력(top100) 없음 — analyst 미실행")
        skip_analyst = True

    # ---- 2) analyst ----
    if _stop["hit"]:
        failed.append(f"중단 신호(sig={_stop['sig']}) — analyst 미실행")
    elif skip_analyst:
        if not any(s.startswith("analyst 입력") for s in failed):
            lines.append("analyst 건너뜀(--skip-analyst)")
    else:
        ok_a, dates = analyst_dates()
        pre_ratio, pre_hit = cover_today(codes, dates, day) if ok_a else (0.0, 0)
        if ok_a and pre_ratio >= ANALYST_SKIP_COVER and not force:
            lines.append(f"analyst 건너뜀: 오늘자 {pre_hit}/{len(codes)} 이미 있음(이중 실행 방지)")
            log(lines[-1])
        else:
            rc = run_step("analyst",
                          ["analyst_collect.py", "--limit", "0", "--sleep", ANALYST_SLEEP,
                           "--deadline-min", str(ANALYST_DEADLINE_MIN)],
                          ANALYST_TIMEOUT_S)
            ok_a, dates = analyst_dates()
            ratio, hit = cover_today(codes, dates, day) if ok_a else (0.0, 0)
            if not ok_a:
                failed.append(f"analyst rc={rc} · Supabase 재확인 «읽기 실패»(반영 여부 불명)")
            elif ratio >= ANALYST_OK_COVER:
                tag = (" ※실행 전부터 오늘자 — 이번 실행분인지 구분 불가"
                       if pre_ratio >= ANALYST_SKIP_COVER else "")
                lines.append(f"analyst rc={rc} · 커버 {hit}/{len(codes)}{tag}")
            else:
                failed.append(f"analyst rc={rc} · 커버 {hit}/{len(codes)} "
                              f"< {ANALYST_OK_COVER:.0%}")

    if failed:
        msg = (f"[MIRI 주간배치 실패] {day} {where}\n" + "\n".join(failed + lines)
               + "\n라이브 API 는 직전 스냅샷을 계속 서빙한다(덮어쓰지 않음)")
        log(msg.replace("\n", " | "))
        notify(msg)
        return 1
    msg = f"[MIRI 주간배치] {day} {where} 완료\n" + "\n".join(lines)
    log(msg.replace("\n", " | "))
    if notify_success:
        notify(msg)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="MIRI 주간 배치(top100 → analyst) — Render Cron 진입점")
    ap.add_argument("--probe", action="store_true",
                    help="읽기 전용 소스 도달성 점검(네이버·한경·fchart·Supabase 읽기). 쓰기·텔레그램 0")
    ap.add_argument("--force", action="store_true", help="「오늘 이미 갱신」 가드 무시")
    ap.add_argument("--skip-top100", action="store_true")
    ap.add_argument("--skip-analyst", action="store_true")
    ap.add_argument("--no-notify-success", action="store_true",
                    help="성공 시 운영자 DM 생략(실패 경보는 항상 보낸다)")
    a = ap.parse_args(argv)
    if a.probe:
        return probe()
    return run(force=a.force, skip_top100=a.skip_top100, skip_analyst=a.skip_analyst,
               notify_success=not a.no_notify_success)


if __name__ == "__main__":
    raise SystemExit(main())

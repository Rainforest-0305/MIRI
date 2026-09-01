# -*- coding: utf-8 -*-
"""소스1 파생: 종목별 '다음 예상 발표일' 추정(데이터 기반).

방법(우선순위):
  1) 'season_lag'  — 과거 원본(정정 제외) 정기보고서의 결산기말(fy.fm) 대비
     실제 접수일(rcept_dt) 지연일수(lag) 의 계절성을 사용. 종목이 이미 제출한
     가장 최근 회계기간의 '다음 분기 슬롯'을 정하고, 그 슬롯 리포트 종류의
     과거 median lag 를 결산기말에 더해 예측한다.
  2) 'yoy'         — 같은 슬롯(같은 fm)의 전년 접수일 + 연간주기(median) 로 교차검증.
  3) 'statutory'   — 이력/괄호 파싱이 부족할 때 한국 법정기한 폴백
     (사업보고서 결산후 90일, 분기·반기 45일). 신뢰도 low.

각 예측 레코드에 method(방식) 와 confidence(신뢰도) 라벨을 남긴다.
DART 콜 없음(순수 계산).

--- 2026-09-01 보강 2건 ---
[1] 「12월 결산」 가정 제거. 기존 FM_TO_TYPE/_NEXT_SLOT 은 fm∈{3,6,9,12} 만 알아서,
    11월결산(현대약품)·8월결산(풍강)·2월결산(JTC)·리츠(반기·분기결산) 종목의 다음
    슬롯을 통째로 틀렸다(실측: 15종목이 2027-05 로 1년 가까이 밀려 있었다).
    → 「종목 자신의 이력」에서 fm→보고서종류 를 학습하고 다음 슬롯 = +3개월 로 잡는다.
      12월결산이면 학습결과가 FM_TO_TYPE 과 완전히 동일해 기존 경로는 안 바뀐다.
    ★리츠 실측 예: SK리츠는 fm 3·6·9·12 «전부 사업보고서»(분기결산), ESR켄달스퀘어리츠는
      fm 5·11 사업 / 2·8 반기(반기결산). 고정표로는 표현 자체가 불가능하다.
[2] 법정기한 sanity 필터(is_sane). 「미래 날짜」라는 이유만으로 살아남던 오예측을 버린다.
    실사고: 반기보고서(2026.06)를 2026-10-05·10-31 로 예측 — 법정기한 2026-08-14 을
    52·78일 넘긴 값이 그대로 캘린더에 남아 있었다.
"""
import os
import sys
import calendar
import statistics
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ★«패키지 경로»로만 import 한다. 절대 `import collect` (맨이름) 하지 않는다.
#   [실사고 2026-09-01] 이 파일이 sys.path 에 자기 폴더를 넣고 `import collect` 를 하면
#   sys.modules["collect"] 를 선점한다. 그런데 features/mezzanine_calendar 에도 collect.py
#   가 있고 app.py:_build_mezzanine_payload 는 «맨이름»으로 그걸 불러온다.
#   → 실적발표 배치를 app.py 에 배선하자마자 /api/mezzanine 이 503 으로 죽었다
#     ("module 'collect' has no attribute 'collect_all'"). 맨이름 등록을 없애 차단한다.
from features.earnings_calendar import collect  # noqa: E402

# 회계기말(fm) → 리포트 종류. 12월결산 «폴백»(종목 이력으로 학습 실패 시에만 쓴다).
FM_TO_TYPE = {3: "분기보고서", 6: "반기보고서", 9: "분기보고서", 12: "사업보고서"}
# 법정 제출기한(결산기말 이후 일수). 폴백용.
STATUTORY_LAG = {"사업보고서": 90, "반기보고서": 45, "분기보고서": 45}

# ---- sanity 상한: 이 날짜를 넘는 예측은 폐기한다 ----
# 기준선은 법정기한(STATUTORY_LAG)이고, 여기에 여유(_SANITY_EXT)를 더한 뒤 주말은
# 익영업일로 이월한다. 여유가 필요한 이유 2가지:
#   (a) 법정기한이 토·일·공휴일이면 익영업일로 밀린다(공휴일 달력이 없어 주말만 이월).
#   (b) 외국법인·특례 종목은 실제로 더 늦게 접수한다(실측 lag 60~61일 정상 접수 다수).
# ★여유값은 «실측으로» 정했다 — bench_cache 정기보고서 52,399건(정정 제외)에 대해
#   이 상한 안에 들어오는 비율:
#      여유 +0  : 사업 98.18% · 반기 78.38% · 분기 92.70%   (정상 접수를 대량 오폐기)
#      여유 +15 : 사업 99.45% · 반기 99.83% · 분기 99.95%   ← 채택
#      여유 +30 : 사업 99.60% · 반기 99.96% · 분기 99.97%   (오예측까지 통과시킴)
#   +15 에서 곡선이 꺾인다. 그 밖(0.05~0.55%)은 지연공시(관리종목 사유)라 예측 대상이 아니다.
_SANITY_EXT = {"사업보고서": 30, "반기보고서": 15, "분기보고서": 15}


def _to_date(yyyymmdd):
    try:
        return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))
    except (ValueError, TypeError, IndexError):
        return None


def _period_end(fy, fm):
    """회계기말의 말일 date."""
    last = calendar.monthrange(fy, fm)[1]
    return date(fy, fm, last)


def _next_business_day(d):
    """토·일이면 다음 월요일로 이월(공휴일 달력 없음 — 그 오차는 _SANITY_EXT 가 흡수)."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def sanity_deadline(fy, fm, report_type):
    """해당 보고서 유형의 «허용 상한일». 이 날짜를 넘는 예측은 오예측으로 폐기한다."""
    lag = STATUTORY_LAG.get(report_type)
    if lag is None:
        return None
    return _next_business_day(
        _period_end(fy, fm) + timedelta(days=lag + _SANITY_EXT.get(report_type, 15)))


def is_sane(pred):
    """예측 레코드가 법정기한 sanity 를 통과하는가. (ok, deadline_iso|None)"""
    d = _to_date((pred.get("predicted_date") or "").replace("-", ""))
    try:
        fy, fm = (int(x) for x in (pred.get("target_period") or "").split("."))
    except ValueError:
        return True, None            # 판정 불가는 통과(있는 정보로만 거른다)
    dl = sanity_deadline(fy, fm, pred.get("target_type"))
    if d is None or dl is None:
        return True, (dl.isoformat() if dl else None)
    return (d <= dl), dl.isoformat()


def _next_fm(fm):
    """다음 정기보고 슬롯의 회계기말 월(+3개월)과 연도증가 여부."""
    n = fm + 3
    return (n - 12, 1) if n > 12 else (n, 0)


def predict_corp(rows):
    """단일 종목(그룹 rows: rcept_dt 오름차순) → 예측 레코드 dict 또는 None.

    rows 는 collect.group_by_corp 의 한 그룹(정기보고서만, amend 포함).
    """
    if not rows:
        return None
    meta = rows[-1]  # 최신 레코드를 종목 대표 메타로
    # 원본(정정 제외) + 괄호 파싱 성공분만 계절성 계산에 사용
    orig = [r for r in rows
            if not r.get("amend") and r.get("fy") and r.get("fm")
            and _to_date(r.get("rcept_dt"))]

    base = {
        "corp_code": meta.get("corp_code", ""),
        "corp_name": meta.get("corp_name", ""),
        "stock_code": meta.get("stock_code", ""),
        "market": meta.get("market", ""),
        "history_count": len(rows),
        "orig_count": len(orig),
    }

    if not orig:
        # 이력 없음 → 예측 불가(메타만). 호출측에서 스킵 카운트.
        base.update({"predicted_date": None, "method": "no_history",
                     "confidence": "none", "basis": "괄호 파싱 가능한 원본 이력 없음"})
        return base

    # 슬롯별(report_type, fm) lag 히스토리 + 종목 «자신의» 회계달력(fm→보고서종류)
    lags_by_type = {}   # report_type -> [lag_days...]
    by_slot = {}        # fm -> [(fy, rcept_date)...]
    type_votes = {}     # fm -> {report_type: 표수}   ← 결산월 학습(12월결산 가정 제거)
    for r in orig:
        rd = _to_date(r["rcept_dt"])
        pe = _period_end(r["fy"], r["fm"])
        lag = (rd - pe).days
        if -5 <= lag <= 200:  # 비정상치(정정 잔재 등) 배제
            lags_by_type.setdefault(r["report_type"], []).append(lag)
        by_slot.setdefault(r["fm"], []).append((r["fy"], rd))
        v = type_votes.setdefault(r["fm"], {})
        v[r["report_type"]] = v.get(r["report_type"], 0) + 1

    # 이미 제출한 가장 최근 회계기간(fy, fm) → 다음 슬롯 = +3개월
    last_fy, last_fm = max((r["fy"], r["fm"]) for r in orig)
    nfm, dy = _next_fm(last_fm)
    target_fy, target_fm = last_fy + dy, nfm
    target_pe = _period_end(target_fy, target_fm)
    # 목표 슬롯의 보고서 종류: 그 종목이 «그 회계월에 실제로 낸» 종류의 최빈값.
    # 이력이 없으면 12월결산 표(FM_TO_TYPE)로 폴백, 그것도 없으면 분기보고서.
    votes = type_votes.get(target_fm)
    if votes:
        target_type = max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
        fiscal_src = "history"
    else:
        target_type = FM_TO_TYPE.get(target_fm, "분기보고서")
        fiscal_src = "fallback_dec"

    # ---- 방법1: season_lag ----
    lags = lags_by_type.get(target_type, [])
    method = None
    confidence = "low"
    predicted = None
    basis = ""
    lag_used = None
    if lags:
        lag_used = int(round(statistics.median(lags)))
        predicted = target_pe + timedelta(days=lag_used)
        method = "season_lag"
        n = len(lags)
        std = statistics.pstdev(lags) if n >= 2 else 999
        if n >= 3 and std <= 7:
            confidence = "high"
        elif n >= 2 and std <= 15:
            confidence = "medium"
        else:
            confidence = "low"
        basis = (f"{target_type} 과거 {n}건의 결산기말 대비 접수지연 "
                 f"median={lag_used}일(σ={std:.1f}) 를 {target_fy}.{target_fm:02d} "
                 f"결산기말({target_pe.isoformat()})에 가산")

    # ---- 방법2: yoy 교차검증(같은 fm 전년 접수일 + 연주기) ----
    yoy_pred = None
    slot_hist = sorted(by_slot.get(target_fm, []), key=lambda x: x[0])
    if len(slot_hist) >= 1:
        prev_fy, prev_rd = slot_hist[-1]
        # 연주기 median (같은 슬롯 연속 접수일 간격)
        if len(slot_hist) >= 2:
            deltas = [(slot_hist[i][1] - slot_hist[i - 1][1]).days
                      for i in range(1, len(slot_hist))]
            yearly = int(round(statistics.median(deltas)))
        else:
            yearly = 365
        # 전년 대비 목표연도까지 반복 가산
        step = max(1, target_fy - prev_fy)
        yoy_pred = prev_rd + timedelta(days=yearly * step)

    if predicted is None and yoy_pred is not None:
        predicted = yoy_pred
        method = "yoy"
        confidence = "medium" if len(slot_hist) >= 2 else "low"
        basis = (f"{target_type}({target_fm:02d}월기) 전년 접수일 {prev_rd.isoformat()} "
                 f"+ 연주기 {yearly}일×{step}")

    # ---- 방법3: statutory 폴백 ----
    if predicted is None:
        lag_used = STATUTORY_LAG[target_type]
        predicted = target_pe + timedelta(days=lag_used)
        method = "statutory"
        confidence = "low"
        basis = (f"이력 부족 → 법정기한 폴백: {target_type} 결산기말"
                 f"({target_pe.isoformat()}) + {lag_used}일")

    # ---- 주말 이월 ----
    # [실측 2026-09-01] bench_cache 정기보고서 62,065건의 접수요일: 월 22.2% · 화 16.8% ·
    # 수 13.5% · 목 23.5% · 금 23.9% — «토·일 0건(0.000%)». DART 는 주말 접수를 받지 않는다.
    # 그런데 median lag 를 그대로 더하면 예측이 주말에 떨어진다(보정 전 실측: 11월 웨이브
    # 2,595건 중 2,005건이 토·일). 캘린더에 토요일 실적발표 2천 건이 뜨는 상태였다.
    # → 익영업일로 민다. 공휴일 달력이 없어 주말만 처리한다(그 오차는 ±1~2영업일).
    predicted_raw = predicted
    predicted = _next_business_day(predicted)

    base.update({
        "target_period": f"{target_fy}.{target_fm:02d}",
        "target_type": target_type,
        "period_end": target_pe.isoformat(),
        "predicted_date": predicted.isoformat(),
        "predicted_date_raw": predicted_raw.isoformat(),
        "predicted_lag_days": lag_used,
        "yoy_crosscheck": yoy_pred.isoformat() if yoy_pred else None,
        "method": method,
        "confidence": confidence,
        "basis": basis,
        "fiscal_src": fiscal_src,
        "last_filed_period": f"{last_fy}.{last_fm:02d}",
        "last_filed_rcept": max(orig, key=lambda r: r["rcept_dt"])["rcept_dt"],
    })
    dl = sanity_deadline(target_fy, target_fm, target_type)
    base["statutory_deadline"] = dl.isoformat() if dl else None
    return base


def predict_all(groups, sanity=True):
    """그룹 dict → (예측 list, 스킵수, sanity 폐기 list).

    sanity=True 면 법정기한을 넘긴 예측을 «버린다». 「미래 날짜」라는 이유만으로
    오예측이 살아남던 구멍을 막는 것이 목적이다(폐기분은 3번째 반환값으로 노출 —
    조용히 사라지면 다음에 또 못 본다).
    """
    preds = []
    skipped = 0
    dropped = []
    for key, rows in groups.items():
        p = predict_corp(rows)
        if not (p and p.get("predicted_date")):
            skipped += 1
            continue
        if sanity:
            ok, dl = is_sane(p)
            if not ok:
                p["dropped_reason"] = (
                    f"법정기한 초과: 예측 {p['predicted_date']} > 상한 {dl} "
                    f"({p.get('target_type')} {p.get('target_period')})")
                dropped.append(p)
                continue
        preds.append(p)
    # 예상 발표일 오름차순
    preds.sort(key=lambda x: x["predicted_date"])
    dropped.sort(key=lambda x: x["predicted_date"])
    return preds, skipped, dropped


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    recs, stats = collect.load_all()
    g = collect.group_by_corp(recs)
    preds, skipped, dropped = predict_all(g)
    print("stats:", stats, "corps:", len(g),
          "preds:", len(preds), "skipped:", skipped, "dropped:", len(dropped))
    for p in preds[:3]:
        print(p["stock_code"], p["corp_name"], p["predicted_date"],
              p["method"], p["confidence"])

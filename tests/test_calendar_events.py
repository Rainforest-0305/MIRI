# -*- coding: utf-8 -*-
"""features/calendar_events 단위·통합 테스트(외부 호출 0).

실행: python -m pytest tests/test_calendar_events.py -q
원문 텍스트 픽스처는 2026-08-29 DART 실공시(태그 제거)에서 가져온 «실제 형식»이다.
"""
import datetime as dt
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, ROOT)
os.environ["GONGSI_CAL_EVENTS_BATCH"] = "0"      # 앱 import 시 배치 스레드 금지

from features.calendar_events import dart_src, build  # noqa: E402

# 스튜디오삼익 [기재정정]현금ㆍ현물배당결정(분기배당) 20260824900610 — 실형식
TXT_DECISION = ("현금ㆍ현물배당 결정 1. 배당구분 분기배당 2. 배당종류 현금배당 - 현물자산의 상세내역 - "
                "3. 1주당 배당금(원) 보통주식 50 종류주식 - - 차등배당 여부 미해당 4. 시가배당률(%) 보통주식 2.7 종류주식 - "
                "5. 배당금총액(원) 555,734,350 6. 배당기준일 2026-08-14 7. 배당금지급 예정일자 2026-08-31 8. 승인기관 이사회 "
                "9. 주주총회 예정일자 - 10. 이사회결의일(결정일) 2026-07-28")
# 아세아텍 현금ㆍ현물배당결정 20260828900940 — 지급일 미정(주총 후)
TXT_DECISION_NOPAY = ("1. 배당구분 결산배당 2. 배당종류 현금배당 3. 1주당 배당금(원) 보통주식 50 종류주식 - "
                      "4. 시가배당률(%) 보통주식 2.7 종류주식 - 5. 배당금총액(원) 909,437,800 6. 배당기준일 2026-06-30 "
                      "7. 배당금지급 예정일자 - 8. 승인기관 주주총회 9. 주주총회 예정일자 2026-09-28 10. 이사회결의일(결정일) 2026-08-28")
# LG 현금ㆍ현물배당을위한주주명부폐쇄(기준일)결정 20260827800293
TXT_CLOSURE = ("현금ㆍ현물배당을 위한 주주명부폐쇄(기준일) 결정 1. 배당구분 중간배당 2. 주주명부폐쇄(기준일) 시작일 - 종료일 - "
               "기준일 2026-09-11 3. 주주명부폐쇄(기준일) 목적 반기배당 확정 4. 이사회결의일(결정일) 2026-08-27")
# 이리츠코크렙 부동산투자회사금전배당결정 20260828800397
TXT_REIT = ("부동산투자회사 금전배당 결정 1. 배당총액(원) 11,006,349,578 2. 1주당 배당금(원) 보통주식 173 종류주식 - "
            "3. 시가배당률(%) 보통주식 5.0 종류주식 - 4. 배당기준일 2026-06-30 5. 배당금지급 예정일 정기주주총회일로부터 1개월 이내 "
            "6. 주주총회 예정일자 2026-09-16 7. 이사회결의일(결정일) 2026-08-28")


def test_parse_decision_full():
    p = dart_src.parse_dividend_text(TXT_DECISION)
    assert p["kind"] == "분기배당" and p["div_type"] == "현금배당"
    assert p["per_share"] == 50 and p["yield_pct"] == 2.7
    assert p["record_date"] == "2026-08-14" and p["pay_date"] == "2026-08-31"
    assert p["total_amount"] == 555734350 and p["agm_date"] is None


def test_parse_decision_pay_pending():
    p = dart_src.parse_dividend_text(TXT_DECISION_NOPAY)
    assert p["record_date"] == "2026-06-30" and p["pay_date"] is None
    assert p["agm_date"] == "2026-09-28" and p["kind"] == "결산배당"


def test_parse_closure_record_only():
    p = dart_src.parse_dividend_text(TXT_CLOSURE)
    assert p["record_date"] == "2026-09-11" and p["per_share"] is None and p["kind"] == "중간배당"


def test_parse_reit_pay_note():
    p = dart_src.parse_dividend_text(TXT_REIT)
    assert p["per_share"] == 173 and p["pay_date"] is None
    assert p["pay_note"] and "1개월" in p["pay_note"]


def test_filing_filters():
    assert dart_src.is_dividend_filing("현금ㆍ현물배당결정              (분기배당)")
    assert dart_src.is_dividend_filing("현금ㆍ현물배당을위한주주명부폐쇄(기준일)결정")
    assert not dart_src.is_dividend_filing("현금ㆍ현물배당결정(자회사의 주요경영사항)")
    assert not dart_src.is_dividend_filing("배당락")
    assert dart_src.is_ipo_filing({"report_nm": "[발행조건확정]증권신고서(지분증권)", "stock_code": ""})
    assert not dart_src.is_ipo_filing({"report_nm": "증권신고서(지분증권)", "stock_code": "207940"})


def test_stock_code_alnum():
    assert dart_src.CODE_RE.match("0126Z0") and dart_src.CODE_RE.match("005930")
    assert not dart_src.CODE_RE.match("12345") and not dart_src.CODE_RE.match("００５９３０")


def test_kdate_range():
    assert dart_src.parse_kdate_range("2026년 09월 17일 ~ 2026년 09월 18일") == ("2026-09-17", "2026-09-18")
    assert dart_src.parse_kdate_range("2026년 09월 22일") == ("2026-09-22", "2026-09-22")
    assert dart_src.parse_kdate_range("-") == (None, None)


def _rec(rcept, code, record, per_share=None, pay=None, closure=False):
    return {"rcept_no": rcept, "stock_code": code, "corp_name": "X", "record_date": record, "per_share": per_share,
            "pay_date": pay, "is_closure": closure, "kind": None, "yield_pct": None, "pay_note": None,
            "agm_date": None, "market": "KOSPI", "rcept_dt": rcept[:8]}


def test_merge_closure_and_decision_same_key():
    f = {"a": _rec("20260827800293", "003550", "2026-09-11", closure=True),
         "b": _rec("20260827800295", "003550", "2026-09-11", per_share=1500, pay="2026-10-05")}
    m = dart_src.merge_dividends(f)
    assert len(m) == 1 and m[0]["per_share"] == 1500 and m[0]["rcept_no"] == "20260827800295"


def test_merge_correction_wins_and_backfills():
    f = {"a": _rec("20260820000001", "000660", "2026-08-31", per_share=300, pay="2026-09-20"),
         "b": _rec("20260825000002", "000660", "2026-08-31", per_share=375)}   # 정정: 지급일 칸이 비어 옴
    m = dart_src.merge_dividends(f)
    assert m[0]["per_share"] == 375 and m[0]["pay_date"] == "2026-09-20"


def test_build_events_and_api(tmp_path, monkeypatch):
    cache = {
        "version": 1,
        "filings": {"a": _rec("20260827800295", "003550", "2026-09-11", per_share=1500, pay="2026-10-05"),
                    "b": _rec("20260828900940", "050860", "2026-06-30", per_share=50)},   # 전달 이전 → 제외
        "ipo_filings": {"C1": {"rcept_no": "20260825000417", "rcept_dt": "20260825", "corp_code": "C1", "corp_name": "스카이랩스",
                               "sub_start": "2026-08-26", "sub_end": "2026-08-27", "pay_date": "2026-08-31", "price": 13000,
                               "is_spac": False, "price_final": True}},
        "toss_scheduled": [{"symbol": "386380", "name": "스카이랩스", "securityType": "STOCK", "market": "KOSDAQ",
                            "status": "SCHEDULED", "listDate": "2026-09-04"},
                           {"symbol": "0ABCD0", "name": "TEST ETF", "securityType": "ETF", "market": "KOSPI",
                            "status": "SCHEDULED", "listDate": "2026-09-08"}],
        "toss_scheduled_log": [], "sources": {"dart": {"ok": True}, "toss": {"ok": True, "etf": 1}},
    }
    today = dt.date(2026, 8, 29)
    evs = build.build_events(cache, today)
    types = sorted((e["type"], e["subtype"], e["date"], e["stock_code"]) for e in evs)
    assert ("dividend", "record", "2026-09-11", "003550") in types
    assert ("dividend", "payment", "2026-10-05", "003550") in types
    assert not any(e["date"] == "2026-06-30" for e in evs)                 # 전달 1일 이전 컷
    assert ("ipo", "subscription", "2026-08-26", "386380") in types        # 토스 심볼 병합
    assert ("ipo", "listing", "2026-09-04", "386380") in types             # 토스 listDate → 상장예정일
    assert ("etf_listing", "listing", "2026-09-08", "0ABCD0") in types
    div = next(e for e in evs if e["type"] == "dividend")
    assert div["dividend"]["per_share"] == 1500 and div["label"] == "배당기준일"
    assert all(e["label"] for e in evs)

    # API — 캐시 파일만 읽는다(외부 호출 0). 존재하지 않는 달은 200 + 빈-정형.
    cache["events"] = evs
    cache["as_of"] = today.isoformat()
    monkeypatch.setattr(build, "CACHE_FILE", tmp_path / "calendar_events.json")
    build.save_cache(cache)
    from features.calendar_events import api as capi
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(capi.router)
    c = TestClient(app)
    r = c.get("/api/calendar/events?ym=2026-09")
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == len([e for e in evs if e["date"].startswith("2026-09")])
    assert j["counts_by_type"]["etf_listing"] == 1 and "2026-10" in j["months"]
    assert j["sources"]["toss"]["etf"] == 1
    r2 = c.get("/api/calendar/events?ym=2031-01")
    assert r2.status_code == 200 and r2.json()["count"] == 0 and r2.json()["items"] == []
    r3 = c.get("/api/calendar/events?ym=bad")
    assert r3.status_code == 200 and r3.json()["ym"] is None
    r4 = c.get("/api/calendar/events", headers={"If-None-Match": r.headers["etag"]})
    assert r4.status_code in (200, 304)


def test_run_keeps_cache_on_dart_failure(tmp_path, monkeypatch):
    """DART 가 죽으면 events 를 «빈 값으로 덮지 않는다» + 경보 호출."""
    prior = {"version": 1, "filings": {}, "ipo_filings": {}, "toss_scheduled": [], "toss_scheduled_log": [],
             "events": [{"id": "x", "type": "dividend", "subtype": "record", "label": "배당기준일", "date": "2026-09-30",
                         "corp_name": "P", "stock_code": "000001", "market": None}],
             "sources": {"dart": {"ok": True, "last_list_date": "2026-08-28"}}}
    monkeypatch.setattr(build, "CACHE_FILE", tmp_path / "calendar_events.json")
    build.save_cache(prior)
    alerts = []
    monkeypatch.setattr(build, "_alert", lambda t: alerts.append(t))

    def boom(*a, **k):
        raise RuntimeError("list.json status=020 한도초과")
    monkeypatch.setattr(dart_src, "collect_dividend_filings", boom)
    s = build.run(days=3, use_toss=False, today=dt.date(2026, 8, 29))
    assert s["ok"] is False and alerts and "DART" in alerts[0]
    after = build.load_cache()
    assert after["events"] == prior["events"]
    assert after["sources"]["dart"]["ok"] is False and after["sources"]["dart"]["last_list_date"] == "2026-08-28"

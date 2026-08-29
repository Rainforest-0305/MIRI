# -*- coding: utf-8 -*-
"""DART 소스: 배당(거래소공시 I + 원문) · 주식 IPO(발행공시 C + estkRs).

콜 예산(일 1회 증분 기준, 실측 2026-08-29 30일치로 산출):
  list.json  I  ≈ 140건/일 → 2페이지/일 · C ≈ 70건/일 → 1페이지/일
  document.xml  배당 신규 rcept_no 당 1콜(≈6건/일, HTML 평균 7.7KB·최대 15KB)
  estkRs.json   신규 IPO 법인당 1콜(≈0.5건/일)
★list.json 은 «일 단위 창»으로 돈다 — 30일 한 창은 4,249건 > 100×40 페이지 상한(실측).
원문 캐시는 features/mezzanine_calendar/dart_doc.py 재사용(bench_cache/caldiv/doc).
"""
import datetime as dt
import re
import sys
import time

import requests

try:
    import config
except ImportError:  # pragma: no cover
    import os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)))
    import config

from features.mezzanine_calendar import dart_doc

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
ESTK_URL = "https://opendart.fss.or.kr/api/estkRs.json"
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")   # ★숫자 6자리 가정 금지 — 0126Z0 같은 영문 포함 코드 실재
DOC_KIND = "caldiv"

CALLS = {"list": 0, "doc": 0, "estk": 0}


def _log(msg):
    print(f"[cal-events][dart] {msg}")


def _key():
    k = getattr(config, "DART_API_KEY", "") or ""
    if not k:
        raise RuntimeError("DART_API_KEY missing")
    return k


# ---------------------------------------------------------------- list.json
def list_day(day: str, pblntf_ty: str, page_cap: int = 10, session=None):
    """하루치 공시 목록(전 페이지). day='YYYYMMDD'. 실패는 예외(호출측이 배치 실패로 집계)."""
    s = session or requests
    out = []
    for p in range(1, page_cap + 1):
        CALLS["list"] += 1
        r = s.get(LIST_URL, params={"crtfc_key": _key(), "bgn_de": day, "end_de": day,
                                    "pblntf_ty": pblntf_ty, "page_no": p, "page_count": 100},
                  timeout=30)
        r.raise_for_status()
        j = r.json()
        st = j.get("status")
        if st == "013":          # 조회된 데이터 없음(주말·휴일) = 정상 0건
            return out
        if st != "000":
            raise RuntimeError(f"list.json status={st} {j.get('message')}")
        out += j.get("list", [])
        if p >= int(j.get("total_page", 1) or 1):
            break
    return out


def list_range(bgn: dt.date, end: dt.date, pblntf_ty: str, session=None):
    """[bgn, end] 일 단위 창 순회."""
    out = []
    d = bgn
    while d <= end:
        if d.weekday() < 5:  # 주말은 DART 접수가 0 — 콜 절약
            out += list_day(d.strftime("%Y%m%d"), pblntf_ty, session=session)
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- 배당 원문 파싱
def strip_html(h: str) -> str:
    t = re.sub(r"<style.*?</style>", " ", h, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    return re.sub(r"\s+", " ", t)


_DATE = r"(\d{4}-\d{2}-\d{2})"


def _num(s):
    if not s:
        return None
    s = s.replace(",", "").strip()
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def parse_dividend_text(t: str) -> dict:
    """배당결정/주주명부폐쇄 원문(태그 제거 텍스트) → 필드. 없는 값은 None.
    형식 근거 [실측 2026-08-29]:
      '1. 배당구분 분기배당 2. 배당종류 현금배당 ... 3. 1주당 배당금(원) 보통주식 50 종류주식 -
       4. 시가배당률(%) 보통주식 2.7 ... 6. 배당기준일 2026-08-14 7. 배당금지급 예정일자 2026-08-31
       ... 9. 주주총회 예정일자 -'
      폐쇄: '2. 주주명부폐쇄(기준일) 시작일 - 종료일 - 기준일 2026-09-11'
      리츠: '4. 배당기준일 2026-06-30 5. 배당금지급 예정일 정기주주총회일로부터 1개월 이내'
    """
    m_kind = re.search(r"배당구분\s*([가-힣]{2,3}배당)", t)
    m_type = re.search(r"배당종류\s*(현금배당|현물배당|현금\s*및\s*현물배당)", t)
    m_ps = re.search(r"1주당\s*배당금\s*\(원\)\s*보통주식\s*([0-9,\.]+)", t)
    m_yd = re.search(r"시가배당률\s*\(%\)\s*보통주식\s*([0-9\.]+)", t)
    m_rec = re.search(r"배당기준일\s*" + _DATE, t) or re.search(r"기준일\s*" + _DATE, t)
    m_pay = re.search(r"지급\s*예정일(?:자)?\s*" + _DATE, t)
    m_paynote = None if m_pay else re.search(r"지급\s*예정일(?:자)?\s*([가-힣0-9 ]{2,40}?)\s*(?:\d{1,2}\.\s|$)", t)
    m_agm = re.search(r"주주총회\s*예정일자\s*" + _DATE, t)
    m_tot = re.search(r"배당금총액\s*\(원\)\s*([0-9,]+)", t)
    pay_note = None
    if m_paynote:
        s = m_paynote.group(1).strip(" -")
        pay_note = s if s and s != "-" else None
    return {
        "kind": m_kind.group(1) if m_kind else None,          # 분기배당/중간배당/결산배당
        "div_type": m_type.group(1) if m_type else None,      # 현금배당/현물배당
        "per_share": _num(m_ps.group(1)) if m_ps else None,
        "yield_pct": _num(m_yd.group(1)) if m_yd else None,
        "record_date": m_rec.group(1) if m_rec else None,
        "pay_date": m_pay.group(1) if m_pay else None,
        "pay_note": pay_note,
        "agm_date": m_agm.group(1) if m_agm else None,
        "total_amount": _num(m_tot.group(1)) if m_tot else None,
    }


def is_dividend_filing(report_nm: str) -> bool:
    n = (report_nm or "").replace(" ", "")
    if "배당락" in n or "자회사의주요경영사항" in n:   # 자회사 건은 자회사 자신의 공시와 중복
        return False
    return ("배당결정" in n) or ("주주명부폐쇄" in n and "배당" in n)


def fetch_dividend_doc(rcept_no: str):
    """원문 → 파싱 dict | None. 캐시 히트면 콜 0."""
    html, src = dart_doc.fetch_document_html(rcept_no, kind=DOC_KIND)
    if src == "fetch":
        CALLS["doc"] += 1
    if not html:
        return None
    return parse_dividend_text(strip_html(html))


def collect_dividend_filings(bgn: dt.date, end: dt.date, known: dict, session=None):
    """I 공시에서 배당 건을 골라 원문 파싱. known(rcept_no→parsed) 에 있으면 재파싱 안 함.
    반환: (filings dict rcept_no→record, n_new, n_docfail)"""
    rows = list_range(bgn, end, "I", session=session)
    out = dict(known)
    n_new = n_fail = 0
    for x in rows:
        if not is_dividend_filing(x.get("report_nm")):
            continue
        rc = str(x.get("rcept_no") or "")
        code = (x.get("stock_code") or "").strip().upper()
        if not rc or not CODE_RE.match(code):
            continue
        if rc in out:
            continue
        parsed = fetch_dividend_doc(rc)
        if parsed is None or not parsed.get("record_date"):
            n_fail += 1
            continue
        nm = (x.get("report_nm") or "").strip()
        parsed.update({
            "rcept_no": rc, "rcept_dt": x.get("rcept_dt"), "corp_code": x.get("corp_code"),
            "corp_name": x.get("corp_name"), "stock_code": code,
            "market": {"Y": "KOSPI", "K": "KOSDAQ"}.get(x.get("corp_cls"), None),
            "report_nm": re.sub(r"\s+", " ", nm),
            "is_closure": "주주명부폐쇄" in nm.replace(" ", ""),
            "is_reit": "부동산투자회사" in nm,
        })
        if not parsed["kind"]:
            if "분기" in nm:
                parsed["kind"] = "분기배당"
            elif "중간" in nm:
                parsed["kind"] = "중간배당"
        out[rc] = parsed
        n_new += 1
        time.sleep(0.15)
    return out, n_new, n_fail


def merge_dividends(filings: dict) -> list:
    """(종목, 기준일) 병합. 금액 있는 결정 > 폐쇄 / 같은 급이면 나중 rcept 가 이긴다(정정)."""
    best = {}
    for rec in filings.values():
        k = (rec["stock_code"], rec["record_date"])
        rank = (1 if rec.get("per_share") is not None else 0, rec["rcept_no"])
        cur = best.get(k)
        if cur is None or rank > cur[0]:
            merged = dict(rec)
            if cur is not None:   # 낮은 급의 값 중 빈 칸 보충(예: 폐쇄 공시의 기준일만 있는 경우)
                for f, v in cur[1].items():
                    if merged.get(f) is None and v is not None:
                        merged[f] = v
            best[k] = (rank, merged)
        else:
            for f, v in rec.items():
                if cur[1].get(f) is None and v is not None:
                    cur[1][f] = v
    return [v[1] for v in best.values()]


# ---------------------------------------------------------------- IPO
def is_ipo_filing(x: dict) -> bool:
    nm = (x.get("report_nm") or "")
    return "증권신고서(지분증권)" in nm and not (x.get("stock_code") or "").strip()


_KDATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")


def parse_kdate_range(s):
    """'2026년 09월 17일 ~ 2026년 09월 18일' → ('2026-09-17','2026-09-18'). 단일이면 (d,d)."""
    ds = ["%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3))) for m in _KDATE.finditer(s or "")]
    if not ds:
        return None, None
    return ds[0], ds[-1]


def fetch_estk(corp_code: str, rcept_no: str, bgn: str, end: str, session=None):
    """estkRs.json 일반사항 중 rcept_no 행(없으면 최신 행). 없으면 None."""
    s = session or requests
    CALLS["estk"] += 1
    r = s.get(ESTK_URL, params={"crtfc_key": _key(), "corp_code": corp_code, "bgn_de": bgn, "end_de": end}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "000":
        return None
    gen = [g for g in j.get("group", []) if g.get("title") == "일반사항"]
    rows = gen[0].get("list", []) if gen else []
    hit = [x for x in rows if x.get("rcept_no") == rcept_no] or rows[-1:]
    if not hit:
        return None
    row = hit[0]
    ss, se = parse_kdate_range(row.get("sbd"))
    pay, _ = parse_kdate_range(row.get("pymd"))
    price = _num(row.get("exprc")) if row.get("exprc") and row.get("exprc") != "-" else None
    return {"sub_start": ss, "sub_end": se, "pay_date": pay, "price": price,
            "estk_rcept_no": row.get("rcept_no")}


def collect_ipo_filings(bgn: dt.date, end: dt.date, known: dict, session=None):
    """C 공시 → 비상장 증권신고서(지분증권) → 법인별 최신 접수 1건 + estkRs.
    known: corp_code → record(이전 배치). 최신 rcept 가 바뀐 법인만 estkRs 재조회.
    반환 (filings corp_code→record, n_new, n_fail)"""
    rows = list_range(bgn, end, "C", session=session)
    latest = {}
    for x in rows:
        if not is_ipo_filing(x):
            continue
        cc = x.get("corp_code")
        if cc and (cc not in latest or x["rcept_no"] > latest[cc]["rcept_no"]):
            latest[cc] = x
    out = dict(known)
    n_new = n_fail = 0
    for cc, x in latest.items():
        prev = out.get(cc)
        if prev and prev.get("rcept_no") == x["rcept_no"]:
            continue
        rd = x.get("rcept_dt") or ""
        b = (dt.datetime.strptime(rd, "%Y%m%d").date() - dt.timedelta(days=200)).strftime("%Y%m%d") if rd else bgn.strftime("%Y%m%d")
        info = fetch_estk(cc, x["rcept_no"], b, rd or end.strftime("%Y%m%d"), session=session)
        if not info or not (info.get("sub_start") or info.get("pay_date")):
            n_fail += 1
            if prev:      # 새 정정본 조회 실패 시 직전 값 유지
                continue
            info = info or {}
        nm = (x.get("corp_name") or "").strip()
        rec = dict(prev or {})
        rec.update(info)
        rec.update({
            "rcept_no": x["rcept_no"], "rcept_dt": rd, "corp_code": cc, "corp_name": nm,
            "report_nm": re.sub(r"\s+", " ", x.get("report_nm") or ""),
            "is_spac": "기업인수목적" in nm.replace(" ", ""),
            "price_final": "[발행조건확정]" in (x.get("report_nm") or ""),
        })
        out[cc] = rec
        n_new += 1
        time.sleep(0.15)
    return out, n_new, n_fail

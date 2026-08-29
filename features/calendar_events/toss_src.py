# -*- coding: utf-8 -*-
"""토스 Open API 소스: 상장 예정(SCHEDULED) 종목 스냅샷.

[실측 2026-08-29]
  GET /api/v1/stocks/all?market=KOSPI|KOSDAQ|KR_ETC&status=SCHEDULED
      → symbol/name/securityType/isinCode/isCommonShare 만. market=KR 은 400.
      ★한도 1 req/s (X-RateLimit-Limit: 1). 연속 호출 429 실측 → 콜 사이 1.2s.
  GET /api/v1/stocks?symbols=A,B,..  → status/listDate/market/… (5 req/s, 50개/콜)
  결과: KOSPI 0 · KOSDAQ 1(스카이랩스 386380 STOCK listDate 2026-09-04) · KR_ETC 0 · ETF 0
토큰·키는 features/ranking/price_adapter 재사용(gongsi-alert/.env 단일 소스, 출력 금지).
"""
import time

import requests

from features.ranking import price_adapter as pa

MARKETS = ("KOSPI", "KOSDAQ", "KR_ETC")
CALLS = {"stocks_all": 0, "stocks": 0}


def _log(msg):
    print(f"[cal-events][toss] {msg}")


def _get_retry(path, params, tries=4, pause=1.2):
    last = None
    for i in range(tries):
        try:
            return pa._get(path, params)
        except requests.HTTPError as e:
            last = e
            code = getattr(e.response, "status_code", None)
            if code == 429:
                time.sleep(pause * (i + 1) * 2)
                continue
            raise
    raise last


def _rows(j):
    r = (j or {}).get("result", [])
    if isinstance(r, dict):
        r = r.get("stocks") or r.get("items") or r.get("list") or []
    return r or []


def scheduled_snapshot():
    """3개 시장 SCHEDULED + 상세. 반환 list[dict(symbol,name,securityType,market,status,listDate)].
    어떤 시장이든 실패하면 예외(부분 스냅샷을 «0건»으로 오독하지 않기 위해)."""
    base = []
    for i, mkt in enumerate(MARKETS):
        if i:
            time.sleep(1.2)          # stocks/all 한도 1 req/s
        CALLS["stocks_all"] += 1
        base += _rows(_get_retry("/api/v1/stocks/all", {"market": mkt, "status": "SCHEDULED"}))
    syms = [x.get("symbol") for x in base if x.get("symbol")]
    detail = {}
    for i in range(0, len(syms), 50):
        if i:
            time.sleep(0.25)
        CALLS["stocks"] += 1
        for d in _rows(_get_retry("/api/v1/stocks", {"symbols": ",".join(syms[i:i + 50])})):
            detail[d.get("symbol")] = d
    out = []
    for x in base:
        d = detail.get(x.get("symbol"), {})
        out.append({
            "symbol": x.get("symbol"), "name": d.get("name") or x.get("name"),
            "securityType": x.get("securityType") or d.get("securityType"),
            "market": d.get("market"), "status": d.get("status"),
            "listDate": d.get("listDate"),
        })
    return out

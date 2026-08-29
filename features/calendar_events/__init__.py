# -*- coding: utf-8 -*-
"""④캘린더 「배당·상장」 데이터 레인 (격리 모듈).

소스(President 결정 2026-08-29 「토스에서 괜찮대. 그냥 해.」):
  dividend     DART 거래소공시(pblntf_ty=I) 「현금ㆍ현물배당결정」·「주주명부폐쇄(기준일)결정」
               + document.xml 원문(배당기준일·지급예정일·1주당배당금·시가배당률)
  ipo          DART 발행공시(pblntf_ty=C) 「증권신고서(지분증권)」(비상장 = stock_code 없음)
               + estkRs.json(청약기일·납입기일·확정공모가)
               + 토스 Open API SCHEDULED 종목의 listDate → 상장예정일 병합
  etf_listing  토스 Open API /api/v1/stocks/all?status=SCHEDULED 중 securityType=ETF
               ★2026-08-29 실측 0건. 코드·배치는 두고 UI 는 데이터가 생기면 자동 노출.

요청경로(api.py)는 캐시 파일만 읽는다(외부 호출 0). 배치(build.py)는 하루 2회.
실패 시 직전 캐시 유지 + 경보(notify_alert._tg_send, 운영자 테스트 채팅).
"""

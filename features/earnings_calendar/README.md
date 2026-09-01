# earnings_calendar — 정기보고서(실적) 발표 캘린더

RIMI(gongsi-alert)의 신규 독립 모듈. 상장사 **정기보고서(사업·반기·분기보고서)**
과거 접수이력으로 **다음 예상 발표일**을 데이터 기반 추정하고, watchlist 상위
종목을 DART 라이브로 조회해 실데이터 증거를 붙인다. 코스피(A_)·코스닥(B_) 전시장 커버.

## 용도
- 종목별 다음 정기보고서(실적) 발표일을 미리 알아 알림/캘린더에 태울 수 있게 한다.
- 추정 근거(방식)·신뢰도 라벨을 레코드에 함께 남겨 소비측이 취사선택 가능.

## 데이터 소스
1. **캐시(DART 0콜)** — `bench_cache/dart/` 의 **6개 접두사 전부** 스캔.
   실측 확정 접두사 매핑:
   | 접두사 | 시장 | 유형 | 정기보고서 |
   |---|---|---|---|
   | `A_`   | 코스피 | 정기공시 | 다수(20,258) |
   | `B_`   | 코스피 | 주요사항 | 0 |
   | `I_`   | 코스피 | 기타 | 소수(64) |
   | `K_A_` | 코스닥 | 정기공시 | 다수(38,494) |
   | `K_B_` | 코스닥 | 주요사항 | 0 |
   | `K_I_` | 코스닥 | 기타 | 소수(44) |
   시장 라벨은 `K_` 접두사 유무로 코스피/코스닥 구분. `report_type` 필터가
   비정기보고서를 걸러내므로 전부 넣어도 안전. **코스피·코스닥 둘 다 커버**
   (President 지시: 스코프 코스피 축소 금지).
   각 row: `rcept_no, corp_code, corp_name, stock_code, report_nm, rcept_dt(YYYYMMDD)`.
   `report_nm` 부분일치로 정기보고서 3종을 추출하고, 말미 `(YYYY.MM)`(결산기말)을
   파싱한다. `기재정정` 표기는 별도 플래그(계절성 계산에서 제외).
2. **라이브(DART ≤~20콜)** — `watchlist.json`의 `devices.legacy.stocks` 상위 N(기본 5)
   종목을 저장소 루트 `dart_poll.resolve_corp` / `fetch_disclosures`로 실제 조회.
   `dart_poll.requests.get`을 래핑해 **실제 HTTP GET 횟수를 실측 카운트**한다.
   키(`config.DART_API_KEY`, kis-trading/.env) 미로드면 라이브 전체 스킵.

## 파일
- `collect.py` — A_/B_ 덤프 스캔 → 정기보고서 이력 추출·종목별 그룹핑.
- `predict.py` — 종목별 다음 예상 발표일 추정(방식·신뢰도 라벨).
- `live_probe.py` — watchlist 상위 N종목 라이브 조회(실측 콜카운터·020 백오프·graceful).
- `demo.py` — 캐시 파이프라인 + 라이브 프로브 통합 데모.
- `out/` — 실행 산출물(`demo_result.json/txt`, `earnings_calendar.json` 예측 전량).

## 예측 방식(데이터 기반)
과거 원본(정정 제외) 정기보고서의 **결산기말→접수일 지연(lag) 계절성**을 사용한다.
1. `season_lag` — 종목이 마지막으로 제출한 회계기간의 다음 분기 슬롯을 정하고
   (03→분기, 06→반기, 09→분기, 12→사업), 그 리포트 종류의 과거 median lag를
   결산기말에 가산. 신뢰도: 이력 3건↑·σ≤7일 → high, 2건↑·σ≤15 → medium.
2. `yoy` — 같은 슬롯 전년 접수일 + 연주기(median)로 교차검증(이력 부족 시 대체).
3. `statutory` — 이력/괄호 부족 시 법정기한 폴백(사업 90일, 분기·반기 45일), low.

## 실행법 (2가지 모두 지원)
```
# 저장소 루트에서
python features/earnings_calendar/demo.py      # 스크립트 직접
python -m features.earnings_calendar.demo      # 모듈
```
어느 쪽이든 저장소 루트를 `sys.path`에 삽입해 `dart_poll`/`config`를 절대 import한다.

## 콜 예산 실측 (2026-07-19 실행, 6접두사 전시장 반영 후)
- 캐시 스캔: 파일 **132개** / **342,584행** → 정기보고서 **58,860건** 추출,
  종목 **2,593**, 다음발표 예상 **2,593건** 생성.
  - 시장분포(추출건): 코스피 **20,322** / 코스닥 **38,538**.
  - 시장분포(예측종목): 코스피 **829** / 코스닥 **1,764**.
  - 신뢰도 high 2,276 · medium 156 · low 161. 소요 ~0.6s.
  - 코스닥 검증: 에코프로비엠(247540)·알테오젠(196170)·에코프로(086520) 모두
    KOSDAQ 라벨로 정기보고서 이력·예측 포함 확인.
- 라이브 프로브: watchlist 상위 5종목 조회, **실측 DART HTTP콜 = 5** (fetch 5회,
  종목당 1콜), errors 0, 소요 ~4.5s. 예산(≤20) 이내.
  - 라이브 증거 예: 삼성전자·SK하이닉스·카카오 모두 `20260515 분기보고서 (2026.03)`
    실데이터 확인.
  - 캐시 스코프 수정 재실행 시 라이브는 스킵(`run(live=False)`)해 예산 절약,
    직전 동일코드 5콜 실측을 재사용(코드 무변경).

## 격리 규칙
- 이 패키지 **안에서만** 파일을 쓴다(`out/` 포함).
- 저장소 루트 `dart_poll`/`config`는 **읽기(import)만**. `build_impact_benchmark.py`,
  `dart_ownership.py`는 import조차 하지 않는다(락).

---

## 2026-09-01 개편 — 「재생성 배치가 아예 없었다」 사고 대응

### 무슨 일이 있었나 (실측)
`data/earnings_calendar.json` 이 `as_of: 20260515` 에서 멈춘 채 **3.5개월** 갔다.
그 안의 예측 2,593건 중 **2,530건이 2026-08월분**(반기보고서)이라, 8월이 지나며
`app.py` 의 `if d < today: continue` 로 전량 만료 → API 응답 **13건** → 실적발표 탭 빈 화면.
배당·상장은 `features/calendar_events/api.py:start_scheduler` 로 매일 도는데
**실적발표에는 그 대응물이 없었고**, `app.py:_SLOT_SPECS` 에도 슬롯이 없어
**3.5개월 정지가 아무 경보도 못 냈다.**

### 추가/변경
| 파일 | 역할 |
|---|---|
| `fetch.py` (신규) | DART `list.json` 정기공시(`pblntf_ty=A`) 증분수집 → `bench_cache/dart/` 확장. **라이브 콜은 이 파일에만 있다**(collect 는 0콜 유지). |
| `build.py` (신규) | 배치 1사이클: fetch → collect → predict → sanity → `data/earnings_calendar.json` 원자적 저장. |
| `batch.py` (신규) | 스케줄러 데몬(`calendar_events` 선례와 동일 패턴). `app.py` 가 기동 시 호출. |
| `predict.py` | (1) 12월결산 가정 제거 (2) 법정기한 sanity 필터 (3) 주말 이월. |
| `collect.py` | `rcept_no` 중복 제거(창 겹침 방어). |
| `app.py` | `stale` 플래그 · `_SLOT_SPECS["earnings"]`(kind=maxage) · 배치 배선 · `/api/health` 노출. |

### 배치 주기와 근거
**12시간마다 점검 → 데이터가 3일 이상 낡았으면 재생성.**
- 월·분기 주기는 «같은 사고를 짧게 재현»한다. 웨이브가 지나가면 전 종목 예측이 다음
  슬롯으로 갈아타야 하는데, 갈아타기가 늦으면 그 기간 탭이 **다시 빈다**
  (월 1회 = 최대 30일, 분기 1회 = 최대 90일).
- 비용이 사실상 0이다 [실측]: 캐시가 있으면 진행 중 분기만 = **31~62콜**,
  캐시가 비어도(Render) 3년 전량 = 약 **384콜**. OpenDART 일 한도 20,000콜의 0.3~1.9%.
- 끄기: `GONGSI_EARN_BATCH=0`. 조정: `GONGSI_EARN_PERIOD_SEC`·`GONGSI_EARN_STALE_H`.

### 관측(다음에 멈추면 알게 만드는 것)
- `app.py:_SLOT_SPECS["earnings"]` — `kind="maxage"`, `max_age_days=10`.
  데이터 기준일이 10일 넘게 낡으면 `NONRUN` 판정 + 운영자 텔레그램 경보(쿨다운 24h).
  [검증] 2026-05-15 스냅샷을 되돌려 넣자 즉시 `NONRUN · 109일 낡았다` 판정 + 경보 발화.
- `GET /api/earnings-calendar` 응답에 `stale` / `as_of_age_days` / `generated_at` 추가.
  임계 60일(`GONGSI_EARN_STALE_DAYS`). 「낡아서 없는 것」과 「진짜 없는 것」을 구분하기 위한 것.
- `GET /api/health` — `slot_earnings_*` + `earnings_data_age_h`·`earnings_last_run_at`·
  `earnings_last_error`·`earnings_runs`.
- ★기준일은 **파일 mtime 이 아니라 내용**(`generated_at` → `as_of`)으로 잰다.
  배포로 파일이 새로 복사되면 mtime 만 새것이 되고 데이터는 낡은 채라, mtime 기준이면
  이번 정지를 또 못 본다.

### 안전장치
- **바닥값 가드** — 예측이 `GONGSI_EARN_MIN_PREDICTIONS`(500) 미만이면 **저장하지 않는다.**
  `bench_cache/` 는 `.gitignore` 라 Render 에는 없다. 가드가 없으면 배치가 시드를
  **빈 파일로 덮어** 사고를 우리가 직접 재현한다.
  [검증] 빈 캐시로 run → `ok=False`, 시드 md5 무변경.
- **법정기한 sanity** (`predict.is_sane`) — 예측일이 그 보고서 유형의 법정기한(+여유)을
  넘으면 폐기. 여유는 실측으로 정했다(predict.py 주석: +15 에서 보존율 99.4~99.9%).
- **주말 이월** — [실측] 정기보고서 62,065건 중 토·일 접수 **0건**. median lag 를 그냥
  더하면 예측이 주말에 떨어진다(보정 전 11월 웨이브 2,595건 중 2,005건이 토·일).

### 맨이름 import 금지 (실사고)
이 패키지 모듈은 **반드시** `from features.earnings_calendar import X` 로 부른다.
`sys.path` 에 이 폴더를 넣고 `import collect` 하면 `sys.modules["collect"]` 를 **선점**하는데,
`features/mezzanine_calendar/collect.py` 를 `app.py:_build_mezzanine_payload` 가 **맨이름으로**
불러온다. → 배치를 `app.py` 에 배선하자마자 `/api/mezzanine` 이 503 으로 죽었다
(`module 'collect' has no attribute 'collect_all'`).
회귀 검사에 `/api/mezzanine` 을 반드시 포함할 것.

### 검증 실측 (2026-09-01)
- 재생성: 예측 **2,651건**(as_of 20260901, DART 콜 31), sanity 폐기 6건.
- API 응답(미래분): **2,636건** — 9월 19 · 10월 12 · **11월 2,595** · 12월 10.
- 11월 피크 = **2026-11-16(월) 2,004건** (법정기한 11/14 토 → 익영업일).
- DART 대조: 라이브 `list.json` 2025-11 정기공시 **2,623건**(KOSPI 836 + KOSDAQ 1,787),
  캐시 정기보고서 실접수 **2,624건**, 피크 2025-11-14(금) **2,011건**.
  → 우리 2026-11 예측 2,595건은 **오차 1.1%**, 피크 형상도 일치.
- 회귀: `/api/calendar/events` 286건(배당 255·IPO 31) · `/api/mezzanine` 200 · `/api/health` 200.

### 회계연도 학습 (12월결산 가정 제거)
`FM_TO_TYPE`(3=분기·6=반기·9=분기·12=사업) 고정표는 12월결산 전용이었다. 실측 반례:

| 종목 | 결산 | 고정표 결과 | 학습 후 |
|---|---|---|---|
| 현대약품(004310) | 11월결산 | 2027-05-15 (1년 밀림) | 2026-10-15 분기보고서 |
| 풍강(093380) | 8월결산 | 2027-05-15 | 2026-11-13 사업보고서 |
| SK리츠(395400) | 분기결산(사업보고서 연 4회) | 2026-09-29 반기보고서(존재하지 않음) | 2026-09-09 사업보고서 |
| ESR켄달스퀘어리츠(365550) | 반기결산(사업보고서 연 2회) | 2027-05-15 | 2026-10-15 반기보고서 |

→ 종목 «자신의» 이력에서 `fm → 보고서종류` 최빈값을 학습하고 다음 슬롯은 +3개월로 잡는다.
  12월결산이면 학습 결과가 고정표와 완전히 동일해 기존 경로는 바뀌지 않는다.
  실측 분포: 학습 2,580종목 / 폴백 71종목.

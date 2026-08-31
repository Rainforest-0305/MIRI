# 토스 시세 브리지 — 운영 런북

> 이 문서는 «집행 절차서»다. 아래 명령 중 **작업 스케줄러 등록·VM 배치·서버 푸시는
> 전부 Partner 게이트**다. 이 레인은 코드·검증까지만 하고 멈췄다.

## 1. 왜 있나

토스 Open API 는 **호출 IP 화이트리스트**다. 2026-08-31 실측:

| 호스트 | 토큰 발급 | 시세 조회 |
|---|---|---|
| 노트북 122.35.177.133 | OK | OK |
| Azure VM 20.214.137.83 | OK (President 가 허용 추가) | OK |
| **Render(싱가포르)** | **403 `IP address not allowed`** | 불가 |

미리 서버는 Render 에 있다 → **토스를 직접 부를 수 없다.** 그래서 두 개가 멈춰 있었다.

- ③랭킹 주가 축: `price_signal {status:"warming", resolved:0}` → 상위 5개 점수가 전부
  `11.62` 동점 = 순위가 무의미
- ④캘린더 상장예정: 토스가 유일한 경로라 직전 값 고정

**해법 = 3단.** 허용 IP 에서 도는 수집기가 Supabase 에 스냅샷을 적고, 서버는 직접 호출이
실패하면 그 스냅샷을 읽는다.

```
[수집기 toss_bridge.py]  ->  [Supabase toss_bridge_snapshots]  ->  [미리 서버]
  VM(정상) / 노트북(폴백)          kind별 1행 + collected_at            app.py, 캘린더 배치
```

## 2. 파일

| 경로 | 역할 |
|---|---|
| `tools/toss_bridge.py` | **수집기**. 저장소 의존 0(requests 만). VM 에 단독 배치 |
| `tools/toss_bridge_run.cmd` | 작업 스케줄러용 래퍼(로그 리다이렉트·로테이션) |
| `tools/register_toss_bridge_task.ps1` | 스케줄 등록 스크립트. **기본은 dry-run** |
| `toss_snapshot.py` | 서버측 스냅샷 리더 + **임계 상수 단일 출처** |
| `app.py` | `_price_lookup` 폴백 3경로 + 직접호출 차단기 |
| `features/today_feed/build.py` | `price_signal` 에 `source`·`age_min` 부착 |
| `features/calendar_events/build.py` | 상장예정 스냅샷 폴백 + **경보 조건 교체** |

## 3. Supabase 테이블 (이미 생성됨 — 실행 불요)

`public.toss_bridge_snapshots` · `kind` PK(`prices`/`scheduled`) · `collected_at timestamptz`
· `source_host` · `item_count` · `payload jsonb` · `meta jsonb` · **RLS on, 정책 0개**
(= anon 전면 차단, service_role 전용).

DDL 은 `toss_snapshot.DDL` / `tools/toss_bridge.py:DDL` 에 있고 `CREATE TABLE IF NOT EXISTS`
로 멱등하다. 수집기가 테이블 부재를 감지하면 Management API 로 1회 자동 생성한다.

## 4. VM 배치 (Partner 집행)

VM: Windows · `C:\Program Files\Python312\python.exe` · 배치 폴더 `C:\Users\urimk\miri-bridge\`
(⛔ `kis-trading\` 안에 두지 말 것 — 실계좌 레포 경계).

```powershell
# 4-1. 폴더 + 파일 2개
New-Item -ItemType Directory -Force C:\Users\urimk\miri-bridge
# toss_bridge.py 와 toss_bridge_run.cmd 를 그 폴더로 복사

# 4-2. .env 생성 (미리 자기 키만. 값은 gongsi-alert\.env 에서 복사)
#      TOSS_APP_KEY / TOSS_SECRET / SUPABASE_URL / SUPABASE_SERVICE_ROLE
#      SUPABASE_ACCESS_TOKEN(선택) / MIRI_BRIDGE_HOST=vm

# 4-3. requests 확인 → 없으면 설치
& "C:\Program Files\Python312\python.exe" -c "import requests; print(requests.__version__)"
& "C:\Program Files\Python312\python.exe" -m pip install requests

# 4-4. 무해한 검증(수집만, DB 미기록)
& "C:\Program Files\Python312\python.exe" C:\Users\urimk\miri-bridge\toss_bridge.py --kinds prices --max-codes 5 --dry

# 4-5. 실제 1회
& "C:\Program Files\Python312\python.exe" C:\Users\urimk\miri-bridge\toss_bridge.py --kinds prices,scheduled

# 4-6. 상태 확인 (host=vm 으로 바뀌면 성공)
& "C:\Program Files\Python312\python.exe" C:\Users\urimk\miri-bridge\toss_bridge.py --show
```

## 5. 스케줄 등록 (Partner 집행 — ⛔이 레인은 실행하지 않았다)

```powershell
# 먼저 반드시 dry-run 으로 확인
powershell -ExecutionPolicy Bypass -File C:\Users\urimk\miri-bridge\register_toss_bridge_task.ps1
# 승인 후에만
powershell -ExecutionPolicy Bypass -File C:\Users\urimk\miri-bridge\register_toss_bridge_task.ps1 -Execute
# 되돌리기
powershell -ExecutionPolicy Bypass -File C:\Users\urimk\miri-bridge\register_toss_bridge_task.ps1 -Remove -Execute
```

등록되는 작업 3종 (**모두 `/ru SYSTEM` = 세션 0 = 창이 절대 뜨지 않는다**):

| 작업 | 주기 | 근거 |
|---|---|---|
| `MIRI_TossBridge_Prices` | 10분마다 08:50~16:20 | KRX 정규장 09:00~15:30. 10분이면 화면이 사실상 실시간. 하루 약 45회 × 35종목 ≈ 1,600 candles GET(한도 5 req/s, 수집기는 0.22s 간격) |
| `MIRI_TossBridge_Sched_AM` | 매일 07:40 | 상장일은 «일» 해상도라 하루 2회로 충분 |
| `MIRI_TossBridge_Sched_PM` | 매일 18:40 | 〃 |

주말에도 돈다 — **의도한 것**이다. 주말 실행은 금요일 종가를 다시 읽어(값은 정확)
스냅샷 나이를 젊게 유지한다. 그래야 월요일 아침마다 낡음 경보가 오발하지 않는다.

## 6. 노트북 폴백 / 수동 실행

노트북 IP 도 허용돼 있다. 저장소 안에서 그대로 돈다.

```bash
cd C:/Users/urimk/ventures/gongsi-alert
python tools/toss_bridge.py --kinds prices,scheduled   # 즉시 1회
python tools/toss_bridge.py --show                     # 현재 스냅샷 나이
python toss_snapshot.py                                # 서버 관점에서 본 스냅샷
```

## 7. 경보 (스팸 제거의 핵심)

**바뀐 것: 「직접 호출 실패」로는 더 이상 울리지 않는다.** 서버가 토스를 못 부르는 건
사고가 아니라 설계된 정상 상태다. 그걸 매시간 알리던 게 스팸의 정체였다.

이제 우는 조건은 하나뿐 — **스냅샷이 낡았을 때**(= 수집기가 진짜 죽었을 때).

임계는 `toss_snapshot.py` 상단 **한 곳**:

| 상수 | 기본 | 환경변수 | 뜻 |
|---|---|---|---|
| `STALE_ALERT_HOURS` | 12h | `MIRI_TOSS_STALE_ALERT_H` | 이보다 낡으면 경보 |
| `STALE_ALERT_MIN_INTERVAL_H` | 12h | `MIRI_TOSS_STALE_ALERT_GAP_H` | 같은 경보 재발송 최소 간격(스로틀) |
| `PRICE_MAX_AGE_MIN` | 72h | `MIRI_TOSS_PRICE_MAX_AGE_MIN` | 시세를 «쓸 수 있는» 최대 나이. 넘으면 degrade |
| `SCHEDULED_MAX_AGE_MIN` | 14일 | `MIRI_TOSS_SCHED_MAX_AGE_MIN` | 상장예정 최대 나이 |

## 8. 응답으로 상태 읽기

`GET /api/ranking` → `price_signal`:

```json
{"status":"active","resolved":30,"requested":30,
 "source":"snapshot","age_min":10.6,"source_counts":{"snapshot":30}}
```

- `source`: `toss-direct`(서버가 직접 호출 성공) / `snapshot`(브리지 경유) / `none`(둘 다 실패)
- 직접·스냅샷이 **섞이면 보수적으로 `snapshot`** 으로 적는다(신선도 과대표기 금지).
  정확한 내역은 `source_counts`.
- `age_min`: 해소된 값 중 **가장 낡은** 나이(분).

운영 점검 한 줄: `source` 가 `none` 이면 수집기 또는 Supabase 를 본다.

## 9. 주의점 2가지

1. **수집 대상 종목은 라이브 랭킹 API 에서 가져온다**(`MIRI_API_BASE`, 기본
   `https://rimi-s76t.onrender.com`). 공시 흐름에 따라 매일 바뀌기 때문에 고정 목록을
   박으면 「스냅샷은 신선한데 정작 필요한 종목이 없다」가 된다. 서버가 순위를 매기는
   바로 그 종목을 그대로 묻는다. API 가 죽으면 직전 스냅샷의 코드를 재사용한다.
2. **`--max-codes` 는 랭킹 풀보다 작다**(기본 50, 풀은 60). 풀 하위 종목은 시세가
   비어 공시중요도만으로 순위가 매겨진다 — 정상 degrade다. 넓히려면 값을 올린다
   (콜 수가 비례해 늘어난다).

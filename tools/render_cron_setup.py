# -*- coding: utf-8 -*-
"""MIRI 주간 배치용 Render Cron Job 생성·검증 도구 (★모든 변경 동작은 Partner 게이트).

기본은 «읽기 전용»이다. 변경 동작(create·probe·run·set-branch)은 --apply 가 있을 때만
API 를 호출하고, 없으면 무엇을 할지만 출력한다. 키·토큰 «값»은 어떤 경우에도 출력하지 않는다.

  python tools/render_cron_setup.py plan                     # 읽기 전용: 만들 서비스 명세·전제 점검
  python tools/render_cron_setup.py create   --apply         # G2: Cron Job 생성(과금 시작, 최소 $1/월)
  python tools/render_cron_setup.py probe    --apply         # G3: one-off job 으로 --probe (쓰기 0)
  python tools/render_cron_setup.py run      --apply         # G4: 정규 명령 1회 실행(Supabase 쓰기)
  python tools/render_cron_setup.py set-branch main --apply  # G6: 추적 브랜치 전환
  python tools/render_cron_setup.py status                   # 읽기 전용: 최근 job·서비스 상태

왜 render.yaml 이 아니라 API 인가:
  [실측 2026-10-03] 이 워크스페이스의 Blueprint 는 0개다. 라이브 웹서비스 RIMI 는
  대시보드로 만든 docker 서비스이고, 레포의 render.yaml(name=miri-gongsi, python, free)과
  «일치하지 않는다». render.yaml 을 Blueprint 로 연결하면 miri-gongsi 웹서비스가
  «새로 하나 더» 생긴다. 그래서 Blueprint 를 건드리지 않고 Cron Job 만 API 로 만든다.

환경변수 «값»은 RIMI 서비스의 env 에서 서버 간으로 복사한다(로컬 .env 를 거치지 않는다
— 라이브와 동일 값이 보장되고, 값이 화면·로그에 나오지 않는다).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.render.com/v1"

WEB_SERVICE_ID = "srv-d9asuil8nd3s739no2eg"     # 라이브 RIMI (env 복사 원본, 변경하지 않음)
CRON_NAME = "miri-weekly-batch"
DEFAULT_BRANCH = "ops/render-weekly-cron"      # 1단계(검증)는 이 브랜치 — 라이브 웹 무영향
# 토 06:00 KST = 금 21:00 UTC. Render cron 은 «UTC» 로 해석한다(공식 문서).
#   06:00 인 이유: 이관 기간에 노트북 schtasks(05:00/05:30)가 먼저 돌면 weekly_batch 의
#   「오늘 이미 갱신」 가드가 건너뛴다 → 이중 실행 방지. app.py 슬롯 감시 유예(05:00+4h,
#   05:30+4h) 안에 끝난다(정상 소요 ≈ 10분).
SCHEDULE = "0 21 * * 5"
PLAN = "starter"                               # 0.5 CPU / 512MB, $0.00016/분, 최소 $1/월
REGION = "singapore"                           # 웹서비스와 같은 리전(같은 송신 IP 대역)
DOCKER_COMMAND = "python tools/weekly_batch.py"
PROBE_COMMAND = "python tools/weekly_batch.py --probe"
# 배치가 실제로 읽는 이름만 복사한다(최소 권한). X_*·VAPID_*·TOSS_* 등은 넣지 않는다.
#   SUPABASE_URL·SUPABASE_SERVICE_ROLE : market_cap_top100·analyst_consensus 쓰기
#   TELEGRAM_BOT_TOKEN·WAITLIST_TG_CHAT_ID : 운영자 DM 경보(config._TG_PAIRS 'miri_bot_dm')
#   ★TG_CHANNEL_ID(실유저 공지채널)는 «절대» 복사하지 않는다.
ENV_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE", "TELEGRAM_BOT_TOKEN", "WAITLIST_TG_CHAT_ID")
FORBIDDEN_KEYS = ("TG_CHANNEL_ID", "TG_CHANNEL_ENABLED")
# 이 경로가 바뀔 때만 cron 을 다시 빌드한다(파이프라인 분 절약 — Hobby 500분/월).
BUILD_PATHS = ["top100_collect.py", "analyst_collect.py", "config.py", "miri_cache.py",
               "notify_alert.py", "dart_poll.py", "tools/weekly_batch.py",
               "requirements.txt", "Dockerfile", "data/corp_index.json", "watchlist.json"]


def _key():
    k = os.getenv("RENDER_API_KEY", "").strip()
    if not k:
        # 레포 .env → (worktree 에서 실행할 때) 형제 폴더 gongsi-alert/.env 순.
        # worktree 에는 .env 가 없다(.gitignore) — 값을 복사해 두지 않으려고 원본을 읽는다.
        try:
            from dotenv import dotenv_values
            for p in (ROOT / ".env", ROOT.parent / "gongsi-alert" / ".env"):
                if p.exists():
                    k = (dotenv_values(p) or {}).get("RENDER_API_KEY", "") or ""
                    if k:
                        break
        except Exception:  # noqa: BLE001
            k = ""
    if not k:
        sys.exit("RENDER_API_KEY 없음(env 또는 레포 .env)")
    return k.strip().strip('"')


def _h():
    return {"Authorization": "Bearer " + _key(), "Accept": "application/json",
            "Content-Type": "application/json"}


def _get(path, **params):
    r = requests.get(API + path, headers=_h(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path, body):
    r = requests.post(API + path, headers=_h(), data=json.dumps(body), timeout=60)
    if r.status_code >= 300:
        # 응답 본문엔 값이 없지만, 혹시 몰라 200자만.
        sys.exit(f"API {path} 실패 HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.text else {}


def _patch(path, body):
    r = requests.patch(API + path, headers=_h(), data=json.dumps(body), timeout=60)
    if r.status_code >= 300:
        sys.exit(f"API {path} 실패 HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.text else {}


def _web():
    return _get(f"/services/{WEB_SERVICE_ID}")


def _find_cron():
    for x in _get("/services", limit=50):
        s = x.get("service") or x
        if s.get("name") == CRON_NAME:
            return s
    return None


def _web_env_values():
    """RIMI env 중 ENV_KEYS 만 {key: value}. 값은 반환만 하고 출력하지 않는다."""
    out = {}
    for x in _get(f"/services/{WEB_SERVICE_ID}/env-vars", limit=100):
        ev = x.get("envVar") or x
        if ev.get("key") in ENV_KEYS:
            out[ev["key"]] = ev.get("value")
    return out


def _branch_on_remote(branch):
    try:
        r = subprocess.run(["git", "ls-remote", "origin", f"refs/heads/{branch}"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def _payload(owner_id, repo, branch, env_values):
    return {
        "type": "cron_job",
        "name": CRON_NAME,
        "ownerId": owner_id,
        "repo": repo,
        "branch": branch,
        "autoDeployTrigger": "commit",       # (구 autoDeploy="yes" 는 deprecated)
        "buildFilter": {"paths": BUILD_PATHS, "ignoredPaths": []},
        "envVars": [{"key": k, "value": v} for k, v in env_values.items()],
        "serviceDetails": {
            "schedule": SCHEDULE,
            "plan": PLAN,
            "region": REGION,
            "runtime": "docker",
            "envSpecificDetails": {"dockerCommand": DOCKER_COMMAND,
                                   "dockerContext": ".",
                                   "dockerfilePath": "./Dockerfile"},
        },
    }


def _masked(p):
    q = json.loads(json.dumps(p))
    q["envVars"] = [{"key": e["key"], "value": "<RIMI 서비스 값 복사 · 비공개>"} for e in q["envVars"]]
    return q


def cmd_plan(a):
    web = _web()
    sd = web.get("serviceDetails") or {}
    print(f"[plan] 원본 웹서비스 {web.get('name')} plan={sd.get('plan')} region={sd.get('region')} "
          f"repo={web.get('repo')} branch={web.get('branch')}")
    have = _web_env_values()
    miss = [k for k in ENV_KEYS if not have.get(k)]
    print(f"[plan] 복사할 env {list(ENV_KEYS)} · RIMI 에 없음: {miss or '없음'}")
    print(f"[plan] 복사 금지 {list(FORBIDDEN_KEYS)} (실유저 채널)")
    exist = _find_cron()
    print(f"[plan] 동명 서비스 '{CRON_NAME}': {'이미 있음 ' + exist.get('id') if exist else '없음'}")
    on = _branch_on_remote(a.branch)
    print(f"[plan] 브랜치 '{a.branch}' 원격 존재: {on}")
    p = _payload(web.get("ownerId"), web.get("repo"), a.branch, {k: "" for k in ENV_KEYS})
    print(json.dumps(_masked(p), ensure_ascii=False, indent=1))
    print("[plan] 예상 비용: Starter $0.00016/분 × 약 10분 × 주 1회 ≈ $0.01/월 → "
          "최소요금 $1/월 적용(출처 render.com/pricing · render.com/docs/cronjobs)")
    return 0


def cmd_create(a):
    if _find_cron():
        sys.exit(f"'{CRON_NAME}' 이미 존재 — 중복 생성 거부")
    on = _branch_on_remote(a.branch)
    if on is False:
        sys.exit(f"브랜치 '{a.branch}' 가 원격에 없다 — 먼저 push(게이트 G1)")
    web = _web()
    vals = _web_env_values()
    miss = [k for k in ENV_KEYS if not vals.get(k)]
    if miss:
        sys.exit(f"RIMI env 에 {miss} 없음 — 생성 중단")
    p = _payload(web.get("ownerId"), web.get("repo"), a.branch, vals)
    if not a.apply:
        print("[create] DRY — 아래로 생성할 예정(--apply 로 실행):")
        print(json.dumps(_masked(p), ensure_ascii=False, indent=1))
        return 0
    res = _post("/services", p)
    s = res.get("service") or res
    print(f"[create] 생성됨 id={s.get('id')} name={s.get('name')} "
          f"schedule={SCHEDULE}(UTC) deployId={res.get('deployId')}")
    return 0


def _cron_or_die():
    s = _find_cron()
    if not s:
        sys.exit(f"'{CRON_NAME}' 없음 — create 먼저")
    return s


def cmd_probe(a):
    s = _cron_or_die()
    body = {"startCommand": PROBE_COMMAND}
    if not a.apply:
        print(f"[probe] DRY — POST /services/{s['id']}/jobs {body}")
        return 0
    res = _post(f"/services/{s['id']}/jobs", body)
    print(f"[probe] job 생성 id={res.get('id')} status={res.get('status')} — "
          "로그에서 '[weekly][PROBE]' 줄의 PASS 를 확인")
    return 0


def cmd_run(a):
    s = _cron_or_die()
    if a.force:
        body = {"startCommand": DOCKER_COMMAND + " --force"}
        if not a.apply:
            print(f"[run] DRY — POST /services/{s['id']}/jobs {body}")
            return 0
        res = _post(f"/services/{s['id']}/jobs", body)
        print(f"[run] one-off job(--force) id={res.get('id')} status={res.get('status')}")
        return 0
    if not a.apply:
        print(f"[run] DRY — POST /cron-jobs/{s['id']}/runs (정규 명령 1회)")
        return 0
    _post(f"/cron-jobs/{s['id']}/runs", {})
    print("[run] 정규 실행 트리거됨 — 대시보드 Runs/Logs 또는 /api/health 의 "
          "top100_updated_at·analyst_updated_at 로 확인")
    return 0


def cmd_set_branch(a):
    s = _cron_or_die()
    if not a.apply:
        print(f"[set-branch] DRY — PATCH /services/{s['id']} branch={a.target}")
        return 0
    _patch(f"/services/{s['id']}", {"branch": a.target})
    print(f"[set-branch] {s['id']} branch → {a.target} (다음 빌드부터 적용)")
    return 0


def cmd_status(a):
    s = _find_cron()
    if not s:
        print(f"[status] '{CRON_NAME}' 없음")
        return 0
    sd = s.get("serviceDetails") or {}
    print(f"[status] {s.get('id')} branch={s.get('branch')} schedule={sd.get('schedule')} "
          f"plan={sd.get('plan')} region={sd.get('region')} suspended={s.get('suspended')}")
    try:
        for x in _get(f"/services/{s['id']}/jobs", limit=5):
            j = x.get("job") or x
            print(f"  job {j.get('id')} {j.get('status')} start={j.get('startedAt')} "
                  f"end={j.get('finishedAt')} cmd={j.get('startCommand')}")
    except Exception as e:  # noqa: BLE001
        print(f"  job 목록 조회 실패: {type(e).__name__}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("plan", "create"):
        sp = sub.add_parser(name)
        sp.add_argument("--branch", default=DEFAULT_BRANCH)
        sp.add_argument("--apply", action="store_true")
    sp = sub.add_parser("probe")
    sp.add_argument("--apply", action="store_true")
    sp = sub.add_parser("run")
    sp.add_argument("--force", action="store_true",
                    help="one-off job 으로 --force 실행(오늘 이미 갱신돼 있어도 강제)")
    sp.add_argument("--apply", action="store_true")
    sp = sub.add_parser("set-branch")
    sp.add_argument("target")
    sp.add_argument("--apply", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    fn = {"plan": cmd_plan, "create": cmd_create, "probe": cmd_probe, "run": cmd_run,
          "set-branch": cmd_set_branch, "status": cmd_status}.get(a.cmd or "plan")
    if a.cmd is None:
        a.branch, a.apply = DEFAULT_BRANCH, False
    return fn(a)


if __name__ == "__main__":
    raise SystemExit(main())

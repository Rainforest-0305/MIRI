# -*- coding: utf-8 -*-
"""features/calendar_events/api 스케줄러 신선도 판정 테스트(외부 호출 0).

실행: python -m pytest tests/test_calendar_staleness.py -q
회귀 대상(2026-10-12): 배포마다 캘린더가 커밋된 시드(2026-08-29)로 돌아갔다.
  원인 = 신선도를 «파일 mtime» 으로 쟀다. 이미지에 구워진 시드는 mtime 이 빌드 시각이라
  데이터가 한 달 넘게 낡아도 12h 동안 「신선」으로 판정됐다.
★임계값은 전부 api 의 상수(_STALE_HOURS·_TZ_SKEW_HOURS)에서 «유도»한다 — 숫자를 박지 않는다.
"""
import datetime as dt
import json
import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, ROOT)
os.environ["GONGSI_CAL_EVENTS_BATCH"] = "0"      # import 시 배치 스레드 금지

from features.calendar_events import api, build  # noqa: E402

H = api._STALE_HOURS
SKEW = api._TZ_SKEW_HOURS
NOW = dt.datetime(2026, 10, 12, 0, 40, 0)


def _iso(t):
    return t.isoformat(timespec="seconds")


def _write(path, generated_at=None, dart_at=None, toss_at=None):
    d = {"version": build.VERSION, "filings": {}, "ipo_filings": {}, "toss_scheduled": [],
         "toss_scheduled_log": [], "events": [], "sources": {}}
    if generated_at is not None:
        d["generated_at"] = _iso(generated_at)
    if dart_at is not None:
        d["sources"]["dart"] = {"ok": True, "checked_at": _iso(dart_at)}
    if toss_at is not None:
        d["sources"]["toss"] = {"ok": True, "checked_at": _iso(toss_at)}
    path.write_text(json.dumps(d), encoding="utf-8")


@pytest.fixture
def cache(monkeypatch, tmp_path):
    p = tmp_path / "calendar_events.json"
    monkeypatch.setattr(build, "CACHE_FILE", p)
    return p


def test_baked_seed_with_fresh_mtime_is_stale(cache):
    """★회귀 본체: 내용은 낡았는데 파일은 방금 놓였다(=배포 직후 시드)."""
    old = NOW - dt.timedelta(hours=H * 80)
    _write(cache, generated_at=old, dart_at=old, toss_at=old)
    # 음성대조 — 종전 규칙(mtime)으로는 이 파일이 «신선»으로 보인다. 그게 결함이었다.
    assert (time.time() - os.path.getmtime(cache)) < H * 3600
    assert api._stale(now=NOW) is True


def test_fresh_content_with_old_mtime_is_fresh(cache):
    """반대 방향: mtime 이 낡아도 내용이 신선하면 다시 돌지 않는다(호출 낭비 방지)."""
    recent = NOW - dt.timedelta(hours=H / 2)
    _write(cache, generated_at=recent, dart_at=recent)
    past = time.time() - H * 3600 * 3
    os.utime(cache, (past, past))
    assert api._stale(now=NOW) is False


@pytest.mark.parametrize("delta_s,expect", [(-60, False), (60, True)])
def test_boundary_is_the_constant(cache, delta_s, expect):
    last = NOW - dt.timedelta(seconds=H * 3600 + delta_s)
    _write(cache, generated_at=last)
    assert api._stale(now=NOW) is expect


def test_missing_file_or_no_timestamps_is_stale(cache):
    assert api._stale(now=NOW) is True              # 파일 없음
    _write(cache)                                   # 시각 필드 없음
    assert api._stale(now=NOW) is True
    cache.write_text("{not json", encoding="utf-8")  # 손상
    assert api._stale(now=NOW) is True


def test_failed_dart_attempt_still_paces_retries(cache):
    """DART 실패 사이클은 generated_at 을 안 바꾸고 sources.dart.checked_at 만 남긴다.
    그 시각도 「마지막 배치」로 쳐야 실패 직후 재시작마다 다시 두드리지 않는다(종전 mtime 과 같은 보호)."""
    _write(cache, generated_at=NOW - dt.timedelta(hours=H * 5),
           dart_at=NOW - dt.timedelta(hours=H / 4))
    stale, last, age_h = api._stale_info(now=NOW)
    assert stale is False
    assert age_h == pytest.approx(H / 4)


def test_timezone_skew_is_tolerated_but_far_future_is_not(cache):
    """시드는 노트북(KST)에서 만들고 Render(UTC)에서 읽는다 → 최대 SKEW 시간 «미래»로 보인다."""
    _write(cache, generated_at=NOW + dt.timedelta(hours=SKEW))
    assert api._stale(now=NOW) is False
    _write(cache, generated_at=NOW + dt.timedelta(hours=SKEW + 2))
    assert api._stale(now=NOW) is True


class _Stop(Exception):
    pass


def _run_loop_once(monkeypatch, runs):
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:          # 1번째 = 부팅 지연, 2번째 = 주기 대기 → 여기서 끊는다
            raise _Stop()

    monkeypatch.setattr(api.time, "sleep", fake_sleep)
    monkeypatch.setattr(build, "run", lambda: runs.append(1) or {"ok": True, "counts": {}, "calls": {}})
    with pytest.raises(_Stop):
        api._loop()
    return sleeps


def test_loop_runs_at_boot_when_seed_is_old(cache, monkeypatch, capsys):
    old = dt.datetime.now() - dt.timedelta(hours=H * 80)
    _write(cache, generated_at=old, dart_at=old)
    runs = []
    sleeps = _run_loop_once(monkeypatch, runs)
    assert runs == [1]
    assert sleeps == [api._BOOT_DELAY, api._PERIOD]
    assert "재생성" in capsys.readouterr().out


def test_loop_skips_at_boot_when_content_is_fresh(cache, monkeypatch, capsys):
    _write(cache, generated_at=dt.datetime.now() - dt.timedelta(hours=H / 2))
    runs = []
    _run_loop_once(monkeypatch, runs)
    assert runs == []
    assert "스킵" in capsys.readouterr().out

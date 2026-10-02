# -*- coding: utf-8 -*-
"""tools/weekly_batch.py 오프라인 테스트 — 네트워크·Supabase·텔레그램·하위프로세스 0.

검증 대상:
  - 순서 강제(top100 → analyst)와 analyst 인자(--sleep 1.2 유지)
  - 「오늘 이미 갱신」 가드(이중 실행 방지)
  - top100 실패 시에도 analyst 입력(data/top100.json)을 Supabase 스냅샷으로 보장
  - 키 부재 시 «시작하지 않음»(rc=2)
  - 수집기 updated_at 이 «KST» 날짜인지(Render 컨테이너=UTC 대비)
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import config  # noqa: E402
import miri_cache as mc  # noqa: E402
import weekly_batch as wb  # noqa: E402

KST = timezone(timedelta(hours=9))


def _rows(day, n=100):
    return [{"code": f"{i:06d}", "rank": i, "name": f"N{i}", "market": "KOSPI",
             "market_cap": 10**12 - i, "cap_label": "x", "updated_at": day}
            for i in range(1, n + 1)]


class _Fake:
    """Supabase 상태를 흉내 낸다. run_step 이 호출되면 «오늘자»로 갱신된 것처럼 바꾼다."""

    def __init__(self, today, top_day, ana_day, top_ok_after=True, ana_ok_after=True):
        self.today = today
        self.top = _rows(top_day)
        self.ana = {r["code"]: ana_day for r in self.top}
        self.top_ok_after = top_ok_after
        self.ana_ok_after = ana_ok_after
        self.calls = []
        self.sent = []

    def top100_rows(self):
        return True, [dict(r) for r in self.top]

    def analyst_dates(self):
        return True, dict(self.ana)

    def run_step(self, name, args, timeout_s):
        self.calls.append((name, list(args)))
        if name == "top100" and self.top_ok_after:
            self.top = _rows(self.today)
        if name == "analyst" and self.ana_ok_after:
            self.ana = {r["code"]: self.today for r in self.top}
        return 0

    def notify(self, text):
        self.sent.append(text)
        return True


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE", "x")
    monkeypatch.setattr(wb, "_install_signals", lambda: None)
    return tmp_path


def _wire(monkeypatch, fake):
    monkeypatch.setattr(wb, "top100_rows", fake.top100_rows)
    monkeypatch.setattr(wb, "analyst_dates", fake.analyst_dates)
    monkeypatch.setattr(wb, "run_step", fake.run_step)
    monkeypatch.setattr(wb, "notify", fake.notify)


def test_stale_runs_in_order_and_keeps_sleep(env, monkeypatch):
    today = wb.today_kst()
    fake = _Fake(today, "2026-09-12", "2026-09-12")
    _wire(monkeypatch, fake)
    assert wb.run() == 0
    assert [c[0] for c in fake.calls] == ["top100", "analyst"]
    a_args = fake.calls[1][1]
    assert a_args[a_args.index("--sleep") + 1] == "1.2"
    assert a_args[a_args.index("--limit") + 1] == "0"
    assert fake.sent and "완료" in fake.sent[-1]


def test_already_fresh_skips_both(env, monkeypatch):
    today = wb.today_kst()
    fake = _Fake(today, today, today)
    _wire(monkeypatch, fake)
    assert wb.run() == 0
    assert fake.calls == []


def test_force_runs_even_if_fresh_and_flags_ambiguity(env, monkeypatch):
    today = wb.today_kst()
    fake = _Fake(today, today, today)
    _wire(monkeypatch, fake)
    assert wb.run(force=True) == 0
    assert [c[0] for c in fake.calls] == ["top100", "analyst"]
    assert "구분 불가" in fake.sent[-1]


def test_top100_fail_still_feeds_analyst_from_supabase(env, monkeypatch):
    today = wb.today_kst()
    fake = _Fake(today, "2026-09-12", "2026-09-12", top_ok_after=False)
    _wire(monkeypatch, fake)
    rc = wb.run()
    assert rc == 1
    assert [c[0] for c in fake.calls] == ["top100", "analyst"]
    snap = mc.load_json(env / "top100.json")
    assert snap["_source"] == "supabase_hydrate" and snap["count"] == 100
    assert "실패" in fake.sent[-1]


def test_analyst_low_cover_is_failure(env, monkeypatch):
    today = wb.today_kst()
    fake = _Fake(today, "2026-09-12", "2026-09-12", ana_ok_after=False)
    _wire(monkeypatch, fake)
    assert wb.run() == 1
    assert "analyst" in fake.sent[-1]


def test_missing_supabase_env_refuses_to_start(env, monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE", "")
    fake = _Fake(wb.today_kst(), "2026-09-12", "2026-09-12")
    _wire(monkeypatch, fake)
    assert wb.run() == 2
    assert fake.calls == []


def test_ensure_top100_json_keeps_fresh_local(env):
    today = wb.today_kst()
    mc.save_json(env / "top100.json", {"updated_at": today, "count": 1,
                                       "items": [{"code": "005930"}]})
    assert wb.ensure_top100_json(_rows("2026-09-12")) == "kept"


# Render cron 실행 순간 = 금 21:00 UTC = 토 06:00 KST. 이 순간 찍히는 날짜는 «토요일»이어야 한다.
_CRON_INSTANT = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            # naive now = «프로세스 로컬» — Render(UTC)라면 금요일이 나온다. 쓰면 안 되는 경로.
            return _CRON_INSTANT.replace(tzinfo=None)
        return _CRON_INSTANT.astimezone(tz)


def test_top100_updated_at_is_kst_saturday(monkeypatch):
    import top100_collect as t1
    monkeypatch.setattr(t1, "datetime", _FixedDT)
    monkeypatch.setattr(t1, "_fetch_market", lambda m, pages=None: (
        [{"code": "005930" if m == "KOSPI" else "247540", "name": m, "market": m,
          "market_cap": 10**12}], {"raw": 1, "etf": 0, "other": 0}))
    monkeypatch.setattr(t1.time, "sleep", lambda s: None)
    items, updated = t1.collect()
    assert updated == "2026-10-10"
    assert all(it["updated_at"] == "2026-10-10" for it in items)


def test_analyst_updated_at_is_kst_saturday(monkeypatch):
    import analyst_collect as ac
    monkeypatch.setattr(ac, "datetime", _FixedDT)
    monkeypatch.setattr(ac, "_prices", lambda code: [["2026-10-08", 100], ["2026-10-09", 101]])
    monkeypatch.setattr(ac, "fetch_reports", lambda *a, **k: ([], ac.REPORTS_ZERO))
    p = ac.build_payload("005930", "삼성전자", prev_loader=lambda: None)
    assert p["updated_at"] == "2026-10-10"

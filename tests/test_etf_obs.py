# -*- coding: utf-8 -*-
"""features/calendar_events/etf_obs — Supabase 관측 로그 fail-open·멱등 테스트(외부 호출 0).

실행: python -m pytest tests/test_etf_obs.py -q
requests 를 가짜 PostgREST(메모리 저장소)로 바꿔 3케이스(테이블 있음/없음/네트워크 오류)에서
배치(build.run)가 «죽지 않고» 이벤트 캐시를 정상 갱신하는지, upsert 가 obs_date 기준 멱등인지 본다.
"""
import datetime as dt
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, ROOT)
os.environ["GONGSI_CAL_EVENTS_BATCH"] = "0"

from features.calendar_events import build, dart_src, etf_obs, toss_src  # noqa: E402

SNAP = [
    {"symbol": "386380", "name": "스카이랩스", "securityType": "STOCK", "market": "KOSDAQ", "status": "SCHEDULED", "listDate": "2026-09-04"},
    {"symbol": "0ABCD0", "name": "TEST ETF", "securityType": "ETF", "market": "KOSPI", "status": "SCHEDULED", "listDate": "2026-09-08"},
]
TODAY = dt.date(2026, 8, 29)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else []

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRest:
    """PostgREST 흉내: on_conflict=obs_date upsert(merge-duplicates) + select. table_exists 토글."""

    def __init__(self, table_exists=True, mgmt_ok=True, network_down=False):
        self.rows = {}
        self.table_exists = table_exists
        self.mgmt_ok = mgmt_ok
        self.network_down = network_down
        self.calls = {"post": 0, "mgmt": 0, "get": 0}

    def post(self, url, headers=None, data=None, timeout=None):
        if "api.supabase.com" in url:
            self.calls["mgmt"] += 1
            if not self.mgmt_ok:
                return _Resp(401, {"message": "bad token"})
            assert "create table if not exists" in json.loads(data)["query"]
            self.table_exists = True
            return _Resp(201, {})
        self.calls["post"] += 1
        if self.network_down:
            raise ConnectionError("boom")
        if not self.table_exists:
            return _Resp(404, {"code": "PGRST205", "message": "Could not find the table"})
        assert "on_conflict=obs_date" in url and "merge-duplicates" in headers["Prefer"]
        for r in json.loads(data):
            self.rows[r["obs_date"]] = r          # PK merge → 같은 날은 1행
        return _Resp(201, {})

    def get(self, url, headers=None, timeout=None):
        self.calls["get"] += 1
        if self.network_down:
            raise ConnectionError("boom")
        if not self.table_exists:
            return _Resp(404, {"code": "PGRST205"})
        return _Resp(200, sorted(self.rows.values(), key=lambda r: r["obs_date"], reverse=True))


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(etf_obs.config, "SUPABASE_URL", "https://proj.supabase.co", raising=False)
    monkeypatch.setattr(etf_obs.config, "SUPABASE_SERVICE_ROLE", "svc-key-test", raising=False)
    monkeypatch.setattr(etf_obs.config, "SUPABASE_ACCESS_TOKEN", "sbp_test", raising=False)


def _batch(tmp_path, monkeypatch):
    """DART 는 빈 결과, 토스는 SNAP. 캐시는 tmp."""
    monkeypatch.setattr(build, "CACHE_FILE", tmp_path / "calendar_events.json")
    monkeypatch.setattr(build, "_alert", lambda t: None)
    monkeypatch.setattr(dart_src, "collect_dividend_filings", lambda b, e, prev: ({}, 0, 0))
    monkeypatch.setattr(dart_src, "collect_ipo_filings", lambda b, e, prev: ({}, 0, 0))
    monkeypatch.setattr(toss_src, "scheduled_snapshot", lambda: list(SNAP))


def test_build_row_pure():
    row = etf_obs.build_row(SNAP, TODAY, dt.datetime(2026, 8, 29, 3, 0, tzinfo=dt.timezone.utc))
    assert row["obs_date"] == "2026-08-29" and row["scheduled_count"] == 2
    assert row["etf_count"] == 1 and row["stock_count"] == 1
    assert row["market_counts"] == {"KOSDAQ": 1, "KOSPI": 1}
    assert row["scheduled_symbols"][1] == {"symbol": "0ABCD0", "name": "TEST ETF", "market": "KOSPI",
                                           "listDate": "2026-09-08", "securityType": "ETF"}
    assert row["fetched_at"].endswith("+00:00")
    json.dumps(row, allow_nan=False)      # 직렬화 가능
    assert etf_obs.build_row([], TODAY)["scheduled_count"] == 0


def test_upsert_idempotent_same_day(cfg, monkeypatch):
    fake = FakeRest(table_exists=True)
    monkeypatch.setattr(etf_obs, "requests", fake)
    assert etf_obs.record(SNAP, TODAY) == (True, "ok")
    assert etf_obs.record(SNAP[:1], TODAY) == (True, "ok")      # 같은 날 2회
    assert len(fake.rows) == 1 and fake.rows["2026-08-29"]["scheduled_count"] == 1   # 1행, 최신값
    assert etf_obs.record(SNAP, TODAY + dt.timedelta(days=1))[0] and len(fake.rows) == 2
    assert fake.calls["mgmt"] == 0


def test_table_missing_creates_via_mgmt(cfg, monkeypatch):
    fake = FakeRest(table_exists=False, mgmt_ok=True)
    monkeypatch.setattr(etf_obs, "requests", fake)
    assert etf_obs.record(SNAP, TODAY) == (True, "ok")
    assert fake.calls["mgmt"] == 1 and fake.calls["post"] == 2 and len(fake.rows) == 1


def test_table_missing_no_token_warns_only(cfg, monkeypatch, capsys):
    fake = FakeRest(table_exists=False)
    monkeypatch.setattr(etf_obs, "requests", fake)
    monkeypatch.setattr(etf_obs.config, "SUPABASE_ACCESS_TOKEN", "", raising=False)
    assert etf_obs.record(SNAP, TODAY) == (False, "table_missing")
    out = capsys.readouterr().out
    assert "경고" in out and "svc-key-test" not in out and fake.calls["mgmt"] == 0


def test_network_error_swallowed(cfg, monkeypatch, capsys):
    fake = FakeRest(network_down=True)
    monkeypatch.setattr(etf_obs, "requests", fake)
    ok, reason = etf_obs.record(SNAP, TODAY)
    assert ok is False and reason == "ConnectionError"
    assert "svc-key-test" not in capsys.readouterr().out
    assert etf_obs.fetch_observations(14) == (False, [])


def test_disabled_without_keys(monkeypatch):
    monkeypatch.setattr(etf_obs.config, "SUPABASE_URL", "", raising=False)
    monkeypatch.setattr(etf_obs.config, "SUPABASE_SERVICE_ROLE", "", raising=False)
    assert etf_obs.enabled() is False
    assert etf_obs.record(SNAP, TODAY)[0] is False


@pytest.mark.parametrize("mode", ["exists", "missing", "network"])
def test_batch_survives_all_db_modes(cfg, tmp_path, monkeypatch, mode):
    """3케이스 모두 배치가 죽지 않고 events 캐시·파일 로그가 정상 갱신된다."""
    fake = FakeRest(table_exists=(mode == "exists"), mgmt_ok=False, network_down=(mode == "network"))
    monkeypatch.setattr(etf_obs, "requests", fake)
    _batch(tmp_path, monkeypatch)
    s = build.run(days=3, use_toss=True, today=TODAY)
    assert s["ok"] is True and s["counts"]["etf_listing"] == 1 and s["counts"]["ipo"] == 1
    c = build.load_cache()
    assert c["toss_scheduled_log"][-1]["date"] == "2026-08-29" and c["toss_scheduled_log"][-1]["etf"] == 1
    assert c["sources"]["toss"]["ok"] is True
    obs = c["sources"]["toss"]["obs_db"]
    assert obs["ok"] is (mode == "exists")
    assert obs["reason"] == {"exists": "ok", "missing": "table_missing", "network": "ConnectionError"}[mode]
    assert len(fake.rows) == (1 if mode == "exists" else 0)


def test_batch_two_cycles_same_day_one_row(cfg, tmp_path, monkeypatch):
    fake = FakeRest(table_exists=True)
    monkeypatch.setattr(etf_obs, "requests", fake)
    _batch(tmp_path, monkeypatch)
    build.run(days=3, use_toss=True, today=TODAY)
    build.run(days=3, use_toss=True, today=TODAY)
    assert len(fake.rows) == 1 and fake.calls["post"] == 2
    assert len(build.load_cache()["toss_scheduled_log"]) == 1


def test_dry_run_skips_db(cfg, tmp_path, monkeypatch):
    fake = FakeRest(table_exists=True)
    monkeypatch.setattr(etf_obs, "requests", fake)
    _batch(tmp_path, monkeypatch)
    build.run(days=3, use_toss=True, dry=True, today=TODAY)
    assert fake.calls["post"] == 0


def test_cli_table_output(cfg, monkeypatch, capsys):
    fake = FakeRest(table_exists=True)
    monkeypatch.setattr(etf_obs, "requests", fake)
    etf_obs.record(SNAP, TODAY)
    monkeypatch.setattr(etf_obs.dt, "date", _FixedDate)
    assert etf_obs.main(["--days", "14"]) == 0
    out = capsys.readouterr().out
    assert "2026-08-29" in out and "0ABCD0" in out and "days_with_etf=1" in out


class _FixedDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 30)

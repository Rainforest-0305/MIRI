# -*- coding: utf-8 -*-
"""배치: DART 배당·IPO + 토스 SCHEDULED → data/calendar_events.json (원자적 저장).

실행:  python -m features.calendar_events.build [--days 90] [--no-toss] [--dry]
정책:
  · 증분 — 캐시의 filings(rcept_no별 파싱 결과)를 상태로 들고 새 접수만 원문 조회.
  · 실패 — 소스 하나라도 예외면 «직전 캐시 유지» + 경보. 빈 값으로 덮지 않는다.
    (토스 실패는 DART 결과까지 버리지 않는다: 토스 절은 직전 스냅샷을 유지하고 sources 에 실패 기록)
  · 토스 SCHEDULED 관측 로그 — 하루 1줄(toss_scheduled_log, 최대 90줄). 2주 뒤
    「토스가 ETF 를 SCHEDULED 로 잡는가」를 데이터로 판정하기 위한 것.
    ★파일은 Render 디스크 휘발로 하루도 못 버틴다 → 같은 관측을 Supabase
    calendar_etf_observations 에 upsert(etf_obs.py, obs_date 1행/일, fail-open).
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

try:
    import config
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)))
    import config

from features.calendar_events import dart_src, etf_obs, toss_src

CACHE_FILE = config.DATA / "calendar_events.json"
VERSION = 1
DISCLAIMER = "DART 공시와 토스증권 상장 예정 정보를 정리한 참고 자료입니다. 일정은 회사 사정으로 바뀔 수 있어요."

LABELS = {
    ("dividend", "record"): "배당기준일",
    ("dividend", "payment"): "배당 지급",
    ("ipo", "subscription"): "청약",
    ("ipo", "payment"): "납입",
    ("ipo", "listing"): "상장",
    ("etf_listing", "listing"): "ETF 상장",
}


def _log(msg):
    print(f"[cal-events] {msg}")


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("version") == VERSION:
            return d
    except (OSError, ValueError):
        pass
    return {"version": VERSION, "filings": {}, "ipo_filings": {}, "toss_scheduled": [],
            "toss_scheduled_log": [], "events": [], "sources": {}}


def save_cache(d):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, CACHE_FILE)


def _norm_name(s):
    s = (s or "").replace("주식회사", "").replace("㈜", "").replace("(주)", "")
    return re.sub(r"[\s\-·ㆍ]", "", s).lower()


# ---------------------------------------------------------------- 이벤트 생성
def _ev(typ, sub, date, corp_name, stock_code, market, extra):
    return {
        "id": f"{typ}:{stock_code or _norm_name(corp_name)}:{date}:{sub}",
        "type": typ, "subtype": sub, "label": LABELS[(typ, sub)], "date": date,
        "corp_name": corp_name, "stock_code": stock_code or "", "market": market,
        **extra,
    }


def dividend_events(merged):
    out = []
    for r in merged:
        info = {
            "kind": r.get("kind"), "div_type": r.get("div_type"),
            "per_share": r.get("per_share"), "yield_pct": r.get("yield_pct"),
            "record_date": r.get("record_date"), "pay_date": r.get("pay_date"),
            "pay_note": r.get("pay_note") or (None if r.get("pay_date") else "주주총회 후 확정"),
            "agm_date": r.get("agm_date"), "is_reit": bool(r.get("is_reit")),
            "rcept_no": r.get("rcept_no"),
        }
        base = dict(dividend=info, rcept_no=r.get("rcept_no"))
        out.append(_ev("dividend", "record", r["record_date"], r["corp_name"], r["stock_code"], r.get("market"), base))
        if r.get("pay_date"):
            out.append(_ev("dividend", "payment", r["pay_date"], r["corp_name"], r["stock_code"], r.get("market"), base))
    return out


def ipo_events(ipo_filings, toss_scheduled):
    """DART 청약·납입 + 토스 SCHEDULED(STOCK) listDate 병합(이름 정규화 매칭)."""
    by_name = {_norm_name(t.get("name")): t for t in toss_scheduled if t.get("securityType") == "STOCK"}
    used = set()
    out = []
    for r in ipo_filings.values():
        t = by_name.get(_norm_name(r.get("corp_name")))
        if t:
            used.add(t.get("symbol"))
        info = {
            "sub_start": r.get("sub_start"), "sub_end": r.get("sub_end"), "pay_date": r.get("pay_date"),
            "list_date": (t or {}).get("listDate"), "price": r.get("price"),
            "price_final": bool(r.get("price_final")), "is_spac": bool(r.get("is_spac")),
            "rcept_no": r.get("rcept_no"),
        }
        code = (t or {}).get("symbol") or ""
        market = (t or {}).get("market")
        base = dict(ipo=info, rcept_no=r.get("rcept_no"))
        if r.get("sub_start"):
            out.append(_ev("ipo", "subscription", r["sub_start"], r["corp_name"], code, market, base))
        if r.get("pay_date"):
            out.append(_ev("ipo", "payment", r["pay_date"], r["corp_name"], code, market, base))
        if info["list_date"]:
            out.append(_ev("ipo", "listing", info["list_date"], r["corp_name"], code, market, base))
    # DART 에 없는데 토스에만 SCHEDULED 인 주식(재상장·이전상장 등) — 상장일만
    for t in toss_scheduled:
        if t.get("securityType") == "STOCK" and t.get("listDate") and t.get("symbol") not in used:
            info = {"sub_start": None, "sub_end": None, "pay_date": None, "list_date": t["listDate"],
                    "price": None, "price_final": False, "is_spac": "스팩" in (t.get("name") or ""), "rcept_no": None}
            out.append(_ev("ipo", "listing", t["listDate"], t.get("name"), t.get("symbol"), t.get("market"), dict(ipo=info, rcept_no=None)))
    return out


def etf_events(toss_scheduled):
    out = []
    for t in toss_scheduled:
        if t.get("securityType") == "ETF" and t.get("listDate"):
            info = {"symbol": t.get("symbol"), "name": t.get("name"), "list_date": t["listDate"]}
            out.append(_ev("etf_listing", "listing", t["listDate"], t.get("name"), t.get("symbol"), t.get("market"), dict(etf=info, rcept_no=None)))
    return out


def build_events(cache, today: dt.date):
    """캐시의 filings/ipo_filings/toss_scheduled → events(전달 1일 이후만, 날짜순)."""
    keep_from = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1).isoformat()
    merged = dart_src.merge_dividends(cache.get("filings", {}))
    evs = dividend_events(merged) + ipo_events(cache.get("ipo_filings", {}), cache.get("toss_scheduled", [])) \
        + etf_events(cache.get("toss_scheduled", []))
    seen, out = set(), []
    for e in sorted(evs, key=lambda e: (e["date"], e["type"], e.get("corp_name") or "")):
        if e["date"] < keep_from or e["id"] in seen:
            continue
        seen.add(e["id"])
        out.append(e)
    return out


# ---------------------------------------------------------------- 배치 본체
def _alert(text):
    try:
        import notify_alert
        ok = notify_alert._tg_send("[MIRI 캘린더 배치] " + text)
        _log(f"경보 {'발송' if ok else '발송불가(로그만)'}: {text}")
    except Exception as e:  # noqa: BLE001
        _log(f"경보 모듈 실패(무시): {type(e).__name__} · 내용: {text}")


# ---------------------------------------------------------------- 토스 절(브리지 경유)
# ★2026-08-31 재설계. 종전에는 「토스 직접 호출 실패」를 그대로 errors 에 넣어
#   매 사이클 텔레그램 경보를 쐈다. 그런데 서버(Render, 싱가포르)는 토스 IP
#   화이트리스트에 없어 **직접 호출이 구조적으로 항상 실패**한다 — 즉 그 경보는
#   「사고」가 아니라 「설계된 정상 상태」를 매시간 알리는 스팸이었다(President 인지).
#   고칠 것은 경보 문구가 아니라 «울리는 조건»이다:
#     직접 실패 → 조용히 브리지 스냅샷으로 대체(정상 경로)
#     스냅샷이 STALE_ALERT_HOURS 이상 낡음 → 그때«만» 경보(=수집기가 진짜 죽음)
def _toss_snapshot_mod():
    try:
        import toss_snapshot
        return toss_snapshot
    except Exception as e:  # noqa: BLE001
        _log(f"브리지 모듈 로드 실패(무시): {type(e).__name__}")
        return None


def fetch_scheduled(dry=False):
    """상장예정 확보. (items|None, info).

    ① 토스 직접 호출 — 허용 IP(노트북·Azure VM)에서만 성공. 성공하면 브리지
       스냅샷에도 적어 다른 호스트(서버)가 쓰게 한다(fail-open).
    ② 실패하면 Supabase 브리지 스냅샷 읽기 — 서버의 «정상» 경로다.
    ③ 둘 다 없으면 (None, info) → 호출부가 직전 캐시를 유지한다(빈 값으로 덮지 않음).
    info = {source: toss-direct|snapshot|none, age_min, reason}
    """
    ts = _toss_snapshot_mod()
    try:
        snap = toss_src.scheduled_snapshot()
        if not dry and ts is not None:
            ok, why = ts.put(ts.KIND_SCHEDULED, {"items": snap},
                             meta={"via": "calendar_batch", "total": len(snap)})
            _log(f"브리지 스냅샷 갱신 {'성공' if ok else '실패(무시): ' + why}")
        return snap, {"source": "toss-direct", "age_min": 0.0, "reason": "ok"}
    except Exception as e:  # noqa: BLE001
        direct_err = f"{type(e).__name__}: {str(e)[:120]}"
    if ts is None:
        return None, {"source": "none", "age_min": None,
                      "reason": "bridge_module_missing", "direct_error": direct_err}
    items, meta = ts.scheduled()
    if items is None:
        return None, {"source": "none", "age_min": meta.get("age_min"),
                      "reason": meta.get("reason"), "direct_error": direct_err}
    return items, {"source": "snapshot", "age_min": meta.get("age_min"), "reason": "ok",
                   "collected_at": meta.get("collected_at"),
                   "source_host": meta.get("source_host"), "direct_error": direct_err}


def _toss_stale_alert(info, prev_src, dry=False):
    """스냅샷 나이 기준 경보(직접 실패로는 울리지 않는다). 반환: last_stale_alert_at|None.

    같은 상태로 매 사이클 울지 않게 STALE_ALERT_MIN_INTERVAL_H 간격 스로틀."""
    if info.get("source") == "toss-direct":
        return None                      # 방금 실측했다 = 신선. 경보 대상 아님
    ts = _toss_snapshot_mod()
    limit_min = (ts.STALE_ALERT_HOURS if ts else 12.0) * 60.0
    gap_h = ts.STALE_ALERT_MIN_INTERVAL_H if ts else 12.0
    age = info.get("age_min")
    if age is not None and age <= limit_min:
        return None                      # 스냅샷이 충분히 신선 → 조용히 대체 운영(정상)
    last = (prev_src or {}).get("last_stale_alert_at")
    if last:
        try:
            since_h = (dt.datetime.now() - dt.datetime.fromisoformat(last)).total_seconds() / 3600.0
            if since_h < gap_h:
                _log(f"스냅샷 낡음(age={age}) — 직전 경보 {since_h:.1f}h 전이라 스킵(스로틀)")
                return last
        except ValueError:
            pass
    age_s = f"{age / 60:.1f}h" if isinstance(age, (int, float)) else "없음"
    if not dry:
        _alert(f"토스 브리지 스냅샷이 낡았습니다(나이 {age_s}, 임계 {limit_min / 60:.0f}h). "
               f"수집기(toss_bridge.py)가 멈췄는지 확인 필요. 사유={info.get('reason')}")
    return dt.datetime.now().isoformat(timespec="seconds")


def run(days: int = 90, use_toss: bool = True, dry: bool = False, today: dt.date = None) -> dict:
    """1사이클. 반환 요약 dict(ok, counts, calls, errors). 실패 시 캐시는 덮지 않는다."""
    t0 = time.time()
    today = today or dt.date.today()
    cache = load_cache()
    for k in dart_src.CALLS:
        dart_src.CALLS[k] = 0
    for k in toss_src.CALLS:
        toss_src.CALLS[k] = 0
    errors = []
    sources = dict(cache.get("sources") or {})

    # 1) DART 창: 직전 성공 접수일-3일 ~ 오늘 (첫 실행은 days 일)
    last = (sources.get("dart") or {}).get("last_list_date")
    bgn = today - dt.timedelta(days=days)
    if last:
        try:
            bgn = max(bgn, dt.date.fromisoformat(last) - dt.timedelta(days=3))
        except ValueError:
            pass
    try:
        filings, n_new_div, n_fail_div = dart_src.collect_dividend_filings(bgn, today, cache.get("filings", {}))
        ipo, n_new_ipo, n_fail_ipo = dart_src.collect_ipo_filings(bgn, today, cache.get("ipo_filings", {}))
        # 오래된 상태 정리(150일)
        cut = (today - dt.timedelta(days=150)).strftime("%Y%m%d")
        filings = {k: v for k, v in filings.items() if (v.get("rcept_dt") or "99999999") >= cut}
        ipo = {k: v for k, v in ipo.items() if (v.get("rcept_dt") or "99999999") >= cut}
        cache["filings"], cache["ipo_filings"] = filings, ipo
        sources["dart"] = {"ok": True, "last_list_date": today.isoformat(), "window": [bgn.isoformat(), today.isoformat()],
                           "div_filings": len(filings), "div_new": n_new_div, "div_docfail": n_fail_div,
                           "ipo_corps": len(ipo), "ipo_new": n_new_ipo, "ipo_fail": n_fail_ipo,
                           "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
    except Exception as e:  # noqa: BLE001
        errors.append(f"dart: {type(e).__name__}: {str(e)[:160]}")
        sources["dart"] = {**(sources.get("dart") or {}), "ok": False, "error": errors[-1],
                           "checked_at": dt.datetime.now().isoformat(timespec="seconds")}

    # 2) 토스 SCHEDULED 스냅샷(직접 → 브리지 스냅샷 폴백) + 관측 로그 1줄/일
    #    ★errors 에 넣지 않는다 — 「직접 호출 실패」는 서버에선 정상 상태이고,
    #      errors 에 넣으면 아래 «일부 소스 실패» 경보가 매 사이클 울린다(스팸의 원인).
    #      토스 절의 경보는 _toss_stale_alert 가 «스냅샷 나이» 기준으로만 판단한다.
    toss_info = None
    if use_toss:
        prev_toss = sources.get("toss") or {}
        snap, toss_info = fetch_scheduled(dry=dry)
        last_alert = _toss_stale_alert(toss_info, prev_toss, dry=dry) \
            or prev_toss.get("last_stale_alert_at")
        if snap is not None:
            cache["toss_scheduled"] = snap
            n_etf = sum(1 for s in snap if s.get("securityType") == "ETF")
            n_stk = sum(1 for s in snap if s.get("securityType") == "STOCK")
            log = [l for l in cache.get("toss_scheduled_log", []) if l.get("date") != today.isoformat()]
            log.append({"date": today.isoformat(), "total": len(snap), "etf": n_etf, "stock": n_stk,
                        "symbols": [f"{s.get('symbol')}:{s.get('securityType')}:{s.get('listDate')}" for s in snap][:30]})
            cache["toss_scheduled_log"] = log[-90:]
            # Supabase 영속 관측(fail-open: 테이블 없음/네트워크 오류여도 배치는 계속, 파일 로그 유지)
            obs_ok, obs_reason = etf_obs.record(snap, today) if not dry else (False, "dry")
            sources["toss"] = {"ok": True, "scheduled": len(snap), "etf": n_etf, "stock": n_stk,
                               "source": toss_info["source"], "age_min": toss_info.get("age_min"),
                               "collected_at": toss_info.get("collected_at"),
                               "source_host": toss_info.get("source_host"),
                               "obs_db": {"ok": bool(obs_ok), "reason": obs_reason},
                               "last_stale_alert_at": last_alert,
                               "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
            _log(f"toss SCHEDULED total={len(snap)} etf={n_etf} stock={n_stk} "
                 f"source={toss_info['source']} age={toss_info.get('age_min')}min obs_db={obs_ok}({obs_reason})")
        else:
            # 직접·스냅샷 둘 다 없음 → 직전 cache['toss_scheduled'] 를 그대로 쓴다(빈 값 덮기 금지)
            sources["toss"] = {**prev_toss, "ok": False, "source": "none",
                               "reason": toss_info.get("reason"),
                               "direct_error": toss_info.get("direct_error"),
                               "age_min": toss_info.get("age_min"),
                               "last_stale_alert_at": last_alert,
                               "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
            _log(f"toss 확보 실패(직전 값 유지): {toss_info.get('reason')} / "
                 f"직접={toss_info.get('direct_error')}")

    dart_ok = sources.get("dart", {}).get("ok")
    # ok 판정: DART 는 필수. 토스는 «스냅샷으로라도 확보되면» 정상으로 본다
    # (직접 호출 불가는 서버의 설계된 정상 상태). 둘 다 없을 때만 실패로 센다.
    toss_ok = (toss_info is None) or (toss_info.get("source") != "none")
    summary = {"ok": bool(dart_ok) and not errors and toss_ok, "errors": errors,
               "toss": toss_info,
               "calls": {"dart": dict(dart_src.CALLS), "toss": dict(toss_src.CALLS)},
               "elapsed_s": round(time.time() - t0, 1)}
    if not dart_ok:
        # DART 자체가 죽으면 이벤트 재생성도 하지 않는다(직전 events 유지). 경보만.
        _alert("DART 수집 실패 → 직전 캐시 유지. " + "; ".join(errors))
        summary["counts"] = _counts(cache.get("events", []))
        if not dry:
            cache["sources"] = sources
            save_cache(cache)     # sources 의 실패 기록만 갱신(events 불변)
        return summary

    events = build_events(cache, today)
    cache.update({"events": events, "sources": sources, "as_of": today.isoformat(),
                  "generated_at": dt.datetime.now().isoformat(timespec="seconds")})
    summary["counts"] = _counts(events)
    if errors:
        _alert("일부 소스 실패(직전 값 유지): " + "; ".join(errors))
    if not dry:
        save_cache(cache)
    _log(f"done ok={summary['ok']} counts={summary['counts']} calls={summary['calls']} {summary['elapsed_s']}s")
    return summary


def _counts(events):
    c = {"total": len(events), "dividend": 0, "ipo": 0, "etf_listing": 0}
    for e in events:
        c[e["type"]] = c.get(e["type"], 0) + 1
    return c


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--no-toss", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    s = run(days=a.days, use_toss=not a.no_toss, dry=a.dry)
    print(json.dumps(s, ensure_ascii=False))
    sys.exit(0 if s["ok"] else 1)

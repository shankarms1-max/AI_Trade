"""READ-ONLY MCX paper-trading READINESS report. Runs inside the backend container (python - < this file) or locally.
HTTP GETs against the backend only. Answers, per instrument: is the strategy genuinely able to trade, what is it waiting
for, and for how long. Exit status is non-zero when a required liveness check fails (a healthy container is not enough).
Environment: VERIFY_BASE."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
import urllib.request

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
IST = timezone(timedelta(hours=5, minutes=30))
INST = ("CRUDEOILM", "NATGASMINI")
FAIL = []


def get(path):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=90))
    except Exception as exc:  # noqa: BLE001
        FAIL.append("API error %s: %s" % (path, str(exc)[:80]))
        return {}


def check(label, ok, detail=""):
    print("  %-4s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        FAIL.append(label)


def parse(ts):
    try:
        d = datetime.fromisoformat(str(ts))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None


now = datetime.now(IST)
hdr = get("/api/portfolio/header")
mcx_open = (hdr.get("mcx") or {}).get("status") == "OPEN"
print("==== MCX PAPER READINESS  %s IST | MCX session %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), "OPEN" if mcx_open else "closed"))
status = {m["instrument"]: m for m in get("/api/mcx-futures/status").get("instruments", [])}
for i in INST:
    m = status.get(i)
    check("%s present in status" % i, m is not None)
    if m is None:
        continue
    cfgd = (get("/api/portfolio/strategies/mcx-" + i.lower()).get("config") or {})
    p = cfgd.get("parameters") or {}
    w, d, f, ss = m.get("warmup") or {}, m.get("last_decision") or {}, m.get("feed") or {}, m.get("session") or {}
    tf = w.get("timeframe_minutes") or p.get("feature_timeframe_minutes") or 0
    req, have = w.get("bars_required"), w.get("bars_available")
    print("\n================ %s ================" % i)
    print("STATE      : %s (%s) | execution=%s | feed age=%ss | source quote=%s" % (m.get("state"), m.get("state_reason"), m.get("execution_state"), f.get("age_seconds"), f.get("last_quote_at")))
    # ---- warm-up ----
    parts = {"ema_slow+slope": (p.get("ema_slow") or 0) + (p.get("slope_bars") or 0), "2*adx+1": 2 * (p.get("adx_period") or 0) + 1,
             "breakout+1": (p.get("breakout_lookback") or 0) + 1, "er+1": (p.get("er_lookback") or 0) + 1,
             "atr+percentile samples": (p.get("atr_period") or 0) + (p.get("atr_percentile_min_samples") or 0),
             "momentum+atr+1": (p.get("momentum_bars") or 0) + (p.get("atr_period") or 0) + 1}
    print("WARM-UP    : needs %s bars of %d min (= %s min of live bars); binding feature: %s" % (req, tf, (req or 0) * tf, max(parts, key=parts.get) if p else "unknown"))
    print("             feature requirements (bars): %s" % json.dumps(parts))
    remaining = None if req is None or have is None else max(0, req - have)
    print("             progress: %s / %s bars | remaining: %s bars = at least %s min of live observations | ready=%s blocking=%s" % (have, req, remaining, None if remaining is None else remaining * tf, w.get("ready"), w.get("blocking")))
    if remaining:
        eta = now + timedelta(minutes=remaining * tf)
        last_entry = parse(ss.get("last_entry_time"))
        print("             earliest ready (if observations continue without a gap): %s IST%s" % (eta.strftime("%H:%M"), "" if not last_entry or eta < last_entry else
              " (AFTER today's last entry time %s: cannot enter today; bars persist, so warm-up completes in a later session)" % last_entry.strftime("%H:%M")))
    print("BACKFILL   : NONE - not implemented (no historical MCX data source). Bars come only from live observations and are persisted in the journal cursor state, so they SURVIVE worker restarts and session rollovers; a restart loses only the partial in-progress bar.")
    # ---- signal ----
    floor = p.get("entry_score_floor")
    comp, cmax = d.get("components") or {}, d.get("component_max") or {}
    print("SIGNAL     : at=%s status=%s direction=%s market_state=%s" % (d.get("at"), d.get("status"), d.get("direction"), d.get("market_state")))
    print("             score=%s vs entry threshold=%s -> %s" % (d.get("score"), floor, "ABOVE" if (d.get("score") is not None and floor is not None and d["score"] >= floor) else "BELOW or not computed"))
    if comp:
        print("             components (points/max): %s" % ", ".join("%s %s/%s" % (k, comp[k], cmax.get(k)) for k in comp))
    print("CYCLE      : id=%s state=%s rearm=%s" % (d.get("cycle_id"), d.get("cycle_state"), d.get("rearm_reason")))
    print("BLOCKERS   : latest=%s" % json.dumps(d.get("vetoes")))
    recent = (get("/api/mcx-futures/decisions?instrument=%s&limit=100" % i).get("items") or [])
    by = {}
    for x in recent:
        for v in x.get("vetoes") or []:
            by[v] = by.get(v, 0) + 1
    print("             last %d decision events, blockers by count: %s" % (len(recent), json.dumps(sorted(by.items(), key=lambda kv: -kv[1])[:10])))
    # ---- orders / fills / positions ----
    orders = []
    off = 0
    while True:
        pg = get("/api/portfolio/orders?engine=mcx-%s&limit=200&offset=%d" % (i.lower(), off))
        orders += pg.get("items") or []
        off += 200
        if off >= pg.get("total", 0):
            break
    byo = {}
    for o in orders:
        k = "%s/%s" % (o["intent"], o["status"]); byo[k] = byo.get(k, 0) + 1
    print("ORDERS     : %d simulated paper orders %s | fills=%d | open positions=%s | trades today=%s | rejected today=%s" % (
        len(orders), json.dumps(byo), sum(1 for o in orders if o["status"] == "FILLED"), m.get("open_position_count"), m.get("trades_today"), m.get("entries_rejected_today")))
    # ---- exits and risk controls ----
    print("EXIT RULES : initial stop = %s x ATR (minimum %s ticks); trailing activates after %s ATR profit, trails %s ATR; max holding %s min; forced exit %s IST; opposite signal exit at score >= %s; exceptional reversal >= %s after %ss confirmation" % (
        p.get("stop_atr_multiple"), p.get("min_stop_ticks"), p.get("trail_activation_atr"), p.get("trail_atr"), p.get("max_holding_minutes"),
        (ss.get("forced_exit_time") or "?")[11:16], p.get("opposite_exit_score_floor"), p.get("exceptional_reversal_score_floor"), p.get("reversal_confirmation_seconds")))
    print("RISK LIMITS: Rs %s max risk/trade | Rs %s daily loss limit | %s entries/session | %s consecutive losses | cooldown %s min after exit, %s after a loss | last entry %s IST | gap guard %s ATR/%s min | blackout events: %s" % (
        p.get("max_risk_per_trade"), p.get("daily_loss_limit"), p.get("max_entries_per_session"), p.get("max_consecutive_losses"), p.get("cooldown_after_exit_minutes"),
        p.get("cooldown_after_loss_minutes"), (ss.get("last_entry_time") or "?")[11:16], p.get("gap_guard_atr"), p.get("gap_guard_minutes"), [e.get("name") for e in p.get("events") or []]))
    print("RISK NOW   : %s" % json.dumps(m.get("risk")))
    # ---- verdict ----
    dec_at = parse(d.get("at"))
    stale_limit = (2 * tf + 1) * 60
    verdict = "UNKNOWN"
    if m.get("state") != "RUNNING" and mcx_open:
        verdict = "NOT RUNNING (%s)" % m.get("state")
    elif m.get("open_position_count"):
        verdict = "IN A POSITION: exits are being managed"
    elif remaining:
        verdict = "WARMING UP: %s bars (>= %s min) remaining; entries are impossible until then" % (remaining, remaining * tf)
    elif d.get("score") is not None and floor is not None and d["score"] >= floor and d.get("vetoes"):
        verdict = "SIGNAL ABOVE THRESHOLD BUT BLOCKED BY: %s" % ", ".join(d["vetoes"])
    elif d.get("score") is not None and floor is not None and d["score"] >= floor:
        verdict = "SIGNAL ABOVE THRESHOLD: an entry should follow on the next executable observation"
    elif d.get("score") is not None:
        verdict = "READY, WAITING FOR A SIGNAL: score %s < threshold %s" % (d.get("score"), floor)
    print("VERDICT    : %s" % verdict)
    check("%s execution_state PAPER_ENABLED (persisted)" % i, m.get("execution_state") == "PAPER_ENABLED", "-> %s" % m.get("execution_state"))
    if mcx_open:
        check("%s state RUNNING with a fresh source quote" % i, m.get("state") == "RUNNING" and f.get("age_seconds") is not None and f["age_seconds"] < 90)
        check("%s decisions are advancing (last decision within %d s)" % (i, stale_limit), dec_at is not None and (now - dec_at).total_seconds() <= stale_limit,
              "last decision %s" % d.get("at"))
        check("%s warm-up is progressing or complete (bars_available reported)" % i, have is not None and req is not None)
print("\n==== READINESS CHECKS: %s" % ("ALL PASS" if not FAIL else "FAILED -> " + "; ".join(FAIL)))
sys.exit(1 if FAIL else 0)

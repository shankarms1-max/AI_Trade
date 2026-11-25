"""READ-ONLY post-recovery verification of the two MCX paper workers (runs inside the backend container).
HTTP GETs and SELECT-only reads. Exit status is non-zero on any required failure. A paper trade that happens during the
verification window is VALIDATED (lifecycle, arithmetic, marks, stops), never treated as a failure.
Environment: VERIFY_BASE, RECOVERY_START (ISO, IST), VERIFY_ADVANCE_WAIT (s), VERIFY_DECISION_WAIT (s)."""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
IST = timezone(timedelta(hours=5, minutes=30))
ADVANCE_WAIT = int(os.environ.get("VERIFY_ADVANCE_WAIT", "60"))
DECISION_WAIT = int(os.environ.get("VERIFY_DECISION_WAIT", "420"))
INST = ("CRUDEOILM", "NATGASMINI")
FAIL, API_ERRORS = [], []


def parse(ts):
    try:
        d = datetime.fromisoformat(str(ts))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None


START = parse(os.environ.get("RECOVERY_START")) or (datetime.now(IST) - timedelta(minutes=10))


def get(path):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=90))
    except Exception as exc:  # noqa: BLE001
        API_ERRORS.append("%s -> %s" % (path, str(exc)[:90]))
        return {}


def check(label, ok, detail=""):
    print("  %-4s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        FAIL.append(label)


def cj(v, n=500):
    t = json.dumps(v, separators=(",", ":"), default=str)
    return t if len(t) <= n else t[:n] + "..."


def cursors():
    from sqlalchemy import select
    from app.db.session import get_session_factory
    from app.mcx_futures.models import McxFuturesCursor
    with get_session_factory()() as s:
        return {c.instrument: {"sequence": c.sequence, "observed_at": c.observed_at, "state": json.loads(c.state_json or "{}"),
                               "latest": json.loads(c.latest_json or "{}")} for c in s.scalars(select(McxFuturesCursor)).all()}


def bar_report(i, c, tf, required, status_bars):
    """Persisted warm-up bars: exact valid / missing counts across the overnight boundary and today's outage."""
    bars = (c["state"].get("bars") or [])
    print("  PERSISTED WARM-UP BARS (%s): %d bars of %d min; required %s" % (i, len(bars), tf, required))
    starts = [parse(b.get("start")) for b in bars]
    ordered = all(a is not None and b is not None and a < b for a, b in zip(starts, starts[1:]))
    check("%s persisted bars are strictly ordered with no duplicates or overlap" % i, ordered and all(s is not None for s in starts))
    by_session = {}
    for b in bars:
        by_session[b.get("session_id")] = by_session.get(b.get("session_id"), 0) + 1
    print("     bars per session: %s" % cj(dict(sorted(by_session.items(), key=lambda kv: str(kv[0])))))
    if bars:
        print("     first bar %s | last bar %s" % (bars[0].get("start"), bars[-1].get("end")))
    within, boundaries = [], []
    for a, b in zip(bars, bars[1:]):
        ea, sb = parse(a.get("end")), parse(b.get("start"))
        if ea is None or sb is None:
            continue
        missing = int(round((sb - ea).total_seconds() / 60.0 / tf))
        if a.get("session_id") != b.get("session_id"):
            boundaries.append({"from_session": a.get("session_id"), "to_session": b.get("session_id"), "prev_end": a.get("end"), "next_start": b.get("start")})
        elif missing > 0:
            within.append({"from": a.get("end"), "to": b.get("start"), "missing_bars": missing})
    flagged = [b for b in bars if (b.get("missing_minutes") or 0) > 0]
    missing_total = sum(g["missing_bars"] for g in within)
    print("     session boundaries inside the window (indicators run continuously across them by design; VWAP resets per session): %s" % cj(boundaries, 400))
    print("     missing bars INSIDE a session (outages): %d bars in %d gap(s): %s" % (missing_total, len(within), cj(within[-6:], 500)))
    print("     bars containing missing 1-minute data: %d (only the decision bar itself vetoes entries, via DATA_GAP_IN_BAR): %s" % (len(flagged), cj([(b["start"], b["missing_minutes"]) for b in flagged[-5:]], 300)))
    valid = len(bars)
    print("     VALID BAR COUNT (the engine's warm-up definition = persisted bars): %d | required %s | remaining %s | features ready: %s" % (
        valid, required, max(0, (required or 0) - valid), valid >= (required or 10 ** 9)))
    check("%s status bars_available == persisted bars" % i, status_bars == valid, "status=%s persisted=%s" % (status_bars, valid))
    check("%s persisted bars survived the restart (not reset to zero)" % i, valid > 0 or (required or 0) == 0, "bars=%d" % valid)
    return valid


def lifecycle(i, m, mcx_open):
    from sqlalchemy import select
    from app.db.session import get_session_factory
    from app.mcx_futures.models import McxFuturesTrade
    with get_session_factory()() as s:
        rows = [(r.state, json.loads(r.document)) for r in s.scalars(select(McxFuturesTrade).where(McxFuturesTrade.instrument == i)).all()]
    fresh = [(st, d) for st, d in rows if (parse(d.get("entry_at") or d.get("at")) or datetime.min.replace(tzinfo=IST)) >= START or st in ("OPEN", "PENDING_PAPER_ENTRY", "UNRESOLVED_EXPOSURE")]
    print("  LIFECYCLE (%s): %d trade record(s) created since recovery or still live: %s" % (i, len(fresh), cj([(st, str(d.get("trade_id"))[:8]) for st, d in fresh], 300)))
    now = datetime.now(IST)
    for st, d in fresh:
        tid = str(d.get("trade_id"))[:8]
        sign = 1 if d.get("direction") == "LONG" else -1 if d.get("direction") == "SHORT" else 0
        if st == "UNRESOLVED_EXPOSURE":
            check("%s trade %s: no UNRESOLVED exposure" % (i, tid), False)
        elif st == "PENDING_PAPER_ENTRY":
            at = parse(d.get("at") or d.get("decided_at"))
            check("%s trade %s: pending entry resolves promptly" % (i, tid), at is None or (now - at).total_seconds() < 600, "pending since %s" % (d.get("at")))
        elif st == "OPEN":
            pos = (m.get("position") or {})
            check("%s trade %s OPEN: entry price, direction and quantity recorded" % (i, tid), d.get("entry_price") is not None and sign != 0 and (d.get("quantity_lots") or 0) > 0)
            check("%s trade %s OPEN: protective stop is set" % (i, tid), pos.get("stop_price") is not None, "stop=%s" % pos.get("stop_price"))
            mk = parse(pos.get("mark_at"))
            if mcx_open:
                check("%s trade %s OPEN: mark is fresh (<120 s)" % (i, tid), mk is not None and (now - mk).total_seconds() < 120, "mark_at=%s" % pos.get("mark_at"))
        elif st == "CLOSED":
            ok_fields = all(d.get(k) is not None for k in ("entry_price", "exit_price", "entry_at", "exit_at", "gross_pnl", "net_pnl", "fees"))
            check("%s trade %s CLOSED: entry/exit fields complete" % (i, tid), ok_fields)
            if ok_fields:
                lot = (m.get("contract") or {}).get("lot_size") or d.get("lot_size") or 0
                gross = (d["exit_price"] - d["entry_price"]) * sign * lot * (d.get("quantity_lots") or 1)
                check("%s trade %s CLOSED: gross == (exit-entry) x sign x lot x lots" % (i, tid), abs(gross - d["gross_pnl"]) < 0.01, "expected %.2f recorded %s" % (gross, d["gross_pnl"]))
                check("%s trade %s CLOSED: net == gross - fees" % (i, tid), abs(d["gross_pnl"] - d["fees"] - d["net_pnl"]) < 0.01)
                check("%s trade %s CLOSED: exit after entry" % (i, tid), parse(d["exit_at"]) >= parse(d["entry_at"]))
                print("     exit reason: %s | entry %s -> exit %s | gross %s fees %s net %s" % (d.get("exit_reason"), d["entry_price"], d["exit_price"], d["gross_pnl"], d["fees"], d["net_pnl"]))


hdr = get("/api/portfolio/header")
mcx_open = (hdr.get("mcx") or {}).get("status") == "OPEN"
print("==== MCX RECOVERY VERIFICATION  %s IST | MCX session %s | recovery started %s" % (datetime.now(IST).strftime("%H:%M:%S"), "OPEN" if mcx_open else "closed", START.strftime("%H:%M:%S")))
st0 = {m["instrument"]: m for m in get("/api/mcx-futures/status").get("instruments", [])}
for i in INST:
    check("expected instrument present in /api/mcx-futures/status: " + i, i in st0)
try:
    A = cursors()
except Exception as exc:  # noqa: BLE001
    A = {}
    API_ERRORS.append("cursor read failed: %s" % exc)
for i in INST:
    check("journal cursor present: " + i, i in A)
if mcx_open:
    print("  ... waiting %d s to prove the journals advance" % ADVANCE_WAIT)
    time.sleep(ADVANCE_WAIT)
B = cursors()
status = {m["instrument"]: m for m in get("/api/mcx-futures/status").get("instruments", [])}
for i in INST:
    m = status.get(i)
    if m is None or i not in B:
        continue
    f, w, d, ss = m.get("feed") or {}, m.get("warmup") or {}, m.get("last_decision") or {}, m.get("session") or {}
    print("\n---------------- %s ----------------" % i)
    print("  state=%s (%s) execution=%s | feed: last_quote_at=%s age_s=%s bid=%s ask=%s ltp=%s" % (m.get("state"), m.get("state_reason"), m.get("execution_state"), f.get("last_quote_at"), f.get("age_seconds"), f.get("bid"), f.get("ask"), f.get("ltp")))
    print("  contract: %s" % cj(m.get("contract")))
    print("  session now: %s" % cj({k: ss.get(k) for k in ("status", "window", "session_date", "last_entry_time", "forced_exit_time")}))
    a_seq, b_seq = (A.get(i) or {}).get("sequence"), B[i]["sequence"]
    print("  journal sequence %s -> %s | observed_at %s -> %s" % (a_seq, b_seq, (A.get(i) or {}).get("observed_at"), B[i]["observed_at"]))
    latest = B[i]["latest"]
    check("%s PAPER enforced and persisted by the running worker (paper_entries_enabled is True)" % i, latest.get("paper_entries_enabled") is True and latest.get("collector_enabled") is True,
          "paper_entries_enabled=%s collector_enabled=%s" % (latest.get("paper_entries_enabled"), latest.get("collector_enabled")))
    check("%s runtime execution_state is PAPER_ENABLED" % i, m.get("execution_state") == "PAPER_ENABLED", "-> %s" % m.get("execution_state"))
    if mcx_open:
        check("%s journal sequence advanced" % i, a_seq is not None and b_seq > a_seq)
        check("%s observed_at advanced" % i, (A.get(i) or {}).get("observed_at") is not None and B[i]["observed_at"] > A[i]["observed_at"])
        check("%s state is RUNNING" % i, m.get("state") == "RUNNING", "-> %s %s" % (m.get("state"), m.get("state_reason")))
        check("%s genuine source quote timestamp present" % i, f.get("last_quote_at") is not None and f.get("source_timestamp_available") is not False)
        check("%s quote is fresh (<90 s)" % i, f.get("age_seconds") is not None and f["age_seconds"] < 90, "-> %s" % f.get("age_seconds"))
        check("%s bid/ask/ltp present (real market data)" % i, None not in (f.get("bid"), f.get("ask"), f.get("ltp")))
        check("%s contract subscribed (token, expiry in the future)" % i, bool((m.get("contract") or {}).get("token")) and str((m.get("contract") or {}).get("expiry")) > datetime.now(IST).date().isoformat())
        check("%s session metadata is for today" % i, ss.get("session_date") == str(datetime.now(IST).date()))
    c = (get("/api/portfolio/strategies/mcx-" + i.lower()).get("config") or {})
    check("%s deployed config hash == persisted worker hash" % i, c.get("matches_running_worker") is True, "deployed %s | persisted %s" % ((c.get("config_id") or "")[-12:], (c.get("persisted_config_hash") or "")[:12]))
    tf = w.get("timeframe_minutes") or (c.get("parameters") or {}).get("feature_timeframe_minutes") or 1
    valid = bar_report(i, B[i], tf, w.get("bars_required"), w.get("bars_available"))
    lifecycle(i, m, mcx_open)
    print("  exposure now (informational, a legitimate trade is allowed): open=%s unresolved=%s" % (m.get("open_position_count"), m.get("unresolved_exposure_count")))
    check("%s no UNRESOLVED exposure" % i, (m.get("unresolved_exposure_count") or 0) == 0)
if mcx_open:
    print("\n  ... waiting up to %d s for each strategy to evaluate a NEW decision after the restart (one completed bar)" % DECISION_WAIT)
    deadline, got = time.time() + DECISION_WAIT, {}
    while time.time() < deadline and len(got) < len(INST):
        for m in get("/api/mcx-futures/status").get("instruments", []):
            at = parse((m.get("last_decision") or {}).get("at"))
            if m["instrument"] in INST and at is not None and at > START and m["instrument"] not in got:
                got[m["instrument"]] = m
        if len(got) < len(INST):
            time.sleep(15)
    for i in INST:
        check("%s evaluated a genuine strategy decision after the restart" % i, i in got, "within %ss" % DECISION_WAIT)
status = {m["instrument"]: m for m in get("/api/mcx-futures/status").get("instruments", [])}
for i in INST:
    m = status.get(i) or {}
    d, w = m.get("last_decision") or {}, m.get("warmup") or {}
    cfgp = (get("/api/portfolio/strategies/mcx-" + i.lower()).get("config") or {}).get("parameters") or {}
    print("\n  DECISION (%s): at=%s status=%s direction=%s score=%s threshold=%s cycle=%s vetoes=%s warm=%s unavailable_inputs=%s" % (
        i, d.get("at"), d.get("status"), d.get("direction"), d.get("score"), cfgp.get("entry_score_floor"), d.get("cycle_state"), d.get("vetoes"), d.get("warm"), cj(d.get("unavailable_inputs"), 200)))
    print("  WARM-UP (%s): ready=%s bars %s/%s blocking=%s" % (i, w.get("ready"), w.get("bars_available"), w.get("bars_required"), w.get("blocking")))
    if mcx_open and w.get("ready") is False:
        print("    -> entries are impossible until %s more bar(s) (>= %s min of live data) are collected" % ((w.get("bars_required") or 0) - (w.get("bars_available") or 0), ((w.get("bars_required") or 0) - (w.get("bars_available") or 0)) * (w.get("timeframe_minutes") or 0)))
try:
    from app.db.session import get_session_factory
    from app.mcx_futures.store import verify_chain
    for i in INST:
        v = verify_chain(get_session_factory(), i)
        check("%s hash-chained journal verifies" % i, v.get("ok") is True, cj(v, 120))
except Exception as exc:  # noqa: BLE001
    check("journal verification ran", False, str(exc)[:100])
check("no API errors", not API_ERRORS, "; ".join(API_ERRORS[:3]))
print("\n==== MCX RECOVERY VERIFICATION: %s" % ("ALL PASS" if not FAIL else "FAILED (%d) -> %s" % (len(FAIL), "; ".join(FAIL))))
sys.exit(1 if FAIL else 0)

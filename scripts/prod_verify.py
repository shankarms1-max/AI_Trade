"""READ-ONLY production verification. Runs inside the backend container (python - < this file) or locally.
Issues only HTTP GETs against the backend itself and SELECT-only reads of the journals. Changes nothing."""
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
IST = timezone(timedelta(hours=5, minutes=30))
FAIL = []


def get(path, timeout=90):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=timeout))
    except Exception as exc:  # noqa: BLE001
        return {"_error": "%s %s" % (type(exc).__name__, str(exc)[:120])}


def show(title):
    print("\n==== " + title)


def cj(value, limit=900):
    text = json.dumps(value, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "...(+%d chars)" % (len(text) - limit)


def check(label, ok, detail=""):
    print("  %-4s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        FAIL.append(label)


def section(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print("  !! section '%s' crashed: %s %s" % (name, type(exc).__name__, str(exc)[:200]))
        FAIL.append("section:" + name)


now_ist = datetime.now(IST)
today = str(now_ist.date())
ENG = {"nifty-acts_v3": "NIFTY ACTS V3", "mcx-crudeoilm": "CRUDEOILM", "mcx-natgasmini": "NATGASMINI"}
INST = ("CRUDEOILM", "NATGASMINI")


def s_header():
    show("1 CLOCK, SESSIONS, BROKER CONNECTIVITY (inferred), DATA FRESHNESS")
    h = get("/api/portfolio/header")
    print("  now IST:", now_ist.isoformat(timespec="seconds"), "| NSE:", (h.get("nse") or {}).get("status"),
          "| MCX:", cj(h.get("mcx")))
    print("  broker connectivity:", cj(h.get("broker_connectivity")))
    print("  data freshness:", cj(h.get("data_freshness")), "| critical alerts:", h.get("critical_alerts"),
          "| warning:", h.get("warning_alerts"), "| live execution:", h.get("live_execution"))
    check("live execution is NONE", h.get("live_execution") == "NONE")


def s_engines():
    show("2 ENGINE STATUS AS SEEN BY THE BACKEND (includes AST via its bridge when reachable)")
    rows = (get("/api/engines/status") or {}).get("engines") or []
    for r in rows:
        print("  %-34s family=%-12s state=%-14s last_update=%s reason=%s" % (
            r.get("label"), r.get("family"), r.get("state"), r.get("last_update"), (r.get("state_reason") or "")[:70]))
    print("  NOTE: AST runs on a separate host; from here only its bridge status is visible.")


MCX = {}


def s_mcx():
    show("3 MCX FUTURES: FEED, CONTRACT, SESSION, WARM-UP, SIGNAL, EXECUTION STATE")
    st = get("/api/mcx-futures/status")
    for m in st.get("instruments", []):
        MCX[m["instrument"]] = m
        i = m["instrument"]
        f = m.get("feed") or {}
        print("\n  -- %s | state=%s (%s) | execution=%s (%s)" % (i, m.get("state"), m.get("state_reason"),
                                                              m.get("execution_state"), m.get("execution_reason")))
        print("  session :", cj(m.get("session")))
        print("  feed    : last_quote_at=%s observed_at=%s age_s=%s stale=%s bid=%s ask=%s ltp=%s spread_ticks=%s" % (
            f.get("last_quote_at"), f.get("observed_at"), f.get("age_seconds"), f.get("stale"), f.get("bid"),
            f.get("ask"), f.get("ltp"), f.get("spread_ticks")))
        print("  contract:", cj(m.get("contract")))
        print("  warm-up :", cj(m.get("warmup"), 1500))
        print("  signal  :", cj(m.get("last_decision"), 1500))
        print("  risk    :", cj(m.get("risk")))
        print("  today   : trades=%s realized_pnl_today=%s rejected_today=%s last_rejection=%s open=%s unresolved=%s" % (
            m.get("trades_today"), m.get("realized_pnl_today"), m.get("entries_rejected_today"),
            cj(m.get("last_rejection"), 300), m.get("open_position_count"), m.get("unresolved_exposure_count")))
        check("%s execution_state is PAPER_ENABLED (persisted by the running worker)" % i,
              m.get("execution_state") == "PAPER_ENABLED", "-> %s" % m.get("execution_state"))


def s_decisions():
    show("4 RECENT DECISIONS, BLOCKERS AND REJECTION REASONS (last 200 journal decision events per instrument)")
    for i in INST:
        items = (get("/api/mcx-futures/decisions?instrument=%s&limit=200" % i) or {}).get("items") or []
        by_status, by_veto = {}, {}
        for d in items:
            by_status[d.get("status") or d.get("type")] = by_status.get(d.get("status") or d.get("type"), 0) + 1
            for v in d.get("vetoes") or []:
                by_veto[v] = by_veto.get(v, 0) + 1
        print("\n  -- %s: %d events | by status: %s" % (i, len(items), cj(by_status, 500)))
        print("     blockers/vetoes (count): %s" % cj(sorted(by_veto.items(), key=lambda x: -x[1])[:12], 900))
        for d in items[:4]:
            print("     latest: at=%s status=%s dir=%s score=%s cycle=%s/%s vetoes=%s unavailable=%s" % (
                d.get("at"), d.get("status"), d.get("direction"), d.get("score"), d.get("cycle_id"),
                d.get("cycle_state"), cj(d.get("vetoes"), 200), cj(d.get("unavailable_inputs"), 160)))
        rej = [t for t in (get("/api/mcx-futures/trades?instrument=%s&limit=200" % i) or {}).get("items") or []
               if t.get("state") == "ENTRY_REJECTED"]
        print("     entry-rejected trade records: %d | latest reasons: %s" % (
            len(rej), cj([r.get("rejection_reason") for r in rej[:5]], 400)))


def s_config():
    show("5 EFFECTIVE ENTRY / EXIT PARAMETERS (deployed config; hash compared with what the running worker persisted)")
    for eid in ("mcx-crudeoilm", "mcx-natgasmini", "nifty-acts_v3"):
        c = (get("/api/portfolio/strategies/" + eid) or {}).get("config") or {}
        print("\n  -- %s | source: %s" % (eid, c.get("source")))
        if eid.startswith("mcx"):
            print("  config_id=%s calibration=%s persisted_hash=%s matches_running_worker=%s" % (
                c.get("config_id"), c.get("calibration_status"), c.get("persisted_config_hash"),
                c.get("matches_running_worker")))
            check("%s deployed config matches the hash the running worker persisted" % eid,
                  c.get("matches_running_worker") is True)
            print("  parameters:", cj(c.get("parameters"), 4000))
            check("%s calibration status is PRE_REGISTERED_UNVALIDATED" % eid,
                  c.get("calibration_status") == "PRE_REGISTERED_UNVALIDATED", "-> %s" % c.get("calibration_status"))
        else:
            print("  effective:", cj(c.get("effective"), 2500))
            print("  exit_policy:", cj(c.get("exit_policy"), 800))


def s_orders():
    show("6 PAPER ORDERS, FILLS, POSITIONS (simulated; broker order id must always be null)")
    o = get("/api/portfolio/orders?limit=200")
    items = o.get("items") or []
    print("  orders total=%s status counts=%s" % (o.get("total"), cj(o.get("status_counts"), 400)))
    check("every order is simulated with a null broker order id",
          all(x.get("simulated") is True and x.get("broker_order_id") is None for x in items))
    for eid in ("mcx-crudeoilm", "mcx-natgasmini"):
        mine = [x for x in items if x.get("engine_id") == eid]
        print("  %-16s orders=%d pending=%d" % (eid, len(mine), sum(1 for x in mine if x["status"] in ("PENDING", "PARTIALLY_FILLED"))))
        for x in mine[:5]:
            print("     %s %s %s qty=%s/%s px=%s status=%s %s" % (x.get("submitted_at"), x.get("intent"), x.get("side"),
                  x.get("filled_quantity"), x.get("requested_quantity"), x.get("avg_fill_price"), x.get("status"),
                  x.get("rejection_reason") or ""))
    f = get("/api/portfolio/fills?limit=200")
    print("  fills total=%s" % f.get("total"))
    for tab in ("open", "unresolved", "closed"):
        p = get("/api/portfolio/positions?tab=%s&limit=200" % tab)
        print("  positions tab=%-10s total=%s tab_counts=%s" % (tab, p.get("total"), cj(p.get("tab_counts"), 200)))
    p = get("/api/portfolio/positions?tab=open&limit=200")
    for x in p.get("items") or []:
        print("     OPEN %s %s %s entry=%s mark=%s unrealized=%s flags=%s" % (x.get("engine_id"), x.get("contract"),
              x.get("direction"), x.get("entry_price"), x.get("mark_price"), x.get("unrealized_gross"), x.get("flags")))


def s_pnl():
    show("7 REALIZED / UNREALIZED P&L (authoritative read model)")
    for label, q in (("TODAY", ""), ("ALL TIME", "?from=2000-01-01&to=" + today)):
        s = get("/api/portfolio/summary" + q)
        print("  %-8s window=%s" % (label, cj(s.get("window"), 120)))
        print("           pnl=%s" % cj({k: (s.get("pnl") or {}).get(k) for k in (
            "realized_gross", "realized_net", "unrealized_gross", "incurred_charges", "total_net_pnl", "partial",
            "excluded_open_positions", "excluded_unpriced_closed")}, 600))
        print("           counts=%s" % cj(s.get("counts"), 400))
    for eid in ("nifty-acts_v3", "mcx-crudeoilm", "mcx-natgasmini"):
        s = get("/api/portfolio/summary?engine=%s&from=2000-01-01&to=%s" % (eid, today))
        print("  %-16s all-time realized net=%s gross=%s closed=%s | today unrealized=%s" % (
            eid, (s.get("pnl") or {}).get("realized_net"), (s.get("pnl") or {}).get("realized_gross"),
            (s.get("counts") or {}).get("closed_trades"),
            (get("/api/portfolio/summary?engine=%s" % eid).get("pnl") or {}).get("unrealized_gross")))
    src = get("/api/portfolio/sources")
    print("  charge model:", cj(src.get("charge_model"), 500), "| brokerage/order:", src.get("brokerage_per_order"))


def s_reconcile():
    show("8 RECONCILIATION: authoritative journals (raw DB) vs the portfolio APIs")
    import json as _j
    from sqlalchemy import select
    from app.acts_paper.models import ActsPaperTrade
    from app.db.session import get_session_factory
    from app.mcx_futures.models import McxFuturesTrade
    from app.mcx_futures.store import verify_chain
    sess = get_session_factory()
    with sess() as s:
        acts = [(r.state, _j.loads(r.document)) for r in s.scalars(select(ActsPaperTrade)).all()]
        mcx = [(r.instrument, r.state, _j.loads(r.document)) for r in s.scalars(select(McxFuturesTrade)).all()]
    api_all = get("/api/portfolio/trades?from=2000-01-01&to=%s&limit=200" % today)
    pos = {t: get("/api/portfolio/positions?tab=%s&limit=200" % t) for t in ("open", "closed", "unresolved")}
    acts_closed = [d for st, d in acts if st == "CLOSED"]
    print("  NIFTY ACTS journal rows: %s" % cj({k: sum(1 for st, _ in acts if st == k) for k in {st for st, _ in acts}}, 300))
    api_acts_closed = [t for t in (api_all.get("items") or []) if str(t.get("engine", "")).startswith("nifty")]
    check("NIFTY closed trade count: journal vs trades API (first 200 rows)",
          len(acts_closed) == sum(1 for p in (pos["closed"].get("items") or []) if p.get("engine_id", "").startswith("nifty"))
          or pos["closed"].get("total", 0) > 200, "journal=%d api_positions_closed_total=%s" % (len(acts_closed), pos["closed"].get("total")))
    eng_gross = round(sum(float(((d.get("final_outcome") or {}).get("gross_rupees")) or 0) for d in acts_closed), 2)
    port_gross = round(sum(float(((get("/api/portfolio/summary?engine=%s&from=2000-01-01&to=%s" % (e, today)).get("pnl") or {})
                                  .get("realized_gross")) or 0) for e in ("nifty-acts_v3", "nifty-acts_v2")), 2)
    check("NIFTY ACTS realized gross: engine-recorded journal sum (V3+V2) vs portfolio recomputed from fills",
          abs(eng_gross - port_gross) < 0.05, "journal=%.2f portfolio=%.2f" % (eng_gross, port_gross))
    for i in INST:
        docs = [(st, d) for ins, st, d in mcx if ins == i]
        closed = [d for st, d in docs if st == "CLOSED"]
        eng = round(sum(float(d.get("gross_pnl") or 0) for d in closed), 2)
        s = get("/api/portfolio/summary?engine=mcx-%s&from=2000-01-01&to=%s" % (i.lower(), today))
        api = (s.get("pnl") or {}).get("realized_gross")
        print("  %-10s journal states=%s" % (i, cj({k: sum(1 for st, _ in docs if st == k) for k in {st for st, _ in docs}}, 300)))
        check("%s closed-trade count: journal vs summary" % i, len(closed) == (s.get("counts") or {}).get("closed_trades"),
              "journal=%d api=%s" % (len(closed), (s.get("counts") or {}).get("closed_trades")))
        check("%s realized gross: journal vs portfolio" % i, api is not None and abs(float(api) - eng) < 0.02,
              "journal=%.2f api=%s" % (eng, api))
        open_n = sum(1 for st, _ in docs if st == "OPEN")
        check("%s open positions: journal vs API" % i, open_n == sum(1 for p in (pos["open"].get("items") or []) if p.get("instrument") == i),
              "journal=%d" % open_n)
        v = verify_chain(sess, i)
        check("%s hash-chained journal verifies" % i, v.get("ok") is True, cj(v, 160))
    alerts = get("/api/portfolio/alerts")
    disc = [a for a in alerts.get("items", []) if a.get("code") == "RECONCILIATION_DISCREPANCY"]
    check("no RECONCILIATION_DISCREPANCY alerts (recomputed gross == engine record)", not disc, "count=%d" % len(disc))


def s_consistency():
    show("9 CROSS-SURFACE CONSISTENCY (the numbers Overview, Positions, Orders, Tradebook, Analytics read from)")
    for label, q in (("today", "from=%s&to=%s" % (today, today)), ("all time", "from=2000-01-01&to=" + today)):
        s = get("/api/portfolio/summary?" + q)
        t = get("/api/portfolio/trades?limit=200&" + q)
        perf = get("/api/portfolio/performance?" + q)
        pc = get("/api/portfolio/positions?tab=closed&limit=200&" + q)
        net = (s.get("pnl") or {}).get("realized_net")
        n = (s.get("counts") or {}).get("closed_trades")
        pnet = round(sum(float(x.get("realized_net") or 0) for x in pc.get("items") or []), 2)
        print("  [%s] summary net=%s gross=%s closed=%s" % (label, net, (s.get("pnl") or {}).get("realized_gross"), n))
        check("%s: tradebook totals net == summary realized_net (Overview / Tradebook)" % label,
              abs(float((t.get("totals") or {}).get("net_pnl", 1e9)) - float(net or 0)) < 0.015,
              "tradebook=%s" % (t.get("totals") or {}).get("net_pnl"))
        check("%s: tradebook trade count == summary closed trades" % label, (t.get("totals") or {}).get("trades") == n)
        if (pc.get("total") or 0) <= 200:
            check("%s: sum of Positions(closed) net == summary realized_net" % label, abs(pnet - float(net or 0)) < 0.015,
                  "positions=%s" % pnet)
        else:
            print("  (positions closed list has %s rows > 200 page: sum check skipped for %s)" % (pc.get("total"), label))
        cum = (perf.get("summary") or perf.get("overall") or {})
        print("  [%s] analytics headline keys: %s" % (label, cj({k: cum.get(k) for k in list(cum)[:8]}, 400)))
    cards = (get("/api/portfolio/strategies") or {}).get("items") or []
    s = get("/api/portfolio/summary?from=%s&to=%s" % (today, today))
    tot = round(sum(float((c.get("today") or {}).get("realized_net") or 0) for c in cards), 2)
    check("sum of strategy cards (today) == Overview realized_net (today)", abs(tot - float((s.get("pnl") or {}).get("realized_net") or 0)) < 0.015,
          "cards=%s overview=%s" % (tot, (s.get("pnl") or {}).get("realized_net")))


def s_exposure():
    show("10 UNRESOLVED EXPOSURE, STALE MARKS, ALERTS")
    a = get("/api/portfolio/alerts")
    print("  critical=%s warning=%s" % (a.get("critical"), a.get("warning")))
    for x in a.get("items", [])[:12]:
        print("   ", x.get("severity"), x.get("code"), x.get("engine"), "|", (x.get("message") or "")[:140])
    u = get("/api/portfolio/positions?tab=unresolved&limit=200")
    print("  unresolved positions listed:", u.get("total"))
    for x in u.get("items") or []:
        print("    %s %s status=%s flags=%s mark_fresh_unrealized=%s entry=%s" % (x.get("engine_id"), x.get("contract"),
              x.get("status"), x.get("flags"), x.get("unrealized_gross"), x.get("entry_at")))
    p = (get("/api/portfolio/summary") or {}).get("pnl") or {}
    print("  summary partial=%s excluded_open=%s excluded_unpriced_closed=%s" % (p.get("partial"),
          p.get("excluded_open_positions"), p.get("excluded_unpriced_closed")))
    print("  (the expired NIFTY 25600/26000 position is expected here as EXPIRED_NOT_SETTLED until the worker terminalises it)")


for name, fn in (("header", s_header), ("engines", s_engines), ("mcx", s_mcx), ("decisions", s_decisions),
                 ("config", s_config), ("orders", s_orders), ("pnl", s_pnl), ("reconcile", s_reconcile),
                 ("consistency", s_consistency), ("exposure", s_exposure)):
    section(name, fn)

print("\n==== PYTHON CHECKS: %s" % ("ALL PASS" if not FAIL else "FAILED -> " + "; ".join(FAIL)))

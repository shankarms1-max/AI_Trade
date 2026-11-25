FAILS=0; SHA7=2438b3b
fail() { echo "FAIL $1"; FAILS=$((FAILS+1)); }
echo "################ A. HOST: time, CPU, RAM, swap, disk ################"
echo "now: $(date -Is) | IST: $(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S')"; uptime; free -m; df -h / | tail -1
echo "-- per-container CPU / memory"; docker stats --no-stream --format '{{.Name}}  cpu={{.CPUPerc}}  mem={{.MemUsage}}' | sort
echo "-- docker disk (read-only)"; docker system df
echo "################ B. CONTAINERS: health, command, what changed, what did NOT ################"
DEPLOY_T=$(docker inspect -f '{{.State.FinishedAt}}' ai_trade-backend-1-pre-$SHA7 2>/dev/null); echo "deploy began (old backend stopped at): $DEPLOY_T"
for c in ai_trade-backend-1 ai_trade-frontend-1 mcx-futures-crudeoilm mcx-futures-natgasmini acts-paper ai_trade-postgres-1 ai_trade-caddy-1 scalper-research-collector; do echo "$c | $(docker inspect -f 'image={{.Config.Image}} id={{printf "%.19s" .Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} started={{.State.StartedAt}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' $c 2>&1)"; done
for c in ai_trade-backend-1 ai_trade-frontend-1 mcx-futures-crudeoilm mcx-futures-natgasmini; do h=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $c 2>&1); [ "$h" = running/healthy ] && echo "PASS $c is running/healthy" || fail "$c is $h (expected running/healthy)"; done
for c in mcx-futures-crudeoilm mcx-futures-natgasmini; do cmd=$(docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' $c 2>&1); case "$cmd" in *mcx_futures_paper_worker.py*--mode\ PAPER*) echo "PASS $c runs the worker: $cmd";; *) fail "$c runs [$cmd], not the MCX worker";; esac; echo "     process: $(docker top $c -o args 2>/dev/null | tail -n +2 | tr '\n' ' ' | cut -c1-150)"; done
ST=$(docker inspect -f '{{.State.Status}}' acts-paper 2>/dev/null); [ "$ST" = running ] && echo "PASS acts-paper is running" || fail "acts-paper is $ST"
echo "-- unrelated containers must have started BEFORE the deploy began, with the same image as the preflight"
for c in acts-paper ai_trade-postgres-1 ai_trade-caddy-1 scalper-research-collector; do ST=$(docker inspect -f '{{.State.StartedAt}}' $c 2>/dev/null); if [ -n "$DEPLOY_T" ] && [ -n "$ST" ] && [ "$ST" \< "$DEPLOY_T" ]; then echo "PASS $c not restarted by the deploy (started $ST)"; else fail "$c started $ST vs deploy $DEPLOY_T"; fi; done
ID=$(docker inspect -f '{{.Image}}' acts-paper 2>/dev/null); case "$ID" in sha256:6dcba18ec164*) echo "PASS acts-paper image unchanged since the preflight (6dcba18ec164, ai-trade-acts-v3:4cd0a3b)";; *) fail "acts-paper image is now $ID";; esac
echo "-- rollback copies kept"; docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' | grep -E -- "-(pre|badcmd)-$SHA7"
echo "################ C. WORKER LIFECYCLE EVIDENCE ################"
for c in mcx-futures-crudeoilm mcx-futures-natgasmini; do echo "-- $c env flags: $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $c | grep -E '^MCX_FUTURES_(MODE|PAPER_ENABLED|TRADING_ENABLED|CRUDEOILM_ENABLED|NATGASMINI_ENABLED)=' | tr '\n' ' ')"; echo "   log lines last 30 min: $(docker logs --since 30m $c 2>&1 | wc -l) | error/traceback lines: $(docker logs --since 30m $c 2>&1 | grep -ciE 'error|traceback|exception')"; echo "   latest log lines (secrets filtered):"; docker logs --tail 6 $c 2>&1 | grep -viE 'key|token|secret|password|authorization' | cut -c1-230 | sed 's/^/     /'; done
echo "-- NIFTY ACTS V3 (acts-paper): errors in last 30 min = $(docker logs --since 30m acts-paper 2>&1 | grep -ciE 'error|traceback|exception')"; docker logs --tail 5 acts-paper 2>&1 | grep -viE 'key|token|secret|password|authorization' | cut -c1-230 | sed 's/^/     /'
echo "################ D. DATABASE: journal cursors ################"
docker exec ai_trade-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select '"'"'NIFTY observed_at='"'"'||observed_at from acts_paper_cursor; select '"'"'NIFTY '"'"'||state||'"'"'='"'"'||count(*) from acts_paper_trades group by state order by state; select instrument||'"'"' seq='"'"'||sequence||'"'"' obs='"'"'||coalesce(observed_at,'"'"'-'"'"') from mcx_futures_cursors; select instrument||'"'"' '"'"'||state||'"'"'='"'"'||count(*) from mcx_futures_trades group by instrument,state order by 1; select instrument||'"'"' paper_entries_enabled='"'"'||coalesce(latest_json::json->>'"'"'paper_entries_enabled'"'"','"'"'MISSING'"'"')||'"'"' execution_state='"'"'||coalesce(latest_json::json->>'"'"'execution_state'"'"','"'"'MISSING'"'"') from mcx_futures_cursors order by 1;"'
echo "################ E. APPLICATION-LEVEL VERIFICATION (read-only; takes ~1-2 min; exits non-zero on any required failure) ################"
docker exec -i -w /app ai_trade-backend-1 python - <<'PY'
"""READ-ONLY production verification. Runs inside the backend container (python - < this file) or locally.
Issues only HTTP GETs against the backend itself and SELECT-only reads of the journals. Changes nothing.
Exit status is NON-ZERO when any required check fails. Environment: VERIFY_BASE, VERIFY_ADVANCE_WAIT (seconds)."""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
ADVANCE_WAIT = int(os.environ.get("VERIFY_ADVANCE_WAIT", "45"))
IST = timezone(timedelta(hours=5, minutes=30))
FAIL, API_ERRORS = [], []
INST = ("CRUDEOILM", "NATGASMINI")
ENGINE_IDS = ("nifty-acts_v3", "mcx-crudeoilm", "mcx-natgasmini")
PRECISION = 0.02          # rupees: financial equality tolerance


def get(path, timeout=90):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=timeout))
    except Exception as exc:  # noqa: BLE001
        API_ERRORS.append("%s -> %s %s" % (path, type(exc).__name__, str(exc)[:100]))
        return {"_error": str(exc)[:100]}


def pages(path, limit=200):
    """Complete pagination: follow offsets until every row has been read; returns (rows, total)."""
    rows, offset, total = [], 0, None
    sep = "&" if "?" in path else "?"
    while True:
        page = get("%s%slimit=%d&offset=%d" % (path, sep, limit, offset))
        if "_error" in page:
            return rows, None
        rows += page.get("items") or []
        total = page.get("total", len(rows))
        offset += limit
        if offset >= total or not page.get("items"):
            return rows, total


def show(title):
    print("\n==== " + title)


def cj(value, limit=900):
    text = json.dumps(value, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "...(+%d chars)" % (len(text) - limit)


def check(label, ok, detail=""):
    print("  %-4s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        FAIL.append(label)


def skip(label, why):
    print("  SKIP %s (%s)" % (label, why))


def money(value, label):
    """A required monetary value: None is a FAILURE, never silently zero."""
    if value is None or isinstance(value, bool):
        check("required value present: " + label, False, "-> missing")
        return None
    return float(value)


def close(a, b, tol=PRECISION):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def section(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print("  !! section '%s' crashed: %s %s" % (name, type(exc).__name__, str(exc)[:200]))
        FAIL.append("section:" + name)


now_ist = datetime.now(IST)
today = str(now_ist.date())
HDR = get("/api/portfolio/header")
MCX_OPEN = (HDR.get("mcx") or {}).get("status") == "OPEN"
NSE_OPEN = (HDR.get("nse") or {}).get("status") == "OPEN"
SESSION = "MCX %s / NSE %s" % ("OPEN" if MCX_OPEN else "closed", "OPEN" if NSE_OPEN else "closed")


def sample():
    """SELECT-only: journal cursors (the authoritative heartbeat)."""
    from sqlalchemy import select
    from app.acts_paper.models import ActsPaperCursor
    from app.db.session import get_session_factory
    from app.mcx_futures.models import McxFuturesCursor
    with get_session_factory()() as s:
        out = {c.instrument: (c.sequence, c.observed_at) for c in s.scalars(select(McxFuturesCursor)).all()}
        a = s.get(ActsPaperCursor, 1)
        out["NIFTY"] = (None, a.observed_at if a else None)
    return out


try:
    SAMPLE_A, SAMPLE_A_AT = sample(), time.time()
except Exception as exc:  # noqa: BLE001
    SAMPLE_A, SAMPLE_A_AT = None, time.time()
    API_ERRORS.append("cursor sample failed: %s" % exc)


def s_header():
    show("1 CLOCK, SESSIONS, CONNECTIVITY (inferred), FEED ACCOUNTING   [session: %s]" % SESSION)
    h = HDR
    print("  now IST:", now_ist.isoformat(timespec="seconds"), "| NSE:", (h.get("nse") or {}).get("status"), "| MCX:", cj(h.get("mcx")))
    print("  broker connectivity:", cj(h.get("broker_connectivity")))
    f = h.get("data_freshness") or {}
    print("  freshness:", cj({k: f.get(k) for k in ("fresh_feeds", "stale_feeds", "unavailable_feeds", "market_closed_feeds", "feeds", "inactive_feeds")}),
          "| critical:", h.get("critical_alerts"), "| warning:", h.get("warning_alerts"))
    for x in f.get("by_feed") or []:
        print("     feed %-30s state=%-16s -> %-13s %s" % (x.get("label"), x.get("state"), x.get("classification"), x.get("reason") or ""))
    check("header is complete (no API error)", "_error" not in h and bool(f))
    check("live execution is NONE", h.get("live_execution") == "NONE")
    parts = [f.get(k) for k in ("fresh_feeds", "stale_feeds", "unavailable_feeds", "market_closed_feeds")]
    check("every active feed is accounted for exactly once (fresh+stale+unavailable+closed == feeds)",
          None not in parts and sum(parts) == f.get("feeds"), "%s vs %s" % (parts, f.get("feeds")))
    live = sum(p for p in parts[:3] if p is not None)
    if live and (parts[0] or 0) < live:
        check("a degraded in-session feed is never reported as plain CONNECTED",
              (h.get("broker_connectivity") or {}).get("state") != "CONNECTED_INFERRED", "-> %s" % (h.get("broker_connectivity") or {}).get("state"))


def s_engines():
    show("2 EXPECTED ENGINES PRESENT (NIFTY ACTS V3, CRUDEOILM, NATGASMINI); AST is not visible from this host")
    rows = (get("/api/engines/status") or {}).get("engines") or []
    for r in rows:
        print("  %-34s family=%-12s state=%-16s last_update=%s reason=%s" % (r.get("label"), r.get("family"), r.get("state"),
              r.get("last_update"), (r.get("state_reason") or "")[:60]))
    for needle in ("NIFTY ACTS V3", "CRUDEOILM", "NATGASMINI"):
        check("engine present in /api/engines/status: " + needle, any(needle in (r.get("label") or "") for r in rows))
    st = get("/api/mcx-futures/status").get("instruments") or []
    for i in INST:
        check("MCX instrument present in /api/mcx-futures/status: " + i, any(m.get("instrument") == i for m in st))
    print("  NOTE: AST runs on a separate host; only its bridge status is visible here (reported, not verified).")


def s_mcx():
    show("3 MCX FUTURES: FEED, CONTRACT, SESSION, WARM-UP, SIGNAL, EXECUTION   [session: %s]" % SESSION)
    for m in get("/api/mcx-futures/status").get("instruments", []):
        i = m["instrument"]; f = m.get("feed") or {}; w = m.get("warmup") or {}; d = m.get("last_decision") or {}
        ss = m.get("session") or {}
        print("\n  -- %s | state=%s (%s) | execution=%s (%s)" % (i, m.get("state"), m.get("state_reason"), m.get("execution_state"), m.get("execution_reason")))
        print("  session (current): %s" % cj(ss))
        print("  last observed session (history): %s" % cj(m.get("last_observed_session"), 400))
        print("  feed    : last_quote_at=%s observed_at=%s age_s=%s source_ts_available=%s bid=%s ask=%s ltp=%s" % (
            f.get("last_quote_at"), f.get("observed_at"), f.get("age_seconds"), f.get("source_timestamp_available"), f.get("bid"), f.get("ask"), f.get("ltp")))
        print("  contract:", cj(m.get("contract")))
        print("  warm-up :", cj(w, 600))
        print("  signal  :", cj({k: d.get(k) for k in ("at", "status", "direction", "score", "cycle_state", "vetoes", "warm")}, 700))
        print("  risk    :", cj(m.get("risk")))
        print("  today   : trades=%s rejected_today=%s last_rejection=%s open=%s unresolved=%s" % (
            m.get("trades_today"), m.get("entries_rejected_today"), cj(m.get("last_rejection"), 300), m.get("open_position_count"), m.get("unresolved_exposure_count")))
        check("%s runtime execution_state is PAPER_ENABLED (persisted by the running worker)" % i, m.get("execution_state") == "PAPER_ENABLED", "-> %s" % m.get("execution_state"))
        check("%s session metadata is current (not a previous day)" % i, (ss.get("source") == "CALENDAR_NOW") and (not ss.get("last_entry_time") or ss["last_entry_time"][:10] == ss.get("session_date")))
        if MCX_OPEN:
            check("%s [SESSION OPEN] state is RUNNING" % i, m.get("state") == "RUNNING", "-> %s %s" % (m.get("state"), m.get("state_reason")))
            check("%s [SESSION OPEN] source quote timestamp present" % i, f.get("last_quote_at") is not None)
            check("%s [SESSION OPEN] quote age under 90 s" % i, f.get("age_seconds") is not None and f["age_seconds"] < 90, "-> %s" % f.get("age_seconds"))
            check("%s [SESSION OPEN] a contract is subscribed" % i, bool((m.get("contract") or {}).get("token")))
            check("%s [SESSION OPEN] warm-up information reported" % i, bool(w) and "bars_available" in w)
            check("%s [SESSION OPEN] a decision was evaluated today" % i, bool(d.get("at")) and str(d["at"])[:10] == today, "at=%s" % d.get("at"))
            check("%s [SESSION OPEN] session_date is today" % i, ss.get("session_date") == today, "-> %s" % ss.get("session_date"))
        else:
            skip("%s feed freshness / decision checks" % i, "MCX session closed; only the closed-session state is asserted")
            check("%s [SESSION CLOSED] state is MARKET_CLOSED" % i, m.get("state") == "MARKET_CLOSED", "-> %s" % m.get("state"))
        c = (get("/api/portfolio/strategies/mcx-" + i.lower()).get("config") or {})
        check("%s deployed config hash == hash persisted by the worker" % i, c.get("matches_running_worker") is True,
              "deployed=%s persisted=%s" % (c.get("config_id"), (c.get("persisted_config_hash") or "")[:12]))
        check("%s calibration status is PRE_REGISTERED_UNVALIDATED" % i, c.get("calibration_status") == "PRE_REGISTERED_UNVALIDATED")


def s_advance():
    show("13 HEARTBEAT: the journal cursors must ADVANCE over a real observation interval   [session: %s]" % SESSION)
    wait = ADVANCE_WAIT - (time.time() - SAMPLE_A_AT)
    if wait > 0 and (MCX_OPEN or NSE_OPEN):
        time.sleep(wait)
    b = sample()
    for i in INST:
        a_seq, a_obs = (SAMPLE_A or {}).get(i, (None, None)); b_seq, b_obs = b.get(i, (None, None))
        print("  %-10s sequence %s -> %s | observed_at %s -> %s" % (i, a_seq, b_seq, a_obs, b_obs))
        if MCX_OPEN:
            check("%s [SESSION OPEN] journal sequence advanced" % i, a_seq is not None and b_seq is not None and b_seq > a_seq)
            check("%s [SESSION OPEN] observed_at advanced" % i, a_obs is not None and b_obs is not None and b_obs > a_obs)
        else:
            skip("%s cursor advance" % i, "MCX session closed")
    a_n, b_n = (SAMPLE_A or {}).get("NIFTY", (None, None))[1], b.get("NIFTY", (None, None))[1]
    print("  %-10s observed_at %s -> %s" % ("NIFTY ACTS", a_n, b_n))
    if NSE_OPEN:
        check("NIFTY ACTS [SESSION OPEN] observed_at advanced", a_n is not None and b_n is not None and b_n > a_n)
    else:
        skip("NIFTY ACTS cursor advance", "NSE closed")


def s_decisions():
    show("5 DECISIONS, BLOCKERS AND REJECTION REASONS (last 200 journal decision events per instrument)")
    for i in INST:
        items = (get("/api/mcx-futures/decisions?instrument=%s&limit=200" % i) or {}).get("items") or []
        by_status, by_veto = {}, {}
        for d in items:
            k = d.get("status") or d.get("type"); by_status[k] = by_status.get(k, 0) + 1
            for v in d.get("vetoes") or []:
                by_veto[v] = by_veto.get(v, 0) + 1
        print("\n  -- %s: %d events | by status: %s" % (i, len(items), cj(by_status, 500)))
        print("     blockers (count): %s" % cj(sorted(by_veto.items(), key=lambda x: -x[1])[:12], 900))
        for d in items[:3]:
            print("     latest: at=%s status=%s dir=%s score=%s cycle=%s/%s vetoes=%s" % (d.get("at"), d.get("status"), d.get("direction"), d.get("score"), d.get("cycle_id"), d.get("cycle_state"), cj(d.get("vetoes"), 200)))
        rej = [t for t in (get("/api/mcx-futures/trades?instrument=%s&limit=200" % i) or {}).get("items") or [] if t.get("state") == "ENTRY_REJECTED"]
        print("     entry-rejected trade records: %d | latest reasons: %s" % (len(rej), cj([r.get("rejection_reason") for r in rej[:5]], 400)))


def s_config():
    show("6 EFFECTIVE ENTRY / EXIT PARAMETERS")
    for eid in ("mcx-crudeoilm", "mcx-natgasmini", "nifty-acts_v3"):
        c = (get("/api/portfolio/strategies/" + eid) or {}).get("config") or {}
        check("parameters available for " + eid, bool(c.get("available")))
        print("\n  -- %s | source: %s" % (eid, c.get("source")))
        if eid.startswith("mcx"):
            print("  config_id=%s calibration=%s" % (c.get("config_id"), c.get("calibration_status")))
            print("  parameters:", cj(c.get("parameters"), 4000))
        else:
            print("  effective:", cj(c.get("effective"), 2500))
            print("  exit_policy:", cj(c.get("exit_policy"), 800))


def s_orders():
    show("7 PAPER ORDERS, FILLS, POSITIONS (complete pagination; broker order id must always be null)")
    orders, total = pages("/api/portfolio/orders")
    check("all orders read (complete pagination)", total is not None and len(orders) == total, "read=%d total=%s" % (len(orders), total))
    check("every order is simulated with a null broker order id", all(x.get("simulated") is True and x.get("broker_order_id") is None for x in orders))
    by = {}
    for x in orders:
        by[x["status"]] = by.get(x["status"], 0) + 1
    print("  orders=%d status counts=%s" % (len(orders), cj(by, 300)))
    for eid in ("mcx-crudeoilm", "mcx-natgasmini"):
        mine = [x for x in orders if x.get("engine_id") == eid]
        print("  %-16s orders=%d pending=%d" % (eid, len(mine), sum(1 for x in mine if x["status"] in ("PENDING", "PARTIALLY_FILLED"))))
        for x in mine[:5]:
            print("     %s %s %s qty=%s/%s px=%s status=%s %s" % (x.get("submitted_at"), x.get("intent"), x.get("side"), x.get("filled_quantity"), x.get("requested_quantity"), x.get("avg_fill_price"), x.get("status"), x.get("rejection_reason") or ""))
    fills, ft = pages("/api/portfolio/fills")
    check("all fills read", ft is not None and len(fills) == ft, "read=%d total=%s" % (len(fills), ft))
    check("fills == FILLED orders", len(fills) == by.get("FILLED", 0), "fills=%d filled_orders=%d" % (len(fills), by.get("FILLED", 0)))
    for tab in ("open", "unresolved", "closed"):
        rows, t = pages("/api/portfolio/positions?tab=%s" % tab)
        check("positions tab %s fully read" % tab, t is not None and len(rows) == t, "read=%d total=%s" % (len(rows), t))
        print("  positions tab=%-10s total=%s" % (tab, t))
    for x in pages("/api/portfolio/positions?tab=open")[0]:
        print("     OPEN %s %s %s entry=%s mark=%s unrealized=%s flags=%s" % (x.get("engine_id"), x.get("contract"), x.get("direction"), x.get("entry_price"), x.get("mark_price") or (x.get("mark") or {}).get("price"), x.get("unrealized_gross"), x.get("flags")))


def s_pnl():
    show("8 REALIZED / UNREALIZED P&L (authoritative read model; missing values are failures, not zeros)")
    for label, q in (("TODAY", ""), ("ALL TIME", "?from=2000-01-01&to=" + today)):
        s = get("/api/portfolio/summary" + q); p = s.get("pnl") or {}
        for k in ("realized_gross", "realized_net", "incurred_charges", "total_net_pnl"):
            money(p.get(k), "%s %s" % (label, k))
        print("  %-8s window=%s" % (label, cj(s.get("window"), 100)))
        print("           pnl=%s" % cj({k: p.get(k) for k in ("realized_gross", "realized_net", "unrealized_gross", "incurred_charges", "charges_on_closed_trades", "charges_not_in_closed_trades", "total_net_pnl", "partial", "excluded_open_positions", "excluded_unpriced_closed", "excluded_unpriced_exposure")}, 800))
        print("           counts=%s" % cj(s.get("counts"), 500))
        if p.get("unrealized_gross") is None:
            print("           (unrealized is null: no fresh executable mark; it is NOT treated as zero)")
    for eid in ENGINE_IDS:
        s = get("/api/portfolio/summary?engine=%s&from=2000-01-01&to=%s" % (eid, today)); p = s.get("pnl") or {}
        print("  %-16s all-time realized net=%s gross=%s closed=%s partial=%s" % (eid, p.get("realized_net"), p.get("realized_gross"), (s.get("counts") or {}).get("closed_trades"), p.get("partial")))
    src = get("/api/portfolio/sources")
    print("  charge model:", cj(src.get("charge_model"), 600), "| brokerage/order:", src.get("brokerage_per_order"))


def s_reconcile():
    show("9 RECONCILIATION: authoritative journals (raw DB) vs portfolio APIs; every exclusion is explained")
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
    closed_pos, ct = pages("/api/portfolio/positions?tab=closed")
    allsum = get("/api/portfolio/summary?from=2000-01-01&to=" + today)
    cnt, pnl = allsum.get("counts") or {}, allsum.get("pnl") or {}
    unpriced_pos = [p for p in closed_pos if p.get("status") == "EXPIRED_UNPRICED"]
    priced_pos = [p for p in closed_pos if p.get("status") == "CLOSED"]
    print("  Positions 'closed' tab: %s rows = %d priced CLOSED + %d EXPIRED_UNPRICED;  summary closed_trades=%s expired_unpriced=%s"
          % (ct, len(priced_pos), len(unpriced_pos), cnt.get("closed_trades"), cnt.get("expired_unpriced")))
    check("closed-tab rows == summary closed_trades + expired_unpriced (130 vs 131 is fully explained)",
          ct == (cnt.get("closed_trades") or 0) + (cnt.get("expired_unpriced") or 0), "rows=%s = %s + %s" % (ct, cnt.get("closed_trades"), cnt.get("expired_unpriced")))
    acts_closed = [d for st, d in acts if st == "CLOSED"]
    acts_unpriced = [d for d in acts_closed if d.get("expiry_terminalization") is True and d.get("final_outcome") is None]
    check("NIFTY journal CLOSED rows == priced + unpriced NIFTY positions", len(acts_closed) == sum(1 for p in closed_pos if str(p.get("engine_id", "")).startswith("nifty")),
          "journal=%d (of which %d expired unpriced)" % (len(acts_closed), len(acts_unpriced)))
    eng_gross = sum(float((d.get("final_outcome") or {}).get("gross_rupees")) for d in acts_closed if d.get("final_outcome") and (d["final_outcome"].get("gross_rupees") is not None))
    port_gross = sum(money(((get("/api/portfolio/summary?engine=%s&from=2000-01-01&to=%s" % (e, today)).get("pnl") or {}).get("realized_gross")), "gross " + e) or 0 for e in ("nifty-acts_v3", "nifty-acts_v2"))
    check("NIFTY realized gross: engine journal (priced trades) vs portfolio recomputed from fills", close(eng_gross, port_gross, 0.05), "journal=%.2f portfolio=%.2f" % (eng_gross, port_gross))
    print("  unpriced exposure is disclosed: partial=%s excluded_unpriced_exposure=%s" % (pnl.get("partial"), pnl.get("excluded_unpriced_exposure")))
    if unpriced_pos:
        check("unpriced exposure is disclosed in the totals (partial + count)", pnl.get("partial") is True and (pnl.get("excluded_unpriced_exposure") or 0) >= len(unpriced_pos))
    for i in INST:
        docs = [(st, d) for ins, st, d in mcx if ins == i]
        closed = [d for st, d in docs if st == "CLOSED"]
        s = get("/api/portfolio/summary?engine=mcx-%s&from=2000-01-01&to=%s" % (i.lower(), today))
        api = money((s.get("pnl") or {}).get("realized_gross"), i + " realized gross")
        eng = sum(float(d["gross_pnl"]) for d in closed)
        check("%s closed-trade count: journal vs summary" % i, len(closed) == (s.get("counts") or {}).get("closed_trades"), "journal=%d api=%s" % (len(closed), (s.get("counts") or {}).get("closed_trades")))
        check("%s realized gross: journal vs portfolio" % i, close(api, eng), "journal=%.2f api=%s" % (eng, api))
        open_n = sum(1 for st, _ in docs if st == "OPEN")
        opens = pages("/api/portfolio/positions?tab=open&instrument=%s" % i)[0]
        check("%s open positions: journal vs API" % i, open_n == len(opens), "journal=%d api=%d" % (open_n, len(opens)))
        v = verify_chain(sess, i)
        check("%s hash-chained journal verifies" % i, v.get("ok") is True, cj(v, 160))
        # cost trace: the engine's recorded fees vs the portfolio charges, per trade
        trades = {t["trade_id"]: t for t in pages("/api/portfolio/trades?engine=mcx-%s&from=2000-01-01&to=%s" % (i.lower(), today))[0]}
        for d in closed:
            tid = d.get("trade_id"); t = trades.get(tid)
            if t is None:
                check("%s trade %s present in the tradebook" % (i, str(tid)[:10]), False); continue
            items = (d.get("costs") or {}).get("items") or {}
            broker = float(items.get("brokerage") or 0.0)
            statutory_engine = float(d.get("fees") or 0) - broker * 1.18          # engine fees minus brokerage and its GST
            diff = float(d.get("fees") or 0) - float(t.get("charges"))
            print("     %s %s gross=%s engine_fees=%.2f (brokerage %.2f) | portfolio charges=%.2f | difference %.2f" % (i, str(tid)[:10], d.get("gross_pnl"), float(d.get("fees") or 0), broker, float(t["charges"]), diff))
            check("%s %s: statutory charges agree; the ONLY difference is the engine's brokerage (+GST)" % (i, str(tid)[:10]), close(statutory_engine, t.get("charges"), 0.05),
                  "engine statutory %.2f vs portfolio %.2f; engine brokerage %.2f" % (statutory_engine, float(t["charges"]), broker))
    disc = [a for a in get("/api/portfolio/alerts").get("items", []) if a.get("code") == "RECONCILIATION_DISCREPANCY"]
    check("no RECONCILIATION_DISCREPANCY alerts", not disc, "count=%d" % len(disc))


def s_charges():
    show("10 CHARGE SCOPES (Overview 'incurred' vs Analytics 'charges' vs gross-net)")
    s = get("/api/portfolio/summary?from=2000-01-01&to=" + today); p = s.get("pnl") or {}
    perf = (get("/api/portfolio/performance?from=2000-01-01&to=" + today).get("overall") or {})
    inc, onc, other = money(p.get("incurred_charges"), "incurred_charges"), money(p.get("charges_on_closed_trades"), "charges_on_closed_trades"), money(p.get("charges_not_in_closed_trades"), "charges_not_in_closed_trades")
    gn = None if p.get("realized_gross") is None or p.get("realized_net") is None else p["realized_gross"] - p["realized_net"]
    print("  incurred_charges=%s | closed-trade charges=%s | not in closed trades=%s | gross-net=%s | analytics charges=%s" % (inc, onc, other, gn, perf.get("charges")))
    print("  scope:", p.get("incurred_charges_scope"))
    check("gross - net == charges on closed trades", close(gn, onc))
    check("analytics charges == charges on closed trades", close(perf.get("charges"), onc))
    check("incurred == closed-trade charges + charges not in closed trades", inc is not None and close(inc, (onc or 0) + (other or 0)))


def s_consistency():
    show("11 CROSS-SURFACE CONSISTENCY (what Overview, Positions, Orders, Tradebook and Analytics read)")
    for label, q in (("today", "from=%s&to=%s" % (today, today)), ("all time", "from=2000-01-01&to=" + today)):
        s = get("/api/portfolio/summary?" + q); p = s.get("pnl") or {}
        net, n = money(p.get("realized_net"), label + " realized_net"), (s.get("counts") or {}).get("closed_trades")
        trades, tt = pages("/api/portfolio/trades?" + q)
        totals = (get("/api/portfolio/trades?limit=1&" + q).get("totals") or {})
        pc, pt = pages("/api/portfolio/positions?tab=closed&" + q)
        priced = [x for x in pc if x.get("status") == "CLOSED"]
        missing = [x for x in priced if x.get("realized_net") is None]
        pnet = sum(float(x["realized_net"]) for x in priced if x.get("realized_net") is not None)
        print("  [%s] summary net=%s closed=%s | tradebook rows=%s totals=%s | positions closed rows=%s (priced %d)" % (label, net, n, tt, cj(totals, 200), pt, len(priced)))
        check("%s: tradebook totals net == summary realized_net" % label, close(totals.get("net_pnl"), net))
        check("%s: tradebook rows == summary closed trades" % label, tt == n)
        check("%s: sum of Positions(closed, priced) net == summary realized_net" % label, close(pnet, net), "positions=%.2f" % pnet)
        check("%s: no priced closed position has a missing net value" % label, not missing, "missing=%d" % len(missing))
        perf = (get("/api/portfolio/performance?" + q).get("overall") or {})
        print("  [%s] analytics: trades=%s net=%s charges=%s" % (label, perf.get("trades"), perf.get("net_pnl"), perf.get("charges")))
        check("%s: analytics net == summary realized_net" % label, (n == 0 and not perf.get("trades")) or close(perf.get("net_pnl"), net))
    cards = (get("/api/portfolio/strategies") or {}).get("items") or []
    s = get("/api/portfolio/summary?from=%s&to=%s" % (today, today))
    parts = [money((c.get("today") or {}).get("realized_net"), "card %s today.realized_net" % c.get("engine_id")) for c in cards]
    check("sum of strategy cards (today) == Overview realized_net (today)", None not in parts and close(sum(parts), (s.get("pnl") or {}).get("realized_net")), "cards=%s" % (round(sum(p for p in parts if p is not None), 2)))


def s_exposure():
    show("12 UNRESOLVED EXPOSURE MUST BE VISIBLE EVERYWHERE UNTIL RECONCILED")
    a = get("/api/portfolio/alerts"); items = a.get("items") or []
    print("  alerts: critical=%s warning=%s" % (a.get("critical"), a.get("warning")))
    for x in items[:12]:
        print("   ", x.get("severity"), x.get("code"), x.get("engine"), "|", (x.get("message") or "")[:150])
    unresolved, ut = pages("/api/portfolio/positions?tab=unresolved")
    print("  unresolved tab: %s positions" % ut)
    alerted = {x.get("position_id") for x in items}
    for x in unresolved:
        print("    %s %s status=%s flags=%s entry=%s" % (x.get("engine_id"), x.get("contract"), x.get("status"), x.get("flags"), x.get("entry_at")))
        check("unresolved position has a global alert: %s" % x.get("contract"), x.get("position_id") in alerted)
        check("unresolved position carries explicit flags: %s" % x.get("contract"), bool(x.get("flags")))
    for label, q in (("today", ""), ("a past window", "?from=2026-01-05&to=2026-01-05"), ("all time", "?from=2000-01-01&to=" + today)):
        s = get("/api/portfolio/summary" + q); p = s.get("pnl") or {}; c = s.get("counts") or {}
        check("summary (%s) counts every unresolved position" % label, (c.get("unresolved_positions") or 0) >= len(unresolved), "summary=%s tab=%s" % (c.get("unresolved_positions"), ut))
        if any(x.get("status") == "EXPIRED_UNPRICED" for x in unresolved):
            check("summary (%s) discloses excluded unpriced exposure and is PARTIAL" % label, p.get("partial") is True and (p.get("excluded_unpriced_exposure") or 0) >= 1)
    if unresolved:
        check("header critical alert count includes unresolved exposure", (HDR.get("critical_alerts") or 0) >= 1, "critical=%s" % HDR.get("critical_alerts"))
    print("  Open NIFTY expired position is expected here (EXPIRED_UNPRICED) until reconciled from an official settlement price.")


for name, fn in (("header", s_header), ("engines", s_engines), ("mcx", s_mcx), ("decisions", s_decisions),
                 ("config", s_config), ("orders", s_orders), ("pnl", s_pnl), ("reconcile", s_reconcile),
                 ("charges", s_charges), ("consistency", s_consistency), ("exposure", s_exposure), ("advance", s_advance)):
    section(name, fn)

show("API ERRORS")
for e in API_ERRORS:
    print("  ", e)
check("no API errors or missing responses", not API_ERRORS, "count=%d" % len(API_ERRORS))
print("\n==== PYTHON CHECKS: %s" % ("ALL PASS" if not FAIL else "FAILED (%d) -> %s" % (len(FAIL), "; ".join(FAIL))))
sys.exit(1 if FAIL else 0)
PY
[ $? -eq 0 ] || fail "application-level checks failed (see FAIL lines above)"
echo "################ E2. PAPER-TRADING READINESS (warm-up, signal vs threshold, blockers, cycle, orders, exit rules) ################"
docker exec -i -w /app ai_trade-backend-1 python - <<'PY2'
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
PY2
[ $? -eq 0 ] || fail "MCX readiness checks failed (see FAIL lines above)"
echo "################ F. RESULT ################"
if [ "$FAILS" -eq 0 ]; then echo "PRODUCTION VERIFICATION: PASS"; else echo "PRODUCTION VERIFICATION: FAILED ($FAILS check group(s)) - paste this whole output"; fi
exit $FAILS

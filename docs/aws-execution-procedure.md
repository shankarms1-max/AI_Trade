# AWS execution procedure — trading terminal release `2438b3bab3e006580b2bd63da94758dfe189fef0`

Host: `ip-172-26-7-186` (16.4.18.3, user `ubuntu`). Everything below is copy-paste ready. Rules baked into every
block: no line continuations (a terminal that inserts blank lines breaks `\`); the script runs inside its own
`bash`, so a failure stops the script and never closes your SSH session; **no `docker compose`, no prune, no
`deploy.sh` / `update_server.sh`, no global restart**; the old container is always kept under a `-pre-2438b3bab3e006580b2bd63da94758dfe189fef0` name;
every swap rolls back automatically when its health or API checks fail; secrets are never printed (environment is
copied container-to-container through process substitution, and only non-secret flag NAMES/values are shown).

Untouched by every block: `acts-paper` (NIFTY ACTS V3), `ai_trade-postgres-1`, `ai_trade-caddy-1`,
`scalper-research-collector`, the AST stack (separate host), all journals and tables. Migration head stays `0025`
(already applied on 24 Nov; no migration is run here).

**Why the order matters:** the new dashboard needs the new backend (`/api/portfolio/*`); the MCX workers must be
refreshed after the backend so their status records carry warm-up and execution state. Run the blocks in order.

## Block 1 — read-only diagnostics (safe any time; run before and after)

```bash
bash <<'EOF'
echo "== time $(date -Is) | disk $(df -h / | tail -1 | awk '{print $4" free"}') | mem $(free -m | awk '/Mem/{print $7" MB available"}')"
echo "== containers"; docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' | sort
echo "== image ids of trading containers (compare before/after)"; for c in acts-paper mcx-futures-crudeoilm mcx-futures-natgasmini ai_trade-backend-1 ai_trade-frontend-1; do echo "$c $(docker inspect -f '{{.Image}} started={{.State.StartedAt}} restarts={{.RestartCount}}' $c 2>/dev/null | cut -c1-90)"; done
echo "== alembic head"; docker exec ai_trade-backend-1 sh -c 'cd /app && alembic current 2>/dev/null | tail -1'
echo "== NIFTY ACTS observation + trade states"; docker exec ai_trade-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select '"'"'observed_at='"'"'||observed_at from acts_paper_cursor; select state||'"'"'='"'"'||count(*) from acts_paper_trades group by state order by state;"'
echo "== MCX cursors + trades"; docker exec ai_trade-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select instrument||'"'"' observed_at='"'"'||coalesce(observed_at,'"'"'-'"'"')||'"'"' seq='"'"'||sequence from mcx_futures_cursors; select instrument||'"'"' '"'"'||state||'"'"'='"'"'||count(*) from mcx_futures_trades group by instrument,state order by 1;"'
echo "== MCX worker flags (non-secret)"; for c in mcx-futures-crudeoilm mcx-futures-natgasmini; do echo "$c: $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $c 2>/dev/null | grep -E '^MCX_FUTURES_' | tr '\n' ' ')"; done
echo "== API (inside backend container)"; for p in /api/health /api/engines/status /api/mcx-futures/status /api/portfolio/header /api/portfolio/summary; do echo "$p -> $(docker exec ai_trade-backend-1 python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000$p',timeout=60).status)" 2>&1 | tail -1)"; done
EOF
```
Healthy tomorrow after 09:30 IST: NIFTY `observed_at` within the last minute and no `OPEN` trade for the 23-Nov expiry;
both MCX cursors fresh during MCX hours. Before the deploy, `/api/portfolio/*` returns 404 (expected).

## Block 2 — NIFTY ACTS V3 incident: verify, and the only remediation needed

The stall of 23 Nov 12:46 IST was the API-key rotation (the worker holds the key it started with). The worker was
recreated on 24 Nov with the new key. **Do not edit the journal and do not invent a settlement price.**

```bash
bash <<'EOF'
echo "== container"; docker ps --filter name=^acts-paper$ --format '{{.Names}} {{.Image}} {{.Status}}'
echo "== last observation"; docker exec ai_trade-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select observed_at from acts_paper_cursor;"'
echo "== the expired 25600/26000 trade"; docker exec ai_trade-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select state, left(id,10), document::json->>'"'"'exit_reason'"'"' from acts_paper_trades where state in ('"'"'OPEN'"'"','"'"'UNRESOLVED_EXPOSURE'"'"') or document::json->>'"'"'exit_reason'"'"'='"'"'CONTRACT_EXPIRED_UNPRICED'"'"';"'
echo "== recent worker log lines"; docker exec acts-paper sh -c 'ls -t /app/logs 2>/dev/null | head -1 | xargs -I{} tail -n 15 /app/logs/{}' 2>&1 | grep -viE 'key|token|secret|password' | tail -15
EOF
```
Expected after 09:20 IST: a fresh `observed_at`, and the old trade shown as `CLOSED ... CONTRACT_EXPIRED_UNPRICED`.
If it is still `OPEN` and `observed_at` is stale, the new key is not reaching the worker: confirm `.env.production` holds
the new key, then recreate only that container (`docker stop acts-paper && docker rename acts-paper acts-paper-old &&
docker run -d --name acts-paper --network ai_trade_app_network --restart unless-stopped --env-file
.env.production -e ACTS_PAPER_ENABLED=true -e ACTS_PAPER_STRATEGY_VERSION=ACTS_V3 ai-trade-acts-v3:4cd0a3b python
/app/worker/acts_paper_worker.py --mode PAPER`, all on ONE line). The trade then terminalises itself on the first
observation. Any real broker exposure would be a separate, explicitly authorised action; none exists in this system
(the paper path has no order-placement code — verified by source search).

## Block 3 — deploy: backend + frontend + MCX paper workers (build ~8-10 min, swaps ~1 min)

Prerequisite: branch `feature/trading-terminal` is pushed to GitHub at `2438b3bab3e006580b2bd63da94758dfe189fef0`.

```bash
bash <<'EOF'
set -uo pipefail
cd /home/ubuntu/AI_Trade; SHA=2438b3bab3e006580b2bd63da94758dfe189fef0; S=${SHA:0:7}; B=/home/ubuntu/build-$S; BI=ai-trade-mcx-futures:$S; FI=nifty-research-frontend:$S; NET=ai_trade_app_network
H=$(TZ=Asia/Kolkata date +%H%M); echo "== IST $H | $(free -m | awk '/Mem/{print $7" MB RAM available"}') | $(free -m | awk '/Swap/{print $4" MB swap free"}')"; if [ "${FORCE:-0}" != 1 ] && [ "$H" -ge 0855 ] && [ "$H" -lt 1535 ]; then echo "ABORT: IST $H is inside NSE hours; the build would compete with the live NIFTY worker. Re-run after 15:35 IST (or before 08:55), or run it as FORCE=1 bash ... to override."; exit 1; fi
git fetch -q origin feature/trading-terminal || { echo "ABORT: fetch failed (is the branch pushed?)"; exit 1; }
[ -d $B ] || git worktree add -f $B $SHA >/dev/null; [ "$(git -C $B rev-parse HEAD)" = "$SHA" ] || { echo "ABORT: build dir is not at $SHA"; exit 1; }
echo "== 1/6 build backend image (3-5 min)"; docker build -q -f $B/backend/Dockerfile -t $BI $B >/dev/null || { echo "ABORT: backend build failed; nothing changed"; exit 1; }
echo "== 2/6 build frontend image (3-5 min)"; docker build -q --build-arg NEXT_PUBLIC_API_BASE_URL= --build-arg NEXT_PUBLIC_POLL_INTERVAL_MS=30000 -t $FI $B/frontend >/dev/null || { echo "ABORT: frontend build failed; nothing changed"; exit 1; }
echo "== 3/6 migration head must already be 0025"; H=$(docker run --rm --network $NET --env-file .env.production $BI sh -c 'cd /app && alembic current 2>/dev/null | tail -1'); echo "$H"; case "$H" in 0025_mcx_futures_paper*) ;; *) echo "ABORT: database is not at 0025 (run the migration step from the earlier release first)"; exit 1;; esac
swap() { OLD=$1; ALIAS=$2; IMG=$3; HC=$4; EXTRA=${5:-}; KEEP=$OLD-pre-$S
  docker ps -a --format '{{.Names}}' | grep -qx $KEEP && { echo "SKIP $OLD: $KEEP exists (already swapped)"; return 0; }
  MOUNTS=$(docker inspect -f '{{range .Mounts}}-v {{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}:{{.Destination}}{{if not .RW}}:ro{{end}} {{end}}' $OLD)
  rb() { echo "!! ROLLBACK $OLD: $1"; docker rm -f $OLD >/dev/null 2>&1; docker rename $KEEP $OLD && docker start $OLD >/dev/null && echo "restored $OLD"; return 1; }
  mapfile -t CMD < <(docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' $KEEP)
  docker rename $OLD $KEEP && docker stop $KEEP >/dev/null
  docker run -d --name $OLD --network $NET --network-alias $ALIAS --restart unless-stopped --env-file <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $KEEP | grep -v '^NEXT_PUBLIC_') $MOUNTS $EXTRA --log-opt max-size=10m --log-opt max-file=5 --health-cmd "$HC" --health-interval 20s $IMG "${CMD[@]}" >/dev/null || { rb "run failed"; return 1; }
  for i in $(seq 1 30); do s=$(docker inspect -f '{{.State.Health.Status}}' $OLD); [ "$s" = healthy ] && break; sleep 3; done; [ "$s" = healthy ] || { rb "not healthy"; return 1; }
  echo "swapped $OLD -> $IMG"; }
echo "== 4/6 backend"; swap ai_trade-backend-1 backend $BI "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5)\"" || exit 1
for p in /api/health /api/portfolio/header /api/portfolio/summary /api/portfolio/positions /api/portfolio/orders /api/portfolio/trades /api/portfolio/performance /api/portfolio/risk /api/portfolio/strategies /api/phase15/summary; do out=$(docker exec ai_trade-backend-1 python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000$p',timeout=90).status)" 2>&1 | tail -1); echo "$p -> $out"; if [ "$out" != 200 ]; then echo "!! API check failed: rolling the backend back"; docker rm -f ai_trade-backend-1 >/dev/null; docker rename ai_trade-backend-1-pre-$S ai_trade-backend-1; docker start ai_trade-backend-1 >/dev/null; exit 1; fi; done
echo "== 5/6 frontend"; swap ai_trade-frontend-1 frontend $FI "node -e \"fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"" || exit 1
for p in / /positions /orders /tradebook /strategies /analytics /risk /research /system /settings /engines; do c=$(docker exec ai_trade-frontend-1 node -e "fetch('http://127.0.0.1:3000$p').then(r=>console.log(r.status))"); echo "$p -> $c"; [ "$c" = 200 ] || { echo "!! page check failed: rolling the frontend back"; docker rm -f ai_trade-frontend-1 >/dev/null; docker rename ai_trade-frontend-1-pre-$S ai_trade-frontend-1; docker start ai_trade-frontend-1 >/dev/null; exit 1; }; done
echo "== 6/6 MCX paper workers (only when FLAT: no open position and no pending entry)"
for I in crudeoilm natgasmini; do W=mcx-futures-$I; U=$(echo $I | tr a-z A-Z)
  docker inspect $W >/dev/null 2>&1 || { echo "SKIP $W: not present"; continue; }
  EXP=$(docker exec ai_trade-postgres-1 sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"select count(*) from mcx_futures_trades where instrument='$U' and state in ('OPEN','PENDING_PAPER_ENTRY','UNRESOLVED_EXPOSURE')\"")
  if [ "$EXP" != 0 ]; then echo "SKIP $W: $EXP open/pending exposure. Re-run this block when it is flat; the running worker keeps trading meanwhile."; continue; fi
  swap $W $W $BI "python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument $U --health" "-e MCX_FUTURES_TRADING_ENABLED=true" || echo "!! $W not swapped (rolled back)"
done
echo "== effective MCX worker settings (must show PAPER, PAPER_ENABLED=true, TRADING_ENABLED=true)"; for W in mcx-futures-crudeoilm mcx-futures-natgasmini; do echo "$W image=$(docker inspect -f '{{.Config.Image}}' $W) $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $W | grep -E '^MCX_FUTURES_(MODE|PAPER_ENABLED|TRADING_ENABLED)=' | tr '\n' ' ')"; done
echo "== DONE. Old containers kept as *-pre-$S. Hard-refresh the browser (Ctrl+Shift+R)."
EOF
```
What it guarantees: nothing is replaced until both images built; a failed health or API/page check rolls that
component back and stops; MCX workers with open exposure are skipped (never restarted under a position); paper
entries are explicitly enabled on refreshed workers (`MCX_FUTURES_TRADING_ENABLED=true`); the kill switch directory
mount is preserved. `acts-paper` is not touched.

## Block 3b — CORRECTION for the MCX workers (defect found in production on 25 Nov)

Block 3 as first issued started the two MCX containers with the image default command (the API server) instead of the
worker command, so no worker ran from 08:55 IST until this fix; Block 3's health check passed vacuously because MCX was
still closed at 08:55. `swap()` above now copies the old container's command. The corrected, rehearsed (real Docker,
success and rollback paths) one-shot fix is the block issued in the session; the original block's `swap()` must not be
re-used without the command carry-over.

## Block 4 — post-deploy operational verification and reconciliation (read-only; exits non-zero on failure)

Source of the embedded script: `scripts/prod_verify.py` (HTTP GETs and SELECT reads only).

```bash
bash <<'EOF'
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
echo "################ F. RESULT ################"
if [ "$FAILS" -eq 0 ]; then echo "PRODUCTION VERIFICATION: PASS"; else echo "PRODUCTION VERIFICATION: FAILED ($FAILS check group(s)) - paste this whole output"; fi
exit $FAILS
EOF
```

## Block 5 — release 2 (4d2fa3d): portfolio/alert/header/session/cost fixes. Run AFTER 15:35 IST, then run Block 4

Prerequisite: branch `feature/trading-terminal` pushed at `4d2fa3d7ea21eb4830113f172c64e8daa363ec97` (requires the owner's push authorisation).

```bash
bash <<'EOF'
set -uo pipefail
cd /home/ubuntu/AI_Trade; SHA=4d2fa3d7ea21eb4830113f172c64e8daa363ec97; S=${SHA:0:7}; B=/home/ubuntu/build-$S; BI=ai-trade-mcx-futures:$S; FI=nifty-research-frontend:$S; NET=ai_trade_app_network
H=$(TZ=Asia/Kolkata date +%H%M); echo "== IST $H | $(free -m | awk '/Mem/{print $7" MB RAM available"}') | $(free -m | awk '/Swap/{print $4" MB swap free"}')"; if [ "${FORCE:-0}" != 1 ] && [ "$H" -ge 0855 ] && [ "$H" -lt 1535 ]; then echo "ABORT: IST $H is inside NSE hours; the build would compete with the live NIFTY worker. Re-run after 15:35 IST (or before 08:55), or run it as FORCE=1 bash ... to override."; exit 1; fi
git fetch -q origin feature/trading-terminal || { echo "ABORT: fetch failed (is the branch pushed?)"; exit 1; }
[ -d $B ] || git worktree add -f $B $SHA >/dev/null; [ "$(git -C $B rev-parse HEAD)" = "$SHA" ] || { echo "ABORT: build dir is not at $SHA"; exit 1; }
echo "== 1/6 build backend image (3-5 min)"; docker build -q -f $B/backend/Dockerfile -t $BI $B >/dev/null || { echo "ABORT: backend build failed; nothing changed"; exit 1; }
echo "== 2/6 build frontend image (3-5 min)"; docker build -q --build-arg NEXT_PUBLIC_API_BASE_URL= --build-arg NEXT_PUBLIC_POLL_INTERVAL_MS=30000 -t $FI $B/frontend >/dev/null || { echo "ABORT: frontend build failed; nothing changed"; exit 1; }
echo "== 3/6 migration head must already be 0025"; H=$(docker run --rm --network $NET --env-file .env.production $BI sh -c 'cd /app && alembic current 2>/dev/null | tail -1'); echo "$H"; case "$H" in 0025_mcx_futures_paper*) ;; *) echo "ABORT: database is not at 0025 (run the migration step from the earlier release first)"; exit 1;; esac
swap() { OLD=$1; ALIAS=$2; IMG=$3; HC=$4; EXTRA=${5:-}; KEEP=$OLD-pre-$S
  docker ps -a --format '{{.Names}}' | grep -qx $KEEP && { echo "SKIP $OLD: $KEEP exists (already swapped)"; return 0; }
  MOUNTS=$(docker inspect -f '{{range .Mounts}}-v {{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}:{{.Destination}}{{if not .RW}}:ro{{end}} {{end}}' $OLD)
  rb() { echo "!! ROLLBACK $OLD: $1"; docker rm -f $OLD >/dev/null 2>&1; docker rename $KEEP $OLD && docker start $OLD >/dev/null && echo "restored $OLD"; return 1; }
  mapfile -t CMD < <(docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' $KEEP)
  docker rename $OLD $KEEP && docker stop $KEEP >/dev/null
  docker run -d --name $OLD --network $NET --network-alias $ALIAS --restart unless-stopped --env-file <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $KEEP | grep -v '^NEXT_PUBLIC_') $MOUNTS $EXTRA --log-opt max-size=10m --log-opt max-file=5 --health-cmd "$HC" --health-interval 20s $IMG "${CMD[@]}" >/dev/null || { rb "run failed"; return 1; }
  for i in $(seq 1 30); do s=$(docker inspect -f '{{.State.Health.Status}}' $OLD); [ "$s" = healthy ] && break; sleep 3; done; [ "$s" = healthy ] || { rb "not healthy"; return 1; }
  echo "swapped $OLD -> $IMG"; }
echo "== 4/6 backend"; swap ai_trade-backend-1 backend $BI "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5)\"" || exit 1
for p in /api/health /api/portfolio/header /api/portfolio/summary /api/portfolio/positions /api/portfolio/orders /api/portfolio/trades /api/portfolio/performance /api/portfolio/risk /api/portfolio/strategies /api/phase15/summary; do out=$(docker exec ai_trade-backend-1 python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000$p',timeout=90).status)" 2>&1 | tail -1); echo "$p -> $out"; if [ "$out" != 200 ]; then echo "!! API check failed: rolling the backend back"; docker rm -f ai_trade-backend-1 >/dev/null; docker rename ai_trade-backend-1-pre-$S ai_trade-backend-1; docker start ai_trade-backend-1 >/dev/null; exit 1; fi; done
echo "== 5/6 frontend"; swap ai_trade-frontend-1 frontend $FI "node -e \"fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"" || exit 1
for p in / /positions /orders /tradebook /strategies /analytics /risk /research /system /settings /engines; do c=$(docker exec ai_trade-frontend-1 node -e "fetch('http://127.0.0.1:3000$p').then(r=>console.log(r.status))"); echo "$p -> $c"; [ "$c" = 200 ] || { echo "!! page check failed: rolling the frontend back"; docker rm -f ai_trade-frontend-1 >/dev/null; docker rename ai_trade-frontend-1-pre-$S ai_trade-frontend-1; docker start ai_trade-frontend-1 >/dev/null; exit 1; }; done
echo "== 6/6 MCX paper workers (only when FLAT: no open position and no pending entry)"
for I in crudeoilm natgasmini; do W=mcx-futures-$I; U=$(echo $I | tr a-z A-Z)
  docker inspect $W >/dev/null 2>&1 || { echo "SKIP $W: not present"; continue; }
  EXP=$(docker exec ai_trade-postgres-1 sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"select count(*) from mcx_futures_trades where instrument='$U' and state in ('OPEN','PENDING_PAPER_ENTRY','UNRESOLVED_EXPOSURE')\"")
  if [ "$EXP" != 0 ]; then echo "SKIP $W: $EXP open/pending exposure. Re-run this block when it is flat; the running worker keeps trading meanwhile."; continue; fi
  swap $W $W $BI "python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument $U --health" "-e MCX_FUTURES_TRADING_ENABLED=true" || echo "!! $W not swapped (rolled back)"
  CMDNOW=$(docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' $W); case "$CMDNOW" in *mcx_futures_paper_worker.py*) echo "PASS $W runs: $CMDNOW";; *) echo "!! $W does NOT run the worker command ($CMDNOW): rolling back"; docker rm -f $W >/dev/null 2>&1; docker rename $W-pre-$S $W && docker start $W >/dev/null;; esac
done
echo "== effective MCX worker settings (must show PAPER, PAPER_ENABLED=true, TRADING_ENABLED=true)"; for W in mcx-futures-crudeoilm mcx-futures-natgasmini; do echo "$W image=$(docker inspect -f '{{.Config.Image}}' $W) $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $W | grep -E '^MCX_FUTURES_(MODE|PAPER_ENABLED|TRADING_ENABLED)=' | tr '\n' ' ')"; done
echo "== DONE. Old containers kept as *-pre-$S. Hard-refresh the browser (Ctrl+Shift+R)."
EOF
```

## Rollback (per component, instant)

```bash
docker rm -f ai_trade-backend-1 && docker rename ai_trade-backend-1-pre-<SHA7> ai_trade-backend-1 && docker start ai_trade-backend-1
```
```bash
docker rm -f ai_trade-frontend-1 && docker rename ai_trade-frontend-1-pre-2438b3b ai_trade-frontend-1 && docker start ai_trade-frontend-1
```
```bash
docker rm -f mcx-futures-crudeoilm && docker rename mcx-futures-crudeoilm-pre-2438b3b mcx-futures-crudeoilm && docker start mcx-futures-crudeoilm
```
Replace `crudeoilm` with `natgasmini` for the other worker. Tables and journals are never touched by a rollback.
Kill switch for new MCX paper entries without a restart: `touch /home/ubuntu/AI_Trade/mcx-control/MCX_FUTURES_KILL`
(open positions keep being managed).

## Not included, by instruction

Historical MCX data acquisition and strategy research were stopped on request; the MCX engines remain
`PRE_REGISTERED_UNVALIDATED` and are judged from forward paper results only.

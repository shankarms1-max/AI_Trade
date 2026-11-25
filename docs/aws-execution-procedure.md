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
  docker rename $OLD $KEEP && docker stop $KEEP >/dev/null
  docker run -d --name $OLD --network $NET --network-alias $ALIAS --restart unless-stopped --env-file <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $KEEP | grep -v '^NEXT_PUBLIC_') $MOUNTS $EXTRA --log-opt max-size=10m --log-opt max-file=5 --health-cmd "$HC" --health-interval 20s $IMG >/dev/null || { rb "run failed"; return 1; }
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

## Block 4 — post-deploy verification and cross-surface P&L reconciliation (read-only)

```bash
bash <<'EOF'
docker exec ai_trade-backend-1 python - <<'PY'
import json, urllib.request
g = lambda p: json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/portfolio/" + p, timeout=90))
s, pos, tr, al, hd, src = g("summary"), g("positions?tab=open&limit=200"), g("trades?limit=200"), g("alerts"), g("header"), g("sources")
day = s["window"]["from"]
t = g(f"trades?from={day}&to={day}&limit=200")
print("header   :", hd["nse"]["status"], "NSE |", hd["mcx"]["status"], "MCX | broker:", hd["broker_connectivity"]["state"], "| live:", hd["live_execution"])
print("summary  :", {k: s["pnl"][k] for k in ("realized_gross", "realized_net", "unrealized_gross", "incurred_charges", "total_net_pnl", "partial")})
print("open     :", s["counts"]["open_positions"], "positions /", pos["total"], "listed | unresolved:", s["counts"]["unresolved_positions"])
chk = round(t["totals"]["net_pnl"], 2) == round(s["pnl"]["realized_net"], 2)
print("RECONCILE: tradebook net %.2f vs summary realized_net %.2f -> %s" % (t["totals"]["net_pnl"], s["pnl"]["realized_net"], "PASS" if chk else "FAIL"))
print("alerts   :", al["critical"], "critical,", al["warning"], "warning"); [print("   ", a["severity"], a["code"], a["engine"], "|", a["message"][:110]) for a in al["items"][:6]]
print("sources  :", {x["source"]: x["status"] for x in src["sources"]})
for e in g("strategies")["items"]:
    d = e["dimensions"]; en = (d.get("enablement") or {}).get("paper_execution")
    print("engine   : %-28s state=%-13s paper=%-14s feed=%s" % (e["label"], e["operational_state"], en, (d.get("feed") or {}).get("last_quote_at") or (d.get("feed") or {}).get("last_observation")))
PY
EOF
```
`RECONCILE ... PASS` proves the tradebook, positions and summary agree. Judge market-session items by the clock:
while MCX is open each MCX engine must show `state=RUNNING`, `paper=ENABLED` and a fresh feed; the NIFTY engine is
`RUNNING` only 09:15-15:30 IST. Do not expect a paper trade on demand: none is ever forced.

## Rollback (per component, instant)

```bash
docker rm -f ai_trade-backend-1 && docker rename ai_trade-backend-1-pre-2438b3b ai_trade-backend-1 && docker start ai_trade-backend-1
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

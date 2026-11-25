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
  mapfile -t CMD < <(docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' $OLD)
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

## Block 3b — MCX worker RECOVERY v2 (the version to use)

Source: `scripts/recover_mcx_workers_v2.inner.sh`. Reproduces the ORIGINAL container's effective configuration
(entrypoint, command, user, workdir, mounts, resource limits, restart policy, network + aliases, log options, runtime env),
creates the replacement with `docker create` first (validates every option before anything changes), re-reads the
persisted exposure before and after stopping the current container, replaces ONLY the two workers, verifies the process,
healthcheck and start log, diffs the new container against the original, and restores the previous worker on ANY failure.
Failures propagate: `WORKER RECOVERY: PASS` is only printed when every step and check passed. Never builds an image.
Rehearsed with real Docker and PostgreSQL (success, open-exposure and unhealthy-replacement paths).

```bash
bash <<'EOF'
set -uo pipefail
cd /home/ubuntu/AI_Trade
S=2438b3b; BI=ai-trade-mcx-futures:$S; NET=ai_trade_app_network; FAILS=0; CRIT=0
TMPD=$(mktemp -d) || { echo "ABORT: cannot create a temp dir"; exit 1; }; chmod 700 "$TMPD"; trap 'rm -rf "$TMPD"' EXIT
bad() { echo "FAIL $*"; FAILS=$((FAILS+1)); }
crit() { echo "CRITICAL $*"; CRIT=$((CRIT+1)); FAILS=$((FAILS+1)); }
isnum() { case "$1" in ''|*[!0-9]*) return 1;; *) return 0;; esac; }
GEN=$(cat <<'PYGEN'
import hashlib, json, shlex, sys
mode, tag = sys.argv[1], sys.argv[2]
extra = sys.argv[3:]
def load(t):
    return json.load(open("/in/%s.c.json" % t))[0], json.load(open("/in/%s.i.json" % t))[0]
def parts(c, im):
    cfg, hc, ns = c["Config"], c["HostConfig"], c["NetworkSettings"]
    img_env = set(im["Config"].get("Env") or [])
    env = [e for e in (cfg.get("Env") or []) if e not in img_env]
    short = c["Id"][:12]
    nets = {n: sorted(a for a in (v.get("Aliases") or []) if a != short) for n, v in (ns.get("Networks") or {}).items()}
    return cfg, hc, env, nets
def health(cfg):
    h = cfg.get("Healthcheck")
    return h if h and h.get("Test") and h["Test"] != ["NONE"] else None
def canon(c, im, skip):
    cfg, hc, env, nets = parts(c, im)
    envd = {}
    for e in env:
        k, _, v = e.partition("=")
        if k not in skip:
            envd[k] = hashlib.sha256(v.encode()).hexdigest()[:10]
    return {"entrypoint": cfg.get("Entrypoint"), "cmd": cfg.get("Cmd"), "user": cfg.get("User") or "", "workdir": cfg.get("WorkingDir") or "",
            "stop_timeout": cfg.get("StopTimeout"), "restart": (hc.get("RestartPolicy") or {}).get("Name") or "no",
            "mounts": sorted([m["Type"], m.get("Name") or m.get("Source"), m["Destination"], bool(m.get("RW"))] for m in c.get("Mounts") or []),
            "networks": nets, "memory": hc.get("Memory"), "memory_swap": hc.get("MemorySwap"), "memory_reservation": hc.get("MemoryReservation"),
            "nano_cpus": hc.get("NanoCpus"), "cpu_shares": hc.get("CpuShares"), "cpuset": hc.get("CpusetCpus"), "pids": hc.get("PidsLimit"),
            "cap_add": sorted(hc.get("CapAdd") or []), "cap_drop": sorted(hc.get("CapDrop") or []), "readonly_rootfs": hc.get("ReadonlyRootfs"),
            "privileged": hc.get("Privileged"), "init": hc.get("Init"), "ulimits": hc.get("Ulimits"), "extra_hosts": hc.get("ExtraHosts"),
            "dns": hc.get("Dns"), "security_opt": hc.get("SecurityOpt"), "tmpfs": hc.get("Tmpfs"),
            "log": [(hc.get("LogConfig") or {}).get("Type"), (hc.get("LogConfig") or {}).get("Config")], "env": envd}
c, im = load(tag)
cfg, hc, env, nets = parts(c, im)
if mode == "args":
    default_hc = extra[0]
    a = []
    if hc.get("Privileged"):
        sys.stderr.write("refusing: the original container is privileged\n"); sys.exit(3)
    if len(nets) != 1:
        sys.stderr.write("refusing: expected exactly one network, found %d\n" % len(nets)); sys.exit(3)
    rp = hc.get("RestartPolicy") or {}
    if rp.get("Name"):
        a += ["--restart", rp["Name"] + (":%d" % rp["MaximumRetryCount"] if rp.get("MaximumRetryCount") else "")]
    if cfg.get("User"): a += ["--user", cfg["User"]]
    if cfg.get("WorkingDir"): a += ["--workdir", cfg["WorkingDir"]]
    if cfg.get("Entrypoint"): a += ["--entrypoint", cfg["Entrypoint"][0]]
    if cfg.get("StopTimeout"): a += ["--stop-timeout", str(cfg["StopTimeout"])]
    if hc.get("Memory"): a += ["--memory", str(hc["Memory"])]
    if hc.get("MemorySwap"): a += ["--memory-swap", str(hc["MemorySwap"])]
    if hc.get("MemoryReservation"): a += ["--memory-reservation", str(hc["MemoryReservation"])]
    if hc.get("NanoCpus"): a += ["--cpus", "%.3f" % (hc["NanoCpus"] / 1e9)]
    if hc.get("CpuShares"): a += ["--cpu-shares", str(hc["CpuShares"])]
    if hc.get("CpusetCpus"): a += ["--cpuset-cpus", hc["CpusetCpus"]]
    if hc.get("PidsLimit") and hc["PidsLimit"] > 0: a += ["--pids-limit", str(hc["PidsLimit"])]
    for k in hc.get("CapAdd") or []: a += ["--cap-add", k]
    for k in hc.get("CapDrop") or []: a += ["--cap-drop", k]
    for k in hc.get("SecurityOpt") or []: a += ["--security-opt", k]
    if hc.get("ReadonlyRootfs"): a += ["--read-only"]
    if hc.get("Init"): a += ["--init"]
    for u in hc.get("Ulimits") or []: a += ["--ulimit", "%s=%s:%s" % (u["Name"], u["Soft"], u["Hard"])]
    for h in hc.get("ExtraHosts") or []: a += ["--add-host", h]
    for d in hc.get("Dns") or []: a += ["--dns", d]
    for p, o in (hc.get("Tmpfs") or {}).items(): a += ["--tmpfs", p + (":" + o if o else "")]
    lc = hc.get("LogConfig") or {}
    if lc.get("Type"): a += ["--log-driver", lc["Type"]]
    for k, v in (lc.get("Config") or {}).items(): a += ["--log-opt", "%s=%s" % (k, v)]
    for m in c.get("Mounts") or []:
        a += ["--mount", "type=%s,source=%s,target=%s%s" % (m["Type"], m.get("Name") or m.get("Source"), m["Destination"], "" if m.get("RW") else ",readonly")]
    (net, aliases), = nets.items()
    a += ["--network", net]
    for al in aliases: a += ["--network-alias", al]
    h = health(cfg)
    if h:
        t = h["Test"]
        a += ["--health-cmd", t[1] if t[0] == "CMD-SHELL" else shlex.join(t[1:])]
        for flag, key in (("--health-interval", "Interval"), ("--health-timeout", "Timeout"), ("--health-start-period", "StartPeriod")):
            if h.get(key): a += [flag, "%gs" % (h[key] / 1e9)]
        if h.get("Retries"): a += ["--health-retries", str(h["Retries"])]
    else:
        a += ["--health-cmd", default_hc, "--health-interval", "20s"]
    sys.stdout.write("".join(x + "\0" for x in a))
elif mode == "cmd":
    sys.stdout.write("".join(x + "\0" for x in (cfg.get("Entrypoint") or [])[1:] + (cfg.get("Cmd") or [])))
elif mode == "env":
    sys.stdout.write("\n".join(env) + "\n")
elif mode == "diff":
    flag = extra[0]
    skip = ("MCX_FUTURES_MODE", "MCX_FUTURES_PAPER_ENABLED", "MCX_FUTURES_TRADING_ENABLED", flag)
    n, nim = load(extra[1])
    a, b = canon(c, im, skip), canon(n, nim, skip)
    bad = []
    for k in sorted(a):
        if a[k] != b[k]:
            bad.append("%s: original=%s new=%s" % (k, json.dumps(a[k])[:200], json.dumps(b[k])[:200]))
    newenv = dict(e.partition("=")[::2] for e in n["Config"].get("Env") or [])
    for k, v in (("MCX_FUTURES_MODE", "PAPER"), ("MCX_FUTURES_PAPER_ENABLED", "true"), ("MCX_FUTURES_TRADING_ENABLED", "true"), (flag, "true")):
        if newenv.get(k) != v:
            bad.append("env %s must be %s, is %s" % (k, v, newenv.get(k)))
    print("\n".join(bad) if bad else "effective configuration identical (entrypoint, command, user, workdir, mounts, network+aliases, restart, limits, log, env) apart from the image and the enforced PAPER flags")
    sys.exit(1 if bad else 0)
PYGEN
)
cmdof() { docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' "$1" 2>/dev/null; }
isworker() { case "$(cmdof "$1")" in *mcx_futures_paper_worker.py*) return 0;; *) return 1;; esac; }
expo() { docker exec ai_trade-postgres-1 sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"select (select count(*) from mcx_futures_trades where instrument='$1' and state in ('OPEN','PENDING_PAPER_ENTRY','UNRESOLVED_EXPOSURE')) + (select count(*) from mcx_futures_cursors where instrument='$1' and (coalesce((state_json::json->'position')::text,'null')<>'null' or coalesce((state_json::json->'pending_entry')::text,'null')<>'null'))\"" 2>/dev/null | tr -d '[:space:]'; }
gen() { docker run --rm --network none --user "$(id -u):$(id -g)" -v "$TMPD":/in:ro $BI python -c "$GEN" "$@"; }
snap() { docker inspect --type container "$1" > "$TMPD/$2.c.json" 2>/dev/null && docker inspect --type image "$(docker inspect -f '{{.Image}}' "$1")" > "$TMPD/$2.i.json" 2>/dev/null; }
restore_old() { local W=$1 K=$2 ok=1
  if docker inspect $W >/dev/null 2>&1; then docker stop -t 30 $W >/dev/null 2>&1; docker rm -f $W >/dev/null 2>&1; fi
  if docker inspect $W >/dev/null 2>&1; then crit "$W: cannot remove the replacement container"; ok=0; fi
  if [ $ok = 1 ]; then docker rename $K $W || { crit "$W: cannot rename $K back"; ok=0; }; fi
  if [ $ok = 1 ]; then docker start $W >/dev/null || { crit "$W: cannot start the previous worker"; ok=0; }; fi
  if [ $ok = 1 ]; then sleep 5; [ "$(docker inspect -f '{{.State.Status}}' $W 2>/dev/null)" = running ] || { crit "$W: the previous worker is not running"; ok=0; }; fi
  if [ $ok = 1 ]; then echo "restored the previous worker as $W (running)"; else echo "CRITICAL: $W is NOT running. Manual recovery: docker rm -f $W; docker rename $K $W; docker start $W"; fi
  [ $ok = 1 ]; }
recover() { local I=$1; local W=mcx-futures-$I; local U=$(echo $I | tr a-z A-Z); local K=$W-pre-$S BAD=$W-badcmd-$S STG=$W-staged-$S FLAG=MCX_FUTURES_${U}_ENABLED
  local HC="python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument $U --health" E1 E2 st s L PT ok k
  echo "================ $W ================"
  docker inspect $K >/dev/null 2>&1 || { crit "$W: the original container $K is missing; its configuration cannot be reproduced"; return 1; }
  docker inspect $W >/dev/null 2>&1 || { crit "$W: no container with that name exists"; return 1; }
  if isworker $W; then echo "OK $W already runs the worker command; nothing to replace"; return 0; fi
  isworker $K || { bad "$W: the original container $K does not run the worker command ($(cmdof $K)); refusing to guess"; return 1; }
  if docker top $W -o pid,args 2>/dev/null | grep -q mcx_futures_paper_worker.py; then echo "OK $W is running a worker process; nothing to replace"; return 0; fi
  echo "process currently running as $W (not a trading process): $(docker top $W -o pid,args 2>/dev/null | tail -n +2 | tr '\n' ' ' | cut -c1-110)"
  snap $K old || { bad "$W: cannot read the original container's configuration"; return 1; }
  gen args old "$HC" > "$TMPD/args.bin" || { bad "$W: cannot derive the docker options from the original container (see message above); nothing changed"; return 1; }
  gen cmd old > "$TMPD/cmd.bin" || { bad "$W: cannot derive the command; nothing changed"; return 1; }
  gen env old > "$TMPD/env.txt" || { bad "$W: cannot derive the environment; nothing changed"; return 1; }; chmod 600 "$TMPD/env.txt"
  mapfile -d '' -t ARGS < "$TMPD/args.bin"; mapfile -d '' -t CMD < "$TMPD/cmd.bin"
  if [ ${#ARGS[@]} -eq 0 ] || [ ${#CMD[@]} -eq 0 ]; then bad "$W: derived configuration is empty; nothing changed"; return 1; fi
  echo "command taken from the original container: ${CMD[*]}"
  docker rm -f $STG >/dev/null 2>&1
  docker create --name $STG "${ARGS[@]}" --env-file "$TMPD/env.txt" -e MCX_FUTURES_MODE=PAPER -e MCX_FUTURES_PAPER_ENABLED=true -e $FLAG=true -e MCX_FUTURES_TRADING_ENABLED=true $BI "${CMD[@]}" >/dev/null || { bad "$W: docker create rejected the reproduced configuration; nothing changed"; docker rm -f $STG >/dev/null 2>&1; return 1; }
  E1=$(expo $U); if ! isnum "$E1"; then bad "$W: cannot read the persisted exposure (got '$E1'); not replacing anything"; docker rm -f $STG >/dev/null 2>&1; return 1; fi
  if [ "$E1" != 0 ]; then echo "$W has $E1 open/pending/unresolved record(s): restoring the previous worker so the position stays managed; replacement deferred"; docker rm -f $STG >/dev/null 2>&1; restore_old $W $K; return 1; fi
  docker stop -t 30 $W >/dev/null || { crit "$W: docker stop failed"; docker rm -f $STG >/dev/null 2>&1; return 1; }
  if [ "$(docker inspect -f '{{.State.Running}}' $W 2>/dev/null)" != false ]; then crit "$W: still running after docker stop"; docker rm -f $STG >/dev/null 2>&1; return 1; fi
  E2=$(expo $U); if ! isnum "$E2" || [ "$E2" != 0 ]; then echo "$W: persisted exposure after the stop is '$E2': restoring the previous worker"; docker rm -f $STG >/dev/null 2>&1; restore_old $W $K; return 1; fi
  docker rename $W $BAD || { crit "$W: cannot rename the current container to $BAD"; docker rm -f $STG >/dev/null 2>&1; docker start $W >/dev/null 2>&1; return 1; }
  docker rename $STG $W || { echo "cannot name the staged container $W: restoring the previous worker"; docker rm -f $STG >/dev/null 2>&1; restore_old $W $K; return 1; }
  docker start $W >/dev/null || { echo "$W: the new worker did not start: restoring the previous worker"; restore_old $W $K; return 1; }
  ok=0; for k in $(seq 1 40); do st=$(docker inspect -f '{{.State.Status}}' $W 2>/dev/null); s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $W 2>/dev/null); [ "$st" = running ] || break; [ "$s" = healthy ] && { ok=1; break; }; sleep 3; done
  L=$(docker logs $W 2>&1 | grep -c MCX_FUTURES_PAPER_STARTED); PT=$(docker top $W -o pid,args 2>/dev/null)
  if [ $ok = 1 ] && [ "$L" -ge 1 ] && echo "$PT" | grep -q mcx_futures_paper_worker.py && ! echo "$PT" | grep -qi uvicorn; then echo "worker process OK: status=$st health=$s start-log-lines=$L"; else echo "$W: new worker failed verification (status=$st health=$s start-log=$L process=[$(echo $PT | tr '\n' ' ' | cut -c1-150)]): restoring the previous worker"; restore_old $W $K; return 1; fi
  snap $W new && gen diff old $FLAG new || { echo "$W: the new container's effective configuration differs from the original (see above): restoring the previous worker"; restore_old $W $K; return 1; }
  docker stop $K >/dev/null 2>&1
  echo "OK $W: replaced (original kept as $K, uvicorn container kept as $BAD)"; return 0; }
docker image inspect $BI >/dev/null 2>&1 || { echo "ABORT: image $BI is not present on this host. This block never builds anything."; exit 1; }
[ "$(docker inspect -f '{{.State.Running}}' ai_trade-postgres-1 2>/dev/null)" = true ] || { echo "ABORT: ai_trade-postgres-1 is not running"; exit 1; }
RECOVERY_START=$(TZ=Asia/Kolkata date -Is); echo "recovery started $RECOVERY_START (IST $(TZ=Asia/Kolkata date +%H:%M)); acts-paper, Postgres, Caddy and the collector are not touched"
for I in crudeoilm natgasmini; do recover $I || { echo "!! $I: recovery did not complete"; FAILS=$((FAILS+1)); }; done
echo "################ VERIFY (read-only): both journals advance, fresh source quotes, PAPER persisted, config hashes, warm-up bars, decisions, trade lifecycle ################"
docker exec -i -w /app -e RECOVERY_START="$RECOVERY_START" ai_trade-backend-1 python - <<'PYV'
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
PYV
[ $? -eq 0 ] || bad "post-recovery verification reported failures (see FAIL lines)"
echo "################ FINAL STATE ################"
for W in mcx-futures-crudeoilm mcx-futures-natgasmini; do echo "$W: status=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $W 2>&1) cmd=[$(cmdof $W)]"; done
if [ "$FAILS" -eq 0 ] && [ "$CRIT" -eq 0 ]; then echo "WORKER RECOVERY: PASS"; exit 0; else echo "WORKER RECOVERY: FAILED (failures=$FAILS critical=$CRIT) - paste this whole output"; exit 1; fi
EOF
```

## Block 4 — production verifier + MCX readiness (read-only; exits non-zero on any required failure)

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
for c in mcx-futures-crudeoilm mcx-futures-natgasmini; do cmd=$(docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' $c 2>&1); case "$cmd" in *mcx_futures_paper_worker.py*--mode\ PAPER*) echo "PASS $c runs the worker: $cmd";; *) fail "$c runs [$cmd], not the MCX worker";; esac; echo "     process: $(docker top $c -o pid,args 2>/dev/null | tail -n +2 | tr '\n' ' ' | cut -c1-150)"; done
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
        sdate = ss.get("session_date") or str(ss.get("last_entry_time") or "")[:10]
        check("%s session metadata is current (not a previous day)" % i, not ss.get("last_entry_time") or ss["last_entry_time"][:10] == sdate == (today if MCX_OPEN else sdate))
        if MCX_OPEN:
            check("%s [SESSION OPEN] state is RUNNING" % i, m.get("state") == "RUNNING", "-> %s %s" % (m.get("state"), m.get("state_reason")))
            check("%s [SESSION OPEN] source quote timestamp present" % i, f.get("last_quote_at") is not None)
            check("%s [SESSION OPEN] quote age under 90 s" % i, f.get("age_seconds") is not None and f["age_seconds"] < 90, "-> %s" % f.get("age_seconds"))
            check("%s [SESSION OPEN] a contract is subscribed" % i, bool((m.get("contract") or {}).get("token")))
            check("%s [SESSION OPEN] warm-up information reported" % i, bool(w) and "bars_available" in w)
            check("%s [SESSION OPEN] a decision was evaluated today" % i, bool(d.get("at")) and str(d["at"])[:10] == today, "at=%s" % d.get("at"))
            check("%s [SESSION OPEN] session is dated today" % i, (ss.get("session_date") or str(ss.get("last_entry_time") or "")[:10]) == today)
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
EOF
```

## Block 5 — Release 2 (v2 safety protocol). Run AFTER 15:35 IST, then Block 4

Source: `scripts/release2.inner.sh`. Builds first (a failed build changes nothing), stages every replacement with
`docker create` from the CURRENT container's effective configuration, replaces backend -> frontend -> each MCX worker,
verifies health / API / pages / worker process / configuration diff, and restores the previous container on any failure.
Workers: replaced only when flat; kill switch ON while the old worker is stopped, exposure re-read after the stop, kill
switch OFF before the new worker starts; a worker with exposure is DEFERRED (keeps running) and the rest still deploys.
Rehearsed with real Docker + PostgreSQL: all-flat, one instrument holding a position (deferred), backend failing its checks
(rolled back, nothing else touched).

```bash
bash <<'EOF'
set -uo pipefail
cd /home/ubuntu/AI_Trade
SHA=b47bee5917a60455dced4bc9fd2e2f9691c95a59; S=${SHA:0:7}; BI=ai-trade-mcx-futures:$S; FI=nifty-research-frontend:$S; NET=ai_trade_app_network; FAILS=0; CRIT=0; KS=""; KS_MADE=0
TMPD=$(mktemp -d) || { echo "ABORT: cannot create a temp dir"; exit 1; }; chmod 700 "$TMPD"
cleanup() { if [ "$KS_MADE" = 1 ] && [ -n "$KS" ]; then rm -f "$KS" && echo "kill switch removed: paper entries enabled again" || echo "CRITICAL: could not remove the kill switch $KS - run: rm -f $KS"; fi; rm -rf "$TMPD"; }
trap cleanup EXIT
bad() { echo "FAIL $*"; FAILS=$((FAILS+1)); }
crit() { echo "CRITICAL $*"; CRIT=$((CRIT+1)); FAILS=$((FAILS+1)); }
isnum() { case "$1" in ''|*[!0-9]*) return 1;; *) return 0;; esac; }
H=$(TZ=Asia/Kolkata date +%H%M); echo "== IST $H | $(free -m | awk '/Mem/{print $7" MB RAM available"}') | $(free -m | awk '/Swap/{print $4" MB swap free"}')"
if [ "${FORCE:-0}" != 1 ] && [ "$H" -ge 0855 ] && [ "$H" -lt 1535 ]; then echo "ABORT: IST $H is inside NSE hours; nothing was built or changed. Run after 15:35 IST."; exit 1; fi
GEN=$(cat <<'PYGEN'
import hashlib, json, shlex, sys
mode, tag = sys.argv[1], sys.argv[2]
extra = sys.argv[3:]
def load(t):
    return json.load(open("/in/%s.c.json" % t))[0], json.load(open("/in/%s.i.json" % t))[0]
def parts(c, im):
    cfg, hc, ns = c["Config"], c["HostConfig"], c["NetworkSettings"]
    img_env = set(im["Config"].get("Env") or [])
    env = [e for e in (cfg.get("Env") or []) if e not in img_env]
    short = c["Id"][:12]
    nets = {n: sorted(a for a in (v.get("Aliases") or []) if a != short) for n, v in (ns.get("Networks") or {}).items()}
    return cfg, hc, env, nets
def health(cfg):
    h = cfg.get("Healthcheck")
    return h if h and h.get("Test") and h["Test"] != ["NONE"] else None
def canon(c, im, skip):
    cfg, hc, env, nets = parts(c, im)
    envd = {}
    for e in env:
        k, _, v = e.partition("=")
        if k not in skip:
            envd[k] = hashlib.sha256(v.encode()).hexdigest()[:10]
    return {"entrypoint": cfg.get("Entrypoint"), "cmd": cfg.get("Cmd"), "user": cfg.get("User") or "", "workdir": cfg.get("WorkingDir") or "",
            "stop_timeout": cfg.get("StopTimeout"), "restart": (hc.get("RestartPolicy") or {}).get("Name") or "no",
            "mounts": sorted([m["Type"], m.get("Name") or m.get("Source"), m["Destination"], bool(m.get("RW"))] for m in c.get("Mounts") or []),
            "networks": nets, "memory": hc.get("Memory"), "memory_swap": hc.get("MemorySwap"), "memory_reservation": hc.get("MemoryReservation"),
            "nano_cpus": hc.get("NanoCpus"), "cpu_shares": hc.get("CpuShares"), "cpuset": hc.get("CpusetCpus"), "pids": hc.get("PidsLimit"),
            "cap_add": sorted(hc.get("CapAdd") or []), "cap_drop": sorted(hc.get("CapDrop") or []), "readonly_rootfs": hc.get("ReadonlyRootfs"),
            "privileged": hc.get("Privileged"), "init": hc.get("Init"), "ulimits": hc.get("Ulimits"), "extra_hosts": hc.get("ExtraHosts"),
            "dns": hc.get("Dns"), "security_opt": hc.get("SecurityOpt"), "tmpfs": hc.get("Tmpfs"),
            "log": [(hc.get("LogConfig") or {}).get("Type"), (hc.get("LogConfig") or {}).get("Config")], "env": envd}
c, im = load(tag)
cfg, hc, env, nets = parts(c, im)
if mode == "args":
    default_hc = extra[0]
    a = []
    if hc.get("Privileged"):
        sys.stderr.write("refusing: the original container is privileged\n"); sys.exit(3)
    if len(nets) != 1:
        sys.stderr.write("refusing: expected exactly one network, found %d\n" % len(nets)); sys.exit(3)
    rp = hc.get("RestartPolicy") or {}
    if rp.get("Name"):
        a += ["--restart", rp["Name"] + (":%d" % rp["MaximumRetryCount"] if rp.get("MaximumRetryCount") else "")]
    if cfg.get("User"): a += ["--user", cfg["User"]]
    if cfg.get("WorkingDir"): a += ["--workdir", cfg["WorkingDir"]]
    if cfg.get("Entrypoint"): a += ["--entrypoint", cfg["Entrypoint"][0]]
    if cfg.get("StopTimeout"): a += ["--stop-timeout", str(cfg["StopTimeout"])]
    if hc.get("Memory"): a += ["--memory", str(hc["Memory"])]
    if hc.get("MemorySwap"): a += ["--memory-swap", str(hc["MemorySwap"])]
    if hc.get("MemoryReservation"): a += ["--memory-reservation", str(hc["MemoryReservation"])]
    if hc.get("NanoCpus"): a += ["--cpus", "%.3f" % (hc["NanoCpus"] / 1e9)]
    if hc.get("CpuShares"): a += ["--cpu-shares", str(hc["CpuShares"])]
    if hc.get("CpusetCpus"): a += ["--cpuset-cpus", hc["CpusetCpus"]]
    if hc.get("PidsLimit") and hc["PidsLimit"] > 0: a += ["--pids-limit", str(hc["PidsLimit"])]
    for k in hc.get("CapAdd") or []: a += ["--cap-add", k]
    for k in hc.get("CapDrop") or []: a += ["--cap-drop", k]
    for k in hc.get("SecurityOpt") or []: a += ["--security-opt", k]
    if hc.get("ReadonlyRootfs"): a += ["--read-only"]
    if hc.get("Init"): a += ["--init"]
    for u in hc.get("Ulimits") or []: a += ["--ulimit", "%s=%s:%s" % (u["Name"], u["Soft"], u["Hard"])]
    for h in hc.get("ExtraHosts") or []: a += ["--add-host", h]
    for d in hc.get("Dns") or []: a += ["--dns", d]
    for p, o in (hc.get("Tmpfs") or {}).items(): a += ["--tmpfs", p + (":" + o if o else "")]
    lc = hc.get("LogConfig") or {}
    if lc.get("Type"): a += ["--log-driver", lc["Type"]]
    for k, v in (lc.get("Config") or {}).items(): a += ["--log-opt", "%s=%s" % (k, v)]
    for m in c.get("Mounts") or []:
        a += ["--mount", "type=%s,source=%s,target=%s%s" % (m["Type"], m.get("Name") or m.get("Source"), m["Destination"], "" if m.get("RW") else ",readonly")]
    (net, aliases), = nets.items()
    a += ["--network", net]
    for al in aliases: a += ["--network-alias", al]
    h = health(cfg)
    if h:
        t = h["Test"]
        a += ["--health-cmd", t[1] if t[0] == "CMD-SHELL" else shlex.join(t[1:])]
        for flag, key in (("--health-interval", "Interval"), ("--health-timeout", "Timeout"), ("--health-start-period", "StartPeriod")):
            if h.get(key): a += [flag, "%gs" % (h[key] / 1e9)]
        if h.get("Retries"): a += ["--health-retries", str(h["Retries"])]
    else:
        a += ["--health-cmd", default_hc, "--health-interval", "20s"]
    sys.stdout.write("".join(x + "\0" for x in a))
elif mode == "cmd":
    sys.stdout.write("".join(x + "\0" for x in (cfg.get("Entrypoint") or [])[1:] + (cfg.get("Cmd") or [])))
elif mode == "env":
    sys.stdout.write("\n".join(env) + "\n")
elif mode == "diff":
    flag = extra[0]          # "-" = a service (backend/frontend): nothing is enforced, everything must match
    skip = () if flag == "-" else ("MCX_FUTURES_MODE", "MCX_FUTURES_PAPER_ENABLED", "MCX_FUTURES_TRADING_ENABLED", flag)
    n, nim = load(extra[1])
    a, b = canon(c, im, skip), canon(n, nim, skip)
    bad = []
    for k in sorted(a):
        if a[k] != b[k]:
            bad.append("%s: original=%s new=%s" % (k, json.dumps(a[k])[:200], json.dumps(b[k])[:200]))
    newenv = dict(e.partition("=")[::2] for e in n["Config"].get("Env") or [])
    for k, v in (() if flag == "-" else (("MCX_FUTURES_MODE", "PAPER"), ("MCX_FUTURES_PAPER_ENABLED", "true"), ("MCX_FUTURES_TRADING_ENABLED", "true"), (flag, "true"))):
        if newenv.get(k) != v:
            bad.append("env %s must be %s, is %s" % (k, v, newenv.get(k)))
    print("\n".join(bad) if bad else "effective configuration identical (entrypoint, command, user, workdir, mounts, network+aliases, restart, limits, log, env) apart from the image%s" % ("" if flag == "-" else " and the enforced PAPER flags"))
    sys.exit(1 if bad else 0)
PYGEN
)
cmdof() { docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' "$1" 2>/dev/null; }
expo() { docker exec ai_trade-postgres-1 sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"select (select count(*) from mcx_futures_trades where instrument='$1' and state in ('OPEN','PENDING_PAPER_ENTRY','UNRESOLVED_EXPOSURE')) + (select count(*) from mcx_futures_cursors where instrument='$1' and (coalesce((state_json::json->'position')::text,'null')<>'null' or coalesce((state_json::json->'pending_entry')::text,'null')<>'null'))\"" 2>/dev/null | tr -d '[:space:]'; }
gen() { docker run --rm --network none --user "$(id -u):$(id -g)" -v "$TMPD":/in:ro $BI python -c "$GEN" "$@"; }
snap() { docker inspect --type container "$1" > "$TMPD/$2.c.json" 2>/dev/null && docker inspect --type image "$(docker inspect -f '{{.Image}}' "$1")" > "$TMPD/$2.i.json" 2>/dev/null; }
health_of() { docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null; }
wait_healthy() { local k; for k in $(seq 1 40); do case "$(health_of $1)" in running/healthy) return 0;; running/*) sleep 3;; *) return 1;; esac; done; return 1; }
# put the previous container back under its name and verify it runs (every step checked)
putback() { local N=$1 K=$2 ok=1
  if docker inspect $N >/dev/null 2>&1; then docker rm -f $N >/dev/null 2>&1; fi
  if docker inspect $N >/dev/null 2>&1; then crit "$N: cannot remove the replacement"; ok=0; fi
  if [ $ok = 1 ]; then docker rename $K $N || { crit "$N: cannot rename $K back"; ok=0; }; fi
  if [ $ok = 1 ]; then docker start $N >/dev/null || { crit "$N: cannot start the previous container"; ok=0; }; fi
  if [ $ok = 1 ]; then sleep 5; [ "$(docker inspect -f '{{.State.Status}}' $N 2>/dev/null)" = running ] || { crit "$N: previous container is not running"; ok=0; }; fi
  if [ $ok = 1 ]; then echo "restored the previous $N (running)"; else echo "CRITICAL: $N is NOT running. Manual: docker rm -f $N; docker rename $K $N; docker start $N"; fi
  [ $ok = 1 ]; }
# stage a replacement of $1 on image $2 from the CURRENT container's effective configuration (nothing changes yet)
stage() { local N=$1 IMG=$2 DEFHC=$3; shift 3
  docker rm -f $N-staged-$S >/dev/null 2>&1
  snap $N cur || { bad "$N: cannot read its configuration"; return 1; }
  gen args cur "$DEFHC" > "$TMPD/args.bin" || { bad "$N: cannot derive docker options"; return 1; }
  gen cmd cur > "$TMPD/cmd.bin" && gen env cur > "$TMPD/env.txt" || { bad "$N: cannot derive command/env"; return 1; }; chmod 600 "$TMPD/env.txt"
  mapfile -d '' -t ARGS < "$TMPD/args.bin"; mapfile -d '' -t CMD < "$TMPD/cmd.bin"
  [ ${#ARGS[@]} -gt 0 ] || { bad "$N: empty derived configuration"; return 1; }
  docker create --name $N-staged-$S "${ARGS[@]}" --env-file "$TMPD/env.txt" "$@" $IMG "${CMD[@]}" >/dev/null || { bad "$N: docker create rejected the reproduced configuration; nothing changed"; docker rm -f $N-staged-$S >/dev/null 2>&1; return 1; }; }
# swap the staged container in for $1 (current one must already be stopped); verify; roll back on any failure
activate() { local N=$1 FLAG=$2 MUSTCMD=$3 K=$1-pre-$S
  docker rename $N $K || { crit "$N: cannot rename to $K"; docker rm -f $N-staged-$S >/dev/null 2>&1; docker start $N >/dev/null 2>&1; return 1; }
  docker rename $N-staged-$S $N || { docker rm -f $N-staged-$S >/dev/null 2>&1; putback $N $K; return 1; }
  docker start $N >/dev/null || { echo "$N: start failed"; putback $N $K; return 1; }
  wait_healthy $N || { echo "$N: not healthy ($(health_of $N))"; putback $N $K; return 1; }
  if [ -n "$MUSTCMD" ]; then local PT; PT=$(docker top $N -o pid,args 2>/dev/null); if ! echo "$PT" | grep -q "$MUSTCMD" || echo "$PT" | grep -qi uvicorn || [ "$(docker logs $N 2>&1 | grep -c MCX_FUTURES_PAPER_STARTED)" -lt 1 ]; then echo "$N: process is not the worker: $(echo $PT | cut -c1-140)"; putback $N $K; return 1; fi; fi
  snap $K old && snap $N new && gen diff old $FLAG new || { echo "$N: configuration differs from the previous container"; putback $N $K; return 1; }
  echo "OK $N -> $(docker inspect -f '{{.Config.Image}}' $N) (previous kept as $K)"; }
api_check() { local p out; for p in /api/health /api/portfolio/header /api/portfolio/summary /api/portfolio/positions /api/portfolio/orders /api/portfolio/trades /api/portfolio/performance /api/portfolio/risk /api/portfolio/strategies /api/portfolio/alerts /api/mcx-futures/status /api/engines/status /api/phase15/summary; do out=$(docker exec ai_trade-backend-1 python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000$p',timeout=90).status)" 2>&1 | tail -1); echo "  $p -> $out"; [ "$out" = 200 ] || return 1; done; }
page_check() { local p c; for p in / /positions /orders /tradebook /strategies /analytics /risk /research /system /settings /engines; do c=$(docker exec ai_trade-frontend-1 node -e "fetch('http://127.0.0.1:3000$p').then(r=>console.log(r.status)).catch(()=>console.log('ERR'))" 2>&1 | tail -1); echo "  $p -> $c"; [ "$c" = 200 ] || return 1; done; }
service() { local N=$1 IMG=$2 HC=$3 CHECK=$4
  echo "================ $N ================"
  if docker inspect $N-pre-$S >/dev/null 2>&1; then [ "$(docker inspect -f '{{.Config.Image}}' $N)" = "$IMG" ] && { echo "SKIP $N: already on $IMG"; return 0; }; bad "$N: $N-pre-$S exists but $N is not on $IMG; resolve by hand"; return 1; fi
  stage $N $IMG "$HC" || return 1
  docker stop -t 30 $N >/dev/null || { crit "$N: docker stop failed"; docker rm -f $N-staged-$S >/dev/null 2>&1; return 1; }
  activate $N - "" || return 1
  $CHECK || { echo "$N: functional checks failed: rolling back"; putback $N $N-pre-$S; return 1; }; }
worker() { local I=$1; local W=mcx-futures-$I; local U=$(echo $I | tr a-z A-Z); local FLAG=MCX_FUTURES_${U}_ENABLED E0 E1 E2 k
  local HC="python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument $U --health"
  echo "================ $W ================"
  if docker inspect $W-pre-$S >/dev/null 2>&1; then [ "$(docker inspect -f '{{.Config.Image}}' $W)" = "$BI" ] && { echo "SKIP $W: already on $BI"; return 0; }; bad "$W: $W-pre-$S exists but $W is not on $BI; resolve by hand"; return 1; fi
  case "$(cmdof $W)" in *mcx_futures_paper_worker.py*) ;; *) bad "$W does not run the worker command ($(cmdof $W)); run the recovery block first"; return 1;; esac
  E0=$(expo $U); if ! isnum "$E0"; then bad "$W: cannot read the persisted exposure; not touching it"; return 1; fi
  if [ "$E0" != 0 ]; then echo "DEFERRED $W: $E0 open/pending record(s). The running worker keeps managing it; re-run this block when flat."; DEFERRED=$((DEFERRED+1)); return 0; fi
  stage $W $BI "$HC" -e MCX_FUTURES_MODE=PAPER -e MCX_FUTURES_PAPER_ENABLED=true -e $FLAG=true -e MCX_FUTURES_TRADING_ENABLED=true || return 1
  if [ -n "$KS" ] && [ ! -e "$KS" ]; then touch "$KS" && KS_MADE=1 && echo "kill switch ON ($KS): no new paper entries while $W is replaced; waiting 35 s for in-flight entries to resolve"; sleep 35; fi
  E1=$(expo $U); if ! isnum "$E1" || [ "$E1" != 0 ]; then echo "DEFERRED $W: exposure '$E1' appeared before the stop; worker left running"; docker rm -f $W-staged-$S >/dev/null 2>&1; [ "$KS_MADE" = 1 ] && rm -f "$KS" && KS_MADE=0; DEFERRED=$((DEFERRED+1)); return 0; fi
  docker stop -t 30 $W >/dev/null || { crit "$W: docker stop failed"; docker rm -f $W-staged-$S >/dev/null 2>&1; return 1; }
  [ "$(docker inspect -f '{{.State.Running}}' $W 2>/dev/null)" = false ] || { crit "$W: still running after stop"; docker rm -f $W-staged-$S >/dev/null 2>&1; return 1; }
  E2=$(expo $U); if ! isnum "$E2" || [ "$E2" != 0 ]; then echo "$W: exposure '$E2' persisted at the stop: restarting the same worker to manage it"; docker rm -f $W-staged-$S >/dev/null 2>&1; docker start $W >/dev/null && sleep 5 && [ "$(docker inspect -f '{{.State.Status}}' $W)" = running ] || crit "$W: could not restart the worker that holds exposure"; [ "$KS_MADE" = 1 ] && rm -f "$KS" && KS_MADE=0; return 1; fi
  [ "$KS_MADE" = 1 ] && rm -f "$KS" && KS_MADE=0 && echo "kill switch OFF (old worker stopped and flat; new worker starts with entries enabled)"
  activate $W $FLAG mcx_futures_paper_worker.py; }
DEFERRED=0
echo "== 1/5 build images (a failed build changes nothing)"
git fetch -q origin feature/trading-terminal || { echo "ABORT: fetch failed"; exit 1; }
B=/home/ubuntu/build-$S; [ -d $B ] || git worktree add -f $B $SHA >/dev/null; [ "$(git -C $B rev-parse HEAD 2>/dev/null)" = "$SHA" ] || { echo "ABORT: build dir is not at $SHA"; exit 1; }
docker build -q -f $B/backend/Dockerfile -t $BI $B >/dev/null || { echo "ABORT: backend build failed; nothing changed"; exit 1; }
docker build -q --build-arg NEXT_PUBLIC_API_BASE_URL= --build-arg NEXT_PUBLIC_POLL_INTERVAL_MS=30000 -t $FI $B/frontend >/dev/null || { echo "ABORT: frontend build failed; nothing changed"; exit 1; }
echo "== 2/5 migration head must already be 0025"; MH=$(docker run --rm --network $NET --env-file .env.production $BI sh -c 'cd /app && alembic current 2>/dev/null | tail -1'); echo "$MH"; case "$MH" in 0025_mcx_futures_paper*) ;; *) echo "ABORT: database is not at 0025; nothing changed"; exit 1;; esac
KF=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' mcx-futures-crudeoilm 2>/dev/null | sed -n 's/^MCX_FUTURES_KILL_SWITCH_FILE=//p'); KF=${KF:-/app/control/MCX_FUTURES_KILL}
KD=$(docker inspect -f '{{range .Mounts}}{{.Destination}}|{{.Source}}{{println}}{{end}}' mcx-futures-crudeoilm 2>/dev/null | awk -F'|' -v d="$(dirname $KF)" '$1==d{print $2}'); [ -n "$KD" ] && KS="$KD/$(basename $KF)"
if [ -n "$KS" ] && [ -e "$KS" ]; then echo "NOTE: a kill switch is already present ($KS); it was set by someone else and is left untouched"; fi
[ -n "$KS" ] && echo "kill switch file on the host: $KS" || echo "WARNING: no kill-switch mount found; workers are replaced only when flat, re-checked after the stop"
echo "== 3/5 backend"; service ai_trade-backend-1 $BI "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5)\"" api_check || { bad "backend not replaced (previous backend restored)"; echo "STOP: frontend and workers are left unchanged because the backend did not deploy"; }
if [ "$FAILS" -eq 0 ]; then
  echo "== 4/5 frontend"; service ai_trade-frontend-1 $FI "node -e \"fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"" page_check || bad "frontend not replaced (previous frontend restored)"
  echo "== 5/5 MCX workers (each only when flat; the other keeps running meanwhile)"
  for I in crudeoilm natgasmini; do worker $I || bad "$I worker not replaced (see above)"; done
fi
echo "################ FINAL STATE ################"
for N in ai_trade-backend-1 ai_trade-frontend-1 mcx-futures-crudeoilm mcx-futures-natgasmini acts-paper; do echo "$N: $(health_of $N) image=$(docker inspect -f '{{.Config.Image}}' $N 2>&1) cmd=[$(cmdof $N | cut -c1-90)]"; done
[ "$DEFERRED" -gt 0 ] && echo "NOTE: $DEFERRED worker replacement(s) deferred because of open exposure; re-run this block later"
if [ "$FAILS" -eq 0 ] && [ "$CRIT" -eq 0 ]; then echo "RELEASE 2: PASS"; exit 0; else echo "RELEASE 2: FAILED (failures=$FAILS critical=$CRIT) - paste this whole output"; exit 1; fi
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

## Pending deliverables (explicit)

- Historical backfill for the MCX engines: NOT implemented (no historical MCX data source is integrated). Warm-up currently needs live observations only (54 bars: 162 min CRUDEOILM, 270 min NATGASMINI); bars persist across restarts. Retained as a required future deliverable; it must not delay worker recovery.
- Release 2 deployment block to be re-issued with the same safety protocol as Block 3b v2 (kill-switch quiesce, exposure re-check after stop, configuration diff).

## Not included, by instruction

Historical MCX data acquisition and strategy research were stopped on request; the MCX engines remain
`PRE_REGISTERED_UNVALIDATED` and are judged from forward paper results only.

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

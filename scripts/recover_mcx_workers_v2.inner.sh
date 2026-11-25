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
        sdate = ss.get("session_date") or str(ss.get("last_entry_time") or "")[:10]   # 2438b3b API has no session_date field
        check("%s session metadata is for today (last entry / forced exit dated today)" % i, sdate == str(datetime.now(IST).date()) and str(ss.get("forced_exit_time") or "")[:10] == sdate, "-> %s" % sdate)
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

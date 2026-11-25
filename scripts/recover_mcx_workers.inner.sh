set -uo pipefail
cd /home/ubuntu/AI_Trade; S=2438b3b; BI=ai-trade-mcx-futures:$S; NET=ai_trade_app_network; FAILS=0
cmp1() { A=$(docker inspect -f "$1" $2 2>&1); B=$(docker inspect -f "$1" $3 2>&1); if [ "$A" = "$B" ]; then echo "  same    $4: $A"; else echo "  DIFFERS $4"; echo "     old: $A"; echo "     new: $B"; fi; }
echo "################ 1. DIAGNOSIS (read-only): previous worker vs the container that replaced it ################"
for I in crudeoilm natgasmini; do W=mcx-futures-$I; K=$W-pre-$S
  echo "== $W   (old = $K, new = $W)"
  cmp1 '{{.Config.Image}}' $K $W image-tag
  cmp1 '{{json .Config.Entrypoint}}' $K $W Entrypoint
  cmp1 '{{json .Config.Cmd}}' $K $W Cmd
  cmp1 '{{.Config.WorkingDir}} user={{.Config.User}}' $K $W workdir+user
  cmp1 '{{json .Config.Healthcheck.Test}}' $K $W Healthcheck
  cmp1 '{{.HostConfig.RestartPolicy.Name}}' $K $W restart-policy
  cmp1 'mem={{.HostConfig.Memory}} cpus={{.HostConfig.NanoCpus}}' $K $W resource-limits
  cmp1 '{{range .Mounts}}{{.Type}}:{{.Source}}>{{.Destination}} {{end}}' $K $W mounts
  cmp1 '{{range $n,$v := .NetworkSettings.Networks}}{{$n}} aliases={{$v.Aliases}} {{end}}' $K $W networks
  echo "  env MCX flags old: $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $K | grep -E '^MCX_FUTURES_' | tr '\n' ' ')"
  echo "  env MCX flags new: $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $W | grep -E '^MCX_FUTURES_' | tr '\n' ' ')"
  echo "  env variable NAMES only in one of them: $(diff <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $K | cut -d= -f1 | sort) <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $W | cut -d= -f1 | sort) | grep '^[<>]' | tr '\n' ' ')"
  echo "  process now running in new container:"; docker top $W -o pid,args 2>&1 | sed 's/^/     /' | cut -c1-160
done
echo "CONCLUSION GUIDE: a DIFFERS on Cmd with new = uvicorn and old = python /app/worker/mcx_futures_paper_worker.py means the replacement lost the command."
echo "################ 2. FIX: recreate ONLY the two workers with the previous container's exact command ################"
fix() { I=$1; W=mcx-futures-$I; U=$(echo $I | tr a-z A-Z); KEEP=$W-pre-$S; BAD=$W-badcmd-$S; HC="python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument $U --health"
  docker inspect $KEEP >/dev/null 2>&1 || { echo "ABORT $W: rollback copy $KEEP is missing"; return 1; }
  case "$(docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' $W 2>/dev/null)" in *mcx_futures_paper_worker.py*) echo "SKIP $W: already runs the worker command"; return 0;; esac
  mapfile -t CMD < <(docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' $KEEP)
  case "${CMD[*]}" in *mcx_futures_paper_worker.py*) ;; *) echo "ABORT $W: the old container's command is not the worker: ${CMD[*]}"; return 1;; esac
  echo "== $W: worker command taken from the old container: ${CMD[*]}"
  EXP=$(docker exec ai_trade-postgres-1 sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"select count(*) from mcx_futures_trades where instrument='$U' and state in ('OPEN','PENDING_PAPER_ENTRY','UNRESOLVED_EXPOSURE')\"")
  MOUNTS=$(docker inspect -f '{{range .Mounts}}-v {{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}:{{.Destination}}{{if not .RW}}:ro{{end}} {{end}}' $KEEP)
  rb() { echo "!! ROLLBACK $W: $1"; docker rm -f $W >/dev/null 2>&1; docker start $KEEP >/dev/null && docker rename $KEEP $W && echo "restored the previous worker as $W"; return 1; }
  docker rename $W $BAD && docker stop $BAD >/dev/null
  [ "$EXP" = 0 ] || { rb "exposure=$EXP, not replacing"; return 1; }
  docker run -d --name $W --network $NET --network-alias $W --restart unless-stopped --env-file <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $KEEP | grep -v '^NEXT_PUBLIC_') $MOUNTS -e MCX_FUTURES_TRADING_ENABLED=true --log-opt max-size=10m --log-opt max-file=5 --health-cmd "$HC" --health-interval 20s $BI "${CMD[@]}" >/dev/null || { rb "run failed"; return 1; }
  for k in $(seq 1 40); do st=$(docker inspect -f '{{.State.Status}}' $W); s=$(docker inspect -f '{{.State.Health.Status}}' $W); [ "$st" = running ] || break; [ "$s" = healthy ] && break; sleep 3; done
  L=$(docker logs $W 2>&1 | grep -c MCX_FUTURES_PAPER_STARTED)
  if [ "$st" = running ] && [ "$s" = healthy ] && [ "$L" -ge 1 ]; then echo "OK $W: worker running (health=$s, start log lines=$L)"; docker stop $KEEP >/dev/null 2>&1; else rb "status=$st health=$s start_log=$L"; fi; }
for I in crudeoilm natgasmini; do fix $I; done
echo "################ 3. VERIFY: process, flags, advancing journal, fresh source quotes, warm-up, decisions, config hash ################"
for W in mcx-futures-crudeoilm mcx-futures-natgasmini; do echo "$W: process=[$(docker top $W -o args 2>/dev/null | tail -n +2 | tr '\n' ' ' | cut -c1-140)] state=$(docker inspect -f '{{.State.Status}}/{{.State.Health.Status}} restarts={{.RestartCount}}' $W) flags: $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $W | grep -E '^MCX_FUTURES_(MODE|PAPER_ENABLED|TRADING_ENABLED)=' | tr '\n' ' ')"; docker logs --tail 5 $W 2>&1 | grep -viE 'key|token|secret|password|authorization' | cut -c1-210 | sed 's/^/     /'; done
SEQ() { docker exec ai_trade-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select instrument||'"'"' '"'"'||sequence||'"'"' '"'"'||coalesce(observed_at,'"'"'-'"'"') from mcx_futures_cursors order by 1"'; }
echo "waiting 75 s for the first observations..."; sleep 75; SA=$(SEQ); echo "$SA"; echo "waiting another 60 s to prove the journal advances..."; sleep 60; SB=$(SEQ); echo "$SB"
for I in CRUDEOILM NATGASMINI; do a=$(echo "$SA" | awk -v i=$I '$1==i{print $2}'); b=$(echo "$SB" | awk -v i=$I '$1==i{print $2}'); if [ -n "$a" ] && [ -n "$b" ] && [ "$b" -gt "$a" ]; then echo "PASS $I journal sequence advanced $a -> $b"; else echo "FAIL $I journal sequence did not advance ($a -> $b)"; FAILS=$((FAILS+1)); fi; done
echo "-- persisted runtime flags written by the NEW workers"; docker exec ai_trade-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select instrument||'"'"' paper_entries_enabled='"'"'||coalesce(latest_json::json->>'"'"'paper_entries_enabled'"'"','"'"'MISSING'"'"')||'"'"' execution_state='"'"'||coalesce(latest_json::json->>'"'"'execution_state'"'"','"'"'MISSING'"'"') from mcx_futures_cursors order by 1;"'
docker exec -i -w /app ai_trade-backend-1 python - <<'PY'
import json, sys, urllib.request
g = lambda p: json.load(urllib.request.urlopen("http://127.0.0.1:8000" + p, timeout=60))
bad = []
def chk(label, ok, detail=""):
    print("  %-4s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok: bad.append(label)
hd = g("/api/portfolio/header"); mcx_open = (hd.get("mcx") or {}).get("status") == "OPEN"
print("  MCX session now:", json.dumps(hd.get("mcx")))
for m in g("/api/mcx-futures/status")["instruments"]:
    i = m["instrument"]; f = m.get("feed") or {}; w = m.get("warmup") or {}; d = m.get("last_decision") or {}
    print("\n  -- %s: state=%s execution=%s" % (i, m.get("state"), m.get("execution_state")))
    print("     feed: last_quote_at=%s observed_at=%s age_s=%s bid=%s ask=%s ltp=%s" % (f.get("last_quote_at"), f.get("observed_at"), f.get("age_seconds"), f.get("bid"), f.get("ask"), f.get("ltp")))
    print("     contract:", json.dumps(m.get("contract")))
    print("     warm-up: ready=%s bars=%s/%s blocking=%s" % (w.get("ready"), w.get("bars_available"), w.get("bars_required"), w.get("blocking")))
    print("     decision: at=%s status=%s dir=%s score=%s cycle=%s vetoes=%s" % (d.get("at"), d.get("status"), d.get("direction"), d.get("score"), d.get("cycle_state"), d.get("vetoes")))
    chk(i + " runtime execution_state is PAPER_ENABLED (persisted by the worker)", m.get("execution_state") == "PAPER_ENABLED", "-> %s" % m.get("execution_state"))
    chk(i + " flat (no open / unresolved exposure)", (m.get("open_position_count"), m.get("unresolved_exposure_count")) == (0, 0), "open=%s unresolved=%s" % (m.get("open_position_count"), m.get("unresolved_exposure_count")))
    if mcx_open:
        chk(i + " state is RUNNING during the MCX session", m.get("state") == "RUNNING", "-> %s %s" % (m.get("state"), m.get("state_reason")))
        chk(i + " source quote timestamp present", f.get("last_quote_at") is not None)
        chk(i + " quote age under 90 s", f.get("age_seconds") is not None and f["age_seconds"] < 90, "-> %s" % f.get("age_seconds"))
        chk(i + " a contract is subscribed", bool((m.get("contract") or {}).get("token")))
        chk(i + " warm-up information is reported", bool(w))
        chk(i + " a decision has been evaluated this session", bool(d.get("at")) and str(d["at"])[:10] == hd["now_ist"][:10], "at=%s" % d.get("at"))
    c = (g("/api/portfolio/strategies/mcx-" + i.lower()).get("config") or {})
    chk(i + " deployed config hash == the hash the worker persisted", c.get("matches_running_worker") is True)
print("\n  PYTHON CHECKS:", "ALL PASS" if not bad else "FAILED -> " + "; ".join(bad))
sys.exit(1 if bad else 0)
PY
[ $? -eq 0 ] || FAILS=$((FAILS+1))
echo "################ 4. PAPER-TRADING READINESS: warm-up, signal vs threshold, blockers, cycle, orders, exit rules ################"
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
[ $? -eq 0 ] || FAILS=$((FAILS+1))
[ "$FAILS" -eq 0 ] && echo "WORKER RECOVERY: PASS" || echo "WORKER RECOVERY: $FAILS CHECK GROUP(S) FAILED (paste this whole output)"

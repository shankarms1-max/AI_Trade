set -uo pipefail
cd /home/ubuntu/AI_Trade; S=2438b3b; BI=ai-trade-mcx-futures:$S; NET=ai_trade_app_network
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
sleep 40
echo "== result: running processes and persisted execution state"
for W in mcx-futures-crudeoilm mcx-futures-natgasmini; do echo "$W cmd=[$(docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' $W)] status=$(docker inspect -f '{{.State.Status}}/{{.State.Health.Status}}' $W) flags: $(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $W | grep -E '^MCX_FUTURES_(MODE|PAPER_ENABLED|TRADING_ENABLED)=' | tr '\n' ' ')"; docker logs --tail 4 $W 2>&1 | grep -viE 'key|token|secret|password|authorization' | cut -c1-200 | sed 's/^/     /'; done
docker exec ai_trade-backend-1 python -c "import json,urllib.request as u;d=json.load(u.urlopen('http://127.0.0.1:8000/api/mcx-futures/status',timeout=60));[print(i['instrument'],'state=',i['state'],'execution=',i['execution_state'],'last_quote=',(i.get('feed') or {}).get('last_quote_at'),'warm=',(i.get('warmup') or {}).get('ready')) for i in d['instruments']]"

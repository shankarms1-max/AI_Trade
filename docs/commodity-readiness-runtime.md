# Commodity ACTS runtime readiness

This patch is based on c774071a and affects Commodity only. The existing release
uses NAKED_SHORT_OPTION paper entries (SELL observed BID, later BUY observed ASK),
not NIFTY spreads. Stops and stress scenarios are not guaranteed loss bounds.
Alpha phase14_1_v1 and ACTS V2 formulas, gates and all risk/exit defaults are unchanged.

## History and memory

The original ordered 5,000-row history query is streamed, with every source row
receiving the original complete validation. Retain the true first capture of each
three-minute bucket in Alpha's unchanged 809-trading-minute dependency cone, all
recent fast-horizon observations, and exact opening/session extrema. Never use
`history_limit=1000` as a substitute. Context and complete Alpha outputs are tested
against the original full-history oracle for both instruments, including sparse
history and session boundaries. Research and paper share that immutable history
only during one observation; exceptions reset the cache. No required history is
truncated, no timestamp is synthesized, and no new history-budget failure blocks
position management.

Ubuntu-1 measurements: a real, paced CRUDE capture plus 5,000-row decision took
16.6552/14.0110 seconds, including the deliberate ten-second diagnostic inter-request
wait. Peak RSS was 213.63 MiB. These are two samples, not a long-run p95 claim.
Simultaneous, network-disabled two-process tests with 5,000 synthetic rows per
instrument took 6.23–12.07 seconds per decision; peak RSS was about 205 MiB each,
minimum host MemAvailable 706.99 MiB, no worker swap or OOM. Synthetic history is
capacity evidence only and never enters production journals. Live cold discovery,
long-running journal growth and simultaneous daytime shared-key throttling are
not established by the offline capacity test.

## Credential and process isolation

`COMMODITY_ACTS_MARKET_DATA_KEY_FILE` optionally reads an owner-private regular
file; symlinks, public permissions, oversized/empty/placeholder values are refused.
The key remains outside Docker environment/argv and is passed only to the existing
consumer-key-only market-data factory. The exposed facade has search and quotes
capabilities only. No login, TOTP, refresh, account or order API is added. The
original default remains disabled without an explicit opt-in.

On Ubuntu-1 the runtime secret is mounted read-only only into Commodity workers.
Do not put it in source, logs, Compose environment or command arguments. Preserve
its private ownership. NIFTY configuration, instances and credentials are untouched.

`worker/commodity_acts_health.py --instrument CRUDEOILM` performs database-only
freshness checks. It does not create a broker client, evaluate a strategy, write a
heartbeat or change journals. Closed sessions without exposure are healthy;
open sessions require fresh research AND paper cursors (90 seconds), with a short
session-start grace period. Expired contract evidence is unhealthy during sessions.
The same command supports NATGASMINI. Docker restart-on-process-exit is independent
of health monitoring; unhealthy positions are never silently repaired or closed.

## Approved explicit runtime policy

| Setting | CRUDEOILM | NATGASMINI |
| --- | ---: | ---: |
| Lot quantity | 10 | 250 |
| Cash loss exit trigger INR | 2,500 | 1,500 |
| Stress reservation ceiling INR | 6,000 | 8,000 |
| Daily loss budget INR | 6,000 | 8,000 |
| Stress move / premium multiple | 5% / 2x | 10% / 2x |
| Lots / own concurrent positions | 1 / 1 | 1 / 1 |
| Entries per session | 2 | 2 |

Global maximum two positions, INR14,000 stress reservation and INR14,000 daily loss.
Window 09:00–23:30 IST, no new decision after 22:29, forced exit 22:30, executable
quote age <=30 seconds, pre-expiry cutoff 20:00 one calendar day before expiry.
A forced-exit request still requires a later exact executable ASK to fill.
Unresolved exposure must remain unresolved without that evidence. Net P&L stays
unavailable without supported configured costs; margin/capital is not fabricated.

The October 22 evidence binds CRUDE future 573423 / November 19 to options November
17, and NAT future 570751 / October 27 to options October 23. Observed master
precision gives CRUDE futures/options ticks 1.00/0.05 and NAT ticks 0.10/0.05.
Do not use the earlier NAT diagnostic's incorrectly labelled documented future
tick (0.05); the retained raw master tick 10 / precision 2 establishes 0.10.
The base policy retains its originally dated evidence for reproducibility. With
`COMMODITY_ACTS_AUTO_CONTRACT_RENEWAL=true` (default false), actual master metadata
and fresh captures produce new evidence valid only through that verified session.
The explicit option `pAssetCode` must match exactly one future token; quantities,
precision/ticks and causal dates must validate. No independent nearest-future
assumption is used. The first entry-eligible expiry is selected using the unchanged
minimum DTE, TTL buffer and pre-expiry cutoff. The October 22 master identifies the
next eligible NAT option as November 20, linked to future 574320 / November 24;
that metadata lookup is not a claim of fresh November quote depth.

Held contracts pin capture while nonexpired, so rollover cannot drop executable
exit quotes. Pending and open trades keep their original stored policy/hash.
Renewal failures block new entries while frozen position management continues.
Evidence is appended through normal PAPER_OBSERVATION events and new trade policy
records; historical snapshots, policies and journals are never rewritten. Expired
unresolved exposure remains reserved and is never synthetically closed. Alpha
history resets on the actual future/option identity, never mixing contract months.

The next-session verification job uses only local read-only API endpoints. It
records genuine timestamps, Alpha/rank/warnings, decisions and actual simulated
fills. Warm-up or no natural trade is not an operational failure. Stale processing,
failed renewal, unresolved exposure or an unfilled forced exit is reported.

## Deployment scope

Only Ubuntu-1 /srv/commodity-acts. No migration is needed. Backend image overlay
contains only the Commodity history/cache/credential changes; PostgreSQL, frontend
and gateway are preserved. Workers retain 512 MiB memory caps, disable swap, use
separate offsets (CRUDE 3,18,33,48; NAT 7,22,37,52), and restart unless stopped.
The health command may be bind-mounted read-only at its normal /app/worker path.

Status/P&L, without broker requests:

```
cd /srv/commodity-acts
docker compose --project-name commodity_acts --env-file .env.production \
  -f compose.commodity.json exec -T crudeoilm \
  python /app/worker/commodity_acts_paper_worker.py --mode PAPER \
  --instrument CRUDEOILM --status
```

Replace crudeoilm/CRUDEOILM with natgasmini/NATGASMINI for the other instrument.
A running scheduler outside the session performs no market polling. Readiness,
Alpha warm-up, entry eligibility, actual fills and process liveness are separate.
Never report a trade without persisted simulated fill evidence.

The renewal follow-up uses versioned read-only source mounts and rolling Commodity
worker replacement, with the other worker kept running. No application image is
rebuilt. Runtime manifests record the mounted source commit and checksums separately
from the unchanged base image. NIFTY services are never accessed or changed.

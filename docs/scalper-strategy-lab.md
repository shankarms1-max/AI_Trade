# Phase 15 credit spread strategy lab

The fifth frozen strategy, `ACTS_V1`, is specified in
[ACTS_V1 research](acts-v1-research.md). The four original strategies below
retain their rules.

The lab compares five fixed, independent paper strategies on the same ordered Phase
15 snapshots. It is a separate offline package (`app.scalper_lab`) and CLI. It does
not change the live worker, live signal rules, existing paper journal, Phase 14.2,
or broker adapter. No order client is imported. SQLite input is opened with
`mode=ro`; PostgreSQL input is read within a `SET TRANSACTION READ ONLY`
transaction. The lab writes new files only in a user-supplied output directory.

## Inputs and causal evidence

Each strategy sees the same snapshot at time *t*. Price structure uses the observed
IST session only. Futures returns compare the exact `future_instrument_id` and
`future_expiry` at actual elapsed 3, 5 and 15 minute reference points. References
must be at or before their target time and no more than 1.5 × 15 seconds stale.
The opening range is the observed 09:15–09:30 range and is marked complete only
when the existing Phase 15 80% sample coverage, early start and late end tests pass.

VWAP is accepted **only** from an optional companion export of the broker's
[documented](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/quotes.md)
futures `avg_cost` (volume weighted average), `last_volume` (actual
cumulative futures volume), and `lstup_time`. The export must include the exact
futures symbol, expiry and token; its LTP must agree with the captured futures
price within 0.01; and its timestamp must be aware and at most 30 seconds old.
`basis` must be `BROKER_VOLUME_WEIGHTED_AVERAGE`, and `source_fields` must list
`avg_cost`, `last_volume`, `lstup_time`. A 1 minute VWAP slope is
`10,000 × (VWAP_t / VWAP_t−1 − 1) / elapsed_minutes` for the same token with
nondecreasing cumulative volume. Missing or unverified data yields an explicit
unavailable reason and a null VWAP/slope. The ordinary Phase 15 captures do not
persist the futures volume and broker VWAP required here. A and B therefore fail
closed on those captures unless a genuine matching broker export is supplied.
Sampled futures LTP, index price and index pseudo-volume are never substituted.

For every option, history is keyed by all six exact identity fields: exchange,
instrument token, expiry, strike, type and trading symbol. Current and historical
OI must have a source timestamp within 30 seconds of their respective captures.
`ΔOI_h = OI_t − OI_reference` for 1/3/5/15 minutes. No broker `oi_change` field
is read. Premium is the bid/ask midpoint. OI rising while premium is flat/falling
is **WRITING**; OI and premium rising is **LONG_BUILDUP**; OI falling while
premium rises is **SHORT_COVERING**; both falling/flat is **LONG_UNWINDING**.
Missing exact history, premium or freshness yields **UNKNOWN**, never zero.

Local PCR uses the complete CE and PE basket at the *current* ATM ±3 or ±5
50-point strikes. OI PCR is `sum(PE OI) / sum(CE OI)`; volume PCR uses the same
ratio of current cumulative contract volumes. The 1/3/5 minute PCR baseline uses
the **same current contract basket** at each historical reference. Change is
current PCR minus baseline PCR; slope is change divided by actual elapsed minutes.
The same changes and slopes are recorded separately for OI PCR and volume PCR.
Missing or ambiguous strike/type members, a nonpositive call denominator or
missing data yields null. Ambiguous option strikes block all spread candidates.
PCR is persisted context, never a standalone entry trigger. A wall is the
highest total OI cluster among a center strike and its immediate ±50 neighbors,
within 500 points of spot on the relevant OTM side. Its cluster ΔOI determines
strengthening or weakening; its center's premium/OI behavior classifies the
direction SUPPORTIVE, NEUTRAL or CONTRADICTORY.

The shared directional regime is **BULLISH** or **BEARISH** when same-contract
futures 3/5/15 minute returns reach respectively +2/+4/+8 basis points in that
direction, observed session range location is at least 0.6 (bull) or at most
0.4 (bear), and a completed opening range is not contradicted. When authoritative
VWAP is present, futures price and VWAP slope must align. With missing VWAP, the
regime can still be directional on futures trend and structure alone, labelled
`FUTURES_TREND_STRUCTURE_VWAP_UNAVAILABLE`; A and B remain closed because their
own VWAP requirement is explicit. Otherwise the regime is **RANGE**. Each strategy
has its own setup and invalidation rules.

## Fixed strategy rules

All agents return `NO_SETUP`, `WATCH` or `ENTER`. A watch expires after eight
observations (two minutes). Fast timing is the sign of the latest 15-second spot
change exceeding 0.5 point. It is never the market thesis or an exit score.

| Strategy | Watch | Enter |
| --- | --- | --- |
| `VWAP_OI_REJECTION` | Directional futures/VWAP regime, 1m writing at the relevant OI wall with strengthening cluster, futures pullback to within max(5 points, 5 bps) above/below VWAP | Next aligned fast move with futures distance from VWAP expanded at least 2 points |
| `ORB_RETEST` | Completed opening range broken by at least 5 points with aligned futures price and VWAP slope; then price retests within −2 to +5 points of the boundary | Price resumes at least 5 points beyond boundary with aligned fast timing |
| `OI_WALL` | Spot touches within 10 points of a writing CE resistance or PE support wall, or a wall weakens within 10 points before a break | Spot moves at least 2 points away from the writing wall with aligned timing; for a break, moves at least 5 points beyond the failed wall with the opposite wall writing |
| `TREND_PULLBACK` | Same-contract futures 5m/15m move at least 4/8 bps in one direction, session range location at least 0.55/at most 0.45, and the 1m spot move pulls back at least 2 bps | An aligned fast move takes spot at least 3 points beyond the watched pullback anchor |

After an `ENTER` setup, the shared executable constructor checks each sensible
short option first, then seeks a far hedge at 100/200/300/400 points. The lab
reranks *valid* candidates using distance to the relevant OI wall, 1m OI/premium
activity, the existing short-quality score (distance, premium, liquidity and
bid/ask quality), actual premium retention, width and stable identity. Max OI
alone cannot win. The existing builder and next-observation fill checks enforce
fresh exact bid/ask quotes, quantity units, OI, volume, minimum credit,
credit/width, one lot and defined maximum loss. A constant internal legacy signal
object is passed only to that constructor; no score gates an `ENTER` decision.
Each agent has its own watch, pending/open trade, daily limit, cooldown and P&L
state. Occupancy in one agent never blocks another.

Pending entries fill only on a later observation. The setup's directional thesis
is rechecked at fill, along with event/kill, TTL, 0DTE, actual fill credit and
defined risk. Mark and exit debit is short ask minus long bid. Missing quotes
cannot create a synthetic fill or exit. Open exposure without an executable book
at forced close or session end is labelled **UNRESOLVED**.

Hard exits are evaluated at the first valid mark: forced close, kill/event,
spread stop (`debit ≥ entry credit × configured stop multiple`), and maximum
rupee loss. No minimum holding period applies. Thesis exits then follow:

| Strategy | Thesis exits |
| --- | --- |
| `VWAP_OI_REJECTION` | Spot crosses the held OI wall adversely by over 10 points; the exact held wall's 1m OI falls at least 10% from its reference; or futures cross VWAP adversely on two consecutive marks |
| `ORB_RETEST` | Spot crosses the invalid side of the opening boundary by over 5 points immediately, or is on the invalid side for two consecutive marks |
| `OI_WALL` | Spot crosses the held wall adversely by over 10 points; a broken wall is re-entered adversely by over 5 points; exact wall OI unwinds at least 10% in 1m; or premium/OI flips to long buildup or short covering on two consecutive marks |
| `TREND_PULLBACK` | Spot crosses the watched pullback anchor adversely by over 5 points, or 5m and 15m futures trends both reverse to at least 4/8 bps opposite |

All four retain configured profit capture, trailing giveback and time exits. A
single noisy opposite 15-second price observation has no exit rule. Every entry
records the strategy, direction, thesis, evidence, short/hedge rationale and
invalidation conditions. Every exit stores its exact reason and contemporaneous
market evidence.

## Running and interpreting comparisons

```bash
python scripts/run_scalper_lab.py \
  --database /path/to/local-phase15.sqlite \
  --start 2026-10-08 --end 2026-10-08 \
  --output /path/to/new-lab-output
```

For PostgreSQL, use `--database-url` or set `DATABASE_URL` instead of
`--database`:

```bash
python scripts/run_scalper_lab.py \
  --database-url 'postgresql+psycopg://readonly_user:password@host:5432/database' \
  --start 2026-10-08 --end 2026-10-08 \
  --output /path/to/new-lab-output
```

The PostgreSQL connection uses the same snapshot and option-quote models, IST
date boundaries and capture-time/ID ordering as SQLite. Its transaction is
read-only; use a database role with `SELECT` privileges for another layer of
protection. The URL is never included in lab output artifacts.

An optional `--futures-csv` must contain `timestamp,source_timestamp,symbol,
expiry,instrument_token,ltp,cumulative_volume,vwap,basis,source_fields`.
`source_fields` is a JSON array within the CSV cell. For a chronological split,
replace `--start/--end` with `--research-start`, `--research-end`,
`--validation-start`, `--validation-end`; research must precede validation.
There is no optimizer, and parameters are fixed before both periods run.
Indicator history and agent state reset at the validation boundary.

Each run writes `contexts.jsonl` (full OI/PCR/VWAP evidence once per snapshot),
`decisions.jsonl` (one decision per strategy per snapshot, linked by
`evidence_snapshot_id`), `trades.jsonl`, `acts_ledger.jsonl`, a manifest with input/config hashes,
`summary.json`, and comparison tables in CSV and Markdown. The summary includes
observations, watches, qualified entries, executions, wins/losses, win rate,
gross P&L, average winner/loser, expectancy, profit factor, drawdown, worst
trade, loss streak, holding times, direction/VIX/regime/exit splits, unresolved
trades and blockers. The default cost schedule is incomplete, so results are
`GROSS_ONLY`, net P&L null and no profitability claim. A complete, dated
`CostSchedule` can be supplied to the library for net accounting.

If no genuine October 8 Phase 15 capture file and futures export are available,
there is no October 8 performance result. Synthetic fixtures test calculations
and causal behavior; they are not market research data.

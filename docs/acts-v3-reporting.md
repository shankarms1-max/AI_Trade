# ACTS V3 reporting on the eb6af5a production baseline

This change is read-only reporting/UI support. It does not include the V3
engine, change any worker, evaluate a strategy, or migrate a schema. All existing
production routes, ORB range evidence, diagnostics, quote/management fields,
pagination and responsive components are retained.

## Strategy identity and metrics

- ACTS trade identity comes from `document.strategy_version`, restricted to
  `ACTS_V2` and `ACTS_V3`. V1 and unsupported versions are excluded.
- Both `ACTS_VERSIONS` entries always exist, giving seven summary rows even
  before V3 has an observation or trade. Unobserved V3 has zero financial/count
  metrics and null signal fields; it never inherits the V2 cursor or evidence.
- The current cursor belongs exclusively to its declared version. Historical V2
  trade counts, realized/unrealized P&L, positions and rejections remain separate.
  Combined totals sum those independent projections using the original unknown
  and unpriced-expiry semantics.
- Optional V3 audit fields are allowlisted from the stored trade root/nested
  opportunity cycle. No cycle or strategy state is reconstructed.

## Bounded queries

`_summary_events` retains the production day/snapshot join, relevant-event
prefilter, compact PostgreSQL JSON projection, first rejection per trade and last
mark selection. ACTS versions are filtered **before** ranking and included in
the ranking partition, preventing a different version's event from hiding the
correct version's evidence. ORB rejection deduplication is unchanged. Malformed
selected evidence still fails closed per strategy; unrelated history is not
deserialized.

Historical ACTS signal lookup first finds the journal head using the sequence
primary key. An inner query reads at most **512 sequence slots**, in descending
sequence order, with its own LIMIT. Only outside that LIMIT are observation type
and strategy version filtered; at most one matching `latest`/timestamp projection
is returned per missing version. This query shape prevents PostgreSQL from
choosing a full journal scan for a rare historical version. No full event/watch
body is transferred or replayed by the application.

If a historical signal lies outside that tail, its current status, timestamp and
score remain unavailable. Its financial metrics still come from the complete
trade projection and today's bounded rejection/mark query. This avoids both a
94,000-event poll and relabelling current V3 signals as V2. The trade projection
is intentionally not tail-limited: earlier entries, exits today and lifetime
unpriced-expiry counts must remain accurate.

Positions/journal retain their bounded UNION query (`limit + 1`, maximum page
size 100). Latest marks are fetched only for returned trade IDs, associated with
their actual strategy version. Conflicting versions are excluded before mark
ranking. Existing V2 evidence without a version remains compatible; V3 marks
without a version are explicitly unavailable.

## Frontend

V2 and V3 have separate rows and journal filters. Both use ACTS score/band/Alpha
facts, while ORB, EMA and Supertrend retain their existing displays. The strategy
count comes from the supplied summary array. The UI does not recompute metrics,
opening ranges, strategy decisions or execution evidence.

## Validation

Regression tests cover mixed-version P&L and counts, cursor ownership, mark
versioning, optional audit evidence, malformed inputs and the existing production
APIs. A 94,000-event fixture runs on SQLite and disposable PostgreSQL 16. Its
PostgreSQL EXPLAIN assertion requires index scans inspecting at most 512 event
rows for historical signal lookup. It also checks bounded query counts and that
large journal watch bodies never reach the Python JSON projection.

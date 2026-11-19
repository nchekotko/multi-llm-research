# Prospective options research, version options-forward-v1

The options collector is part of the existing API process. Its OS lock prevents
two collectors sharing a state directory. Enable/disable and change settings at
`/v1/options/settings` or the console's options page. Settings changes require the
current revision. The default is disabled on a new installation. Production is
explicitly enabled after deployment and validation; it resumes after restarts.

## Evidence and selection

The authenticated AlgoPack client reads the market-wide options reference/quote
snapshot every 900 seconds. Selection uses only that snapshot: structural
candidates occupy at most half the budget; near-reference-price short maturities
for configured assets use the remaining slots. The default cap is 60 options.
This is a selected-universe pilot, not exhaustive real-time coverage of every
listed series. Prior closing/reference underlying price is used for selection,
not as an executable hedge or a valuation price.

Option and stock/futures underlying L2 books are collected with three bounded
HTTP workers, followed by a 30-second pause. The cycle duration is additional to
that pause. Each response stores request/receipt times, HTTP date, source
dataversion, sequence information, all returned depth and sanitized error type.
The compressed SQLite evidence has SHA-256 integrity checks; both discoveries
and book cycles are accessible through authenticated `/v1/options/cycles/{id}`.
No raw licensed data or credentials belong in Git.

The default database cap is 1 GiB, with at least 1 GiB free space. Hitting a cap
pauses writes of market evidence rather than deleting earlier observations.
No automatic retention deletion exists. Collection window is 09:00–24:00 Moscow;
actual freshness/session checks determine whether a book can be used.

## Detectors and boundaries

- Structural: strike ordering, European equidistant butterflies, European boxes.
  Screen uses exchange entry/exercise fee fields and one adverse tick per leg;
  long-box screen discounts at a fixed 40% annual scenario, reverse-box at zero.
  These are candidate filters, not predictions or certified arbitrage.
- Quote lag: unchanged option best prices alongside a movement in the underlying
  exceeding its current spread. No estimated delta, news timestamp or causal claim.
- Book imbalance: absolute imbalance >= 0.8 over the first three price levels.
- Liquidity vacuum: first-three-level depth falls below 30% of its previous value.

The latter three are features only. They do not generate unsupported PnL claims.
News classification, calibrated IV surfaces, trade flow, cancellations, passive
queue position and passive fill simulation are not implemented by this version.
Snapshot depth changes cannot distinguish cancellation from execution.

## Prospective execution scenario

Structural signals use a later book cycle after the configured minimum latency.
Each leg's request must start after the eligible entry time. Repricing uses depth
and correct bid/ask sides; missing depth, stale sessions, excessive feed delay,
crossed books and excessive receipt skew reject the observation. An opportunity
that no longer meets the entry threshold is cancelled. Outcomes are durable and
resume after restart; stale pending outcomes become unobservable, not zero PnL.

One package is crossed and closed after the configured horizon (default 300 s),
again with later depth. This is an all-legs visible-depth counterfactual, not an
atomic order or proof of fills. A partial real fill and subsequent hedge loss are
not modeled. Multiple signals sharing liquidity are not an aggregate portfolio.
Repeated signals are dependent observations, not independent trades.

Round-trip scenario costs include exchange fees on each side, configurable broker
fees, adverse ticks on each side and holding-period funding. Capital is a proxy:
sum of each leg's maximum available IMBUY/IMP/IMNP, not actual broker portfolio
margin. Exercise costs are used by the initial screen, not a round-trip that
closes before expiry. Brokerage is unknown by default, never silently certified
as zero. Such outcomes have `cost_incomplete` status. Even configured-cost results
remain scenarios with `certified_edge=false`.

Before drawing conclusions: register a fixed strategy and thresholds, separate
calibration and future OOS periods, group overlapping signals, include censored
entries/exits, use session/block resampling, and reconcile broker-specific fees,
capital and execution latency. HTTP sampling cannot establish subsecond edge.

## Operations

Source modules: `application/options_scanner.py`, `application/options_math.py`.
State: `<API state root>/options/research.sqlite3`. Production API state root:
`/srv/app/var/openrouter`. No LLM calls or trading orders are made.
Settings are immutable rows; each structural signal records its own settings.
The console provides controls, candidate/evaluation tables and evidence downloads.

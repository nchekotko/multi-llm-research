# Economic Calendar news and prospective option reactions

The `signal-news-bridge` container uses the pinned Economic Calendar image and
its existing SmartLab, OilPrice/EIA and CBR/Cbonds public-news collectors. It has
no account secrets, live Economic Calendar DB mounts, published ports or write
access to application code. Its only writable persistent mount is
`var/openrouter/news-bridge`. It polls with a 60-second pause; existing upstream
cache and source delays remain part of the observation, not zero-latency news.

The first successful batch of each feed is bootstrap/archive. Durable identities
preserve that status after restart. SmartLab's list parser supplies only a date;
no intraday publication time is inferred. Both bridge first-seen and consumer
ingestion/analysis-completion timestamps are stored. Article text is evidence,
not executable instructions. No LLM or paid model calls are used.

The MOEX discovery includes all option reference rows irrespective of the normal
scanner's asset filter. The matcher supports equities, indices, currencies and
commodities, and both premium/European and margined/American contracts when
listed. Aliases, exact tickers and explicitly labelled macro-exposure proxies
are rules, not verified causal links. Unrecognised new asset codes can remain
unclassified. The UI exposes catalogue group counts, matched series, selected
contracts and reasons; absent coverage is not a negative research finding.

Selection freezes up to 12 contracts per event, by asset/style/type, nearest
expiry and distance to the reference underlying price, using only information
available then. Up to 40 outstanding event-contract studies are collected in
addition to the ordinary scanner universe. Capacity exclusions are retained.
Call and put long scenarios are separate controls, not an ex-post directional
strategy. Reference prices are selection inputs, not executable prices.

Only non-bootstrap, newly observed events consumed within 180 seconds are
prospective. Precisely timestamped news older than one day is archived. The
bridge must be fresh. Revised content is retained as a separate version;
repeated/overlapping events are not independent observations.

Entry occurs on a subsequent usable book after actual processing plus configured
latency. Source dataversion must also be after eligibility. Purchase crosses ask,
sale crosses bid for one contract. Intermediate snapshots are retained; terminal
exit uses the configured horizon, default 300 seconds. Empty/crossed/stale books,
missing metadata and excessive sampling gaps produce unobservable outcomes, not
zero PnL. A pre-event baseline is used only if already received before first-seen;
otherwise it is explicitly missing. Post-receipt movement is not causality.

Costs: exchange BUYSELLFEE on both sides, an **unconfirmed ALOR scenario** of an
additional 1x exchange fee to the broker on each side, configured adverse ticks,
and holding-period funding on max(IMBUY, initial option value). This is a capital
proxy, not portfolio margin. Hedge trades, passive queue execution, financing
agreement terms, exercise and taxes are not simulated. Trades are never sent.

State is in the existing options research SQLite database, with event/study
tables and references to immutable hashed book cycles. Collection disk guards
apply; bridge also reserves 1 GiB free disk and caps its durable identity map.
Authenticated endpoint: `/v1/options/news`. Console: `#news`. Horizon, latency
and slippage inherit the settings captured from the options collector at event
creation. The actual brokerage tariff must be reconciled before certifying net
edge. No OOS profitability is claimed from bootstrap or synthetic QA fixtures.

Deployment QA on 2026-11-19: the upstream SmartLab parser substitutes its clock
when date_raw is absent. The bridge and consumer discard that value. Initial
records were normalized and duplicate versions consolidated, retaining the
first event and its actual observed books. Original rows remain in the SQLite
news_events_before_timestamp_fix and news_studies_before_timestamp_fix audit
tables. No historical entry or synthetic production news was created.

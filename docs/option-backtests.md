# Option inefficiency research engines

The four research directions have five deterministic signal families. These are
baseline hypotheses, not trained alpha models or certified arbitrage. No paid LLM
calls and no brokerage orders are made.

| Family | Signal using information already received | Position |
|---|---|---|
| lag | Reprice at current underlying with previous observed IV; require underlying move and price gap | Long undervalued call or put |
| continuation | Trailing underlying return crosses a predeclared threshold | Long call / put; one-unit underlying benchmark where contract/cost metadata exists |
| volatility | Trailing squared log returns / elapsed time annualized; forecast exceeds both leg IVs | Same-strike, same-expiry long straddle |
| skew | Centre IV exceeds adjacent wings, equal strike spacing | Long 1/-2/1 butterfly |
| term | Near expiry IV exceeds farther expiry IV at the same strike and underlying | Short near vertical / long far vertical with matching protective wings |

Continuation is a momentum baseline, not a claim that textual news predicts future
returns. Volatility is a trailing realized-variance baseline, not a fitted event
volatility forecast. The term position uses four legs (two protected verticals), not an unprotected
calendar. Every short leg has a same-expiry protective wing.
Smile and term gaps are relative-value hypotheses, not
model-free arbitrages. Future versions need rolling event-class estimation,
independent holdout, placebo/time-shift controls and multiple-testing correction.

## Valuation

European: generalized Black-Scholes / Black-76, inverse IV by bisection.
American: CRR tree with 32 steps, early-exercise max at each node; coarse scenario
Greeks by finite differences. Premium futures use zero carry and configured
discount rate. Margined futures use zero carry/discount as a scenario; daily VM
and actual exchange clearing are NOT simulated. European premium shares require
an explicit continuous dividend-yield assumption. The expiry hour (default 19:00 Moscow) is an explicit pricing scenario, not
a certified fixing time; positions still stop before the last-trading calendar day.
No missing dividend yield is
silently replaced with zero. Index/currency/commodity spot bases without a
tradable/carry mapping remain blocked even though their futures can be evaluated.

Pricing references: [QuantLib implementation](https://github.com/lballabio/QuantLib/blob/master/ql/pricingengines/blackformula.cpp),
[MOEX contract types](https://www.moex.com/s205),
[MOEX premium options FAQ](https://www.moex.com/msn/ru-stock-options-faq).
Premium European and margined American contracts must not share settlement or
assignment assumptions. Short American options are blocked until an assignment
engine exists. All positions must close before expiration day and within the
same Moscow calendar day; no exercise, corporate actions or overnight VM PnL.

## Replay and execution

Read only hash-verified books and the discovery snapshot explicitly attached to
each cycle. Never join historical books to today's reference table. Reports
contain the cycle/discovery hashes, settings and news manifest. Requests fix the
cutoff at submission; later arriving books cannot join that run. Metadata changes
invalidate a held position. Warm-up is chronological, gaps reset signal eligibility.
News gating uses recorded analysis-completion time and originally selected assets,
not an inferred publication timestamp. Corrected false issuer associations are
excluded: this is cleaned-data exploratory replay, not a blind live track record.

Signals use the current received snapshot and past history. Entry must occur on a
later request and source timestamp after configured latency. Exits use a later book
after the holding horizon. Depth is walked at ask for buys/bid for sells; received
and source times must be synchronized across legs. Insufficient depth censors the
whole package; atomic all-or-none fills are a scenario, not guaranteed legging.
Censored open positions halt new entries. No missing exit becomes zero PnL.

Costs include both sides of exchange fees plus a configured broker multiplier,
adverse ticks, and annual funding on a conservative gross leg-capital proxy.
Broker multiplier 1 is an unconfirmed ALOR scenario, not a verified account tariff.
IMSELL is required for short legs. No portfolio margin, tax or exercise fees are
claimed. Aggregate strategy PnL is null if any trade is censored or no trade closes;
completed-only PnL is separately labelled and never treated as full-sample profit.

Hedge modes: none, static initial delta, dynamic delayed rebalancing. Quantities
are integral exchange lots/contracts using currency delta and actual ruble point
value; rounding leaves residual delta. Each hedge order is filled only on a later
usable snapshot and charged spread/fees/slippage. Dynamic capital uses a conservative
high-water proxy. Per-underlying metadata requires kind, point/tick values, exchange
and broker fees, capital per unit, availability timestamp and provenance. Missing
metadata blocks hedging. Stock shorts are blocked pending borrow availability and
costs. The underlying continuation benchmark is one unit, explicitly not risk-matched.

## Interface and operation

`#optionlab`: choose family, news gating, latency, horizon, fees, valuation
assumptions and hedge mode; submit and inspect/download immutable run reports.
`GET/POST /v1/options/research`, `GET /v1/options/research/{id}` require the existing
authentication. Jobs run in one bounded background worker, survive page navigation,
and are marked interrupted after a process restart. Up to 500 cycles, 128 MiB of
decoded evidence, 100 instruments per frame, 90 seconds of compute and 100 signals.
One-GiB disk reserve. No evidence is silently evicted. Reports live in
`options/backtests/{id}.json`. A failure yields no partial profitable report.

## Data required before a result is usable

* Synchronized L2 history for every option leg AND actual underlying/hedge.
* Reference metadata as of each date: multiplier, tick values, fees, expiry,
  exercise/margin style, capital and corporate-action adjustments.
* Dividend/carry assumptions with provenance, actual broker tariff; stock borrow
  terms before permitting stock shorts.
* Chains spanning C/P, adjacent strikes, and two expiries. The existing bounded
  collector does not guarantee complete coverage of every structure or asset.
* Enough prospective news-linked observations for independent validation. Initial
  archive and synthetic QA fixtures are not a historical alpha track record.

Collection scheduling groups each underlying request near the middle of its option
chain. This adds no requests and does not loosen skew tolerances. Old asynchronous
snapshots remain unchanged and are excluded by the same quality gates.

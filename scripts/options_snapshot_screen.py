"""Bounded option quote diagnostics. Candidates are not certified executable arbitrage."""

import itertools
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

MSK = timezone(timedelta(hours=3))


def rows(table):
    return [dict(zip(table["columns"], row, strict=True)) for row in table["data"]]


def positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def instruments(payload):
    market = {r["SECID"]: r for r in rows(payload["marketdata"])}
    result = []
    for ref in rows(payload["securities"]):
        found = re.search(r"([MP])(\d{6})([CP])([AE])(-?\d+(?:\.\d+)?)$", ref["LATNAME"])
        if found is None:
            continue
        result.append(
            {**ref, **market.get(ref["SECID"], {}), "margin_style": found[1], "exercise_style": found[4]}
        )
    return result


def valid_quote(r):
    return (
        positive(r.get("BID"))
        and positive(r.get("OFFER"))
        and r["OFFER"] >= r["BID"]
        and positive(r.get("BIDDEPTH"))
        and positive(r.get("OFFERDEPTH"))
    )


def screen(payload, received_at):
    now = datetime.fromisoformat(received_at).astimezone(MSK)
    all_rows = instruments(payload)
    groups = defaultdict(list)
    candidates = []
    tests = Counter()
    assets = defaultdict(lambda: Counter())
    spreads = defaultdict(list)
    ages = []
    expired_today = 0
    for r in all_rows:
        asset = r["ASSETCODE"] + "/" + r["margin_style"] + r["exercise_style"]
        assets[asset]["listed"] += 1
        assets[asset]["traded"] += int(positive(r.get("NUMTRADES")))
        assets[asset]["volume"] += r.get("VOLTODAY") or 0
        if not valid_quote(r):
            continue
        assets[asset]["two_sided_with_depth"] += 1
        spread_pct = 200 * (r["OFFER"] - r["BID"]) / (r["OFFER"] + r["BID"])
        spreads[asset].append(spread_pct)
        stamp = datetime.fromisoformat(r["SYSTIME"]).replace(tzinfo=MSK)
        age = (now - stamp).total_seconds()
        ages.append(age)
        assets[asset]["updated_within_30s"] += int(0 <= age <= 30)
        assets[asset]["last_outside_current_quotes"] += int(
            positive(r.get("LAST")) and not r["BID"] <= r["LAST"] <= r["OFFER"]
        )
        if r["LASTTRADEDATE"] <= now.date().isoformat():
            expired_today += 1
            continue
        if not positive(r.get("MINSTEP")) or not positive(r.get("STEPPRICE")):
            continue
        key = (
            r["UNDERLYINGASSET"],
            r["LASTDELDATE"],
            r["margin_style"],
            r["exercise_style"],
            r["OPTIONTYPE"],
        )
        groups[key].append(r)

    def candidate(kind, legs, guaranteed_payoff=0.0, financing_rate=None):
        tests[kind] += 1
        points = [r["STEPPRICE"] / r["MINSTEP"] for r, q in legs]
        if any(not math.isclose(p, points[0], rel_tol=1e-6) for p in points):
            tests["conversion_mismatch"] += 1
            return
        pv = points[0]
        if any(r.get("BUYSELLFEE") is None or r.get("EXERCISEFEE") is None for r, q in legs):
            return
        cost = sum(q * (r["OFFER"] if q > 0 else r["BID"]) * pv for r, q in legs)
        # Exchange fee fields + exercise allowance + one adverse tick for each contract.
        buffers = sum(abs(q) * (r["BUYSELLFEE"] + r["EXERCISEFEE"] + r["STEPPRICE"]) for r, q in legs)
        edge = guaranteed_payoff - cost - buffers
        if edge <= 1e-8:
            return
        stamps = [datetime.fromisoformat(r["SYSTIME"]).replace(tzinfo=MSK) for r, q in legs]
        capacity = min(int((r["OFFERDEPTH"] if q > 0 else r["BIDDEPTH"]) // abs(q)) for r, q in legs)
        if capacity < 1:
            return
        candidates.append(
            {
                "family": kind,
                "underlying": legs[0][0]["UNDERLYINGASSET"],
                "expiry": legs[0][0]["LASTDELDATE"],
                "style": legs[0][0]["margin_style"] + legs[0][0]["exercise_style"],
                "quote_cost_rub": round(cost, 6),
                "buffer_rub": round(buffers, 6),
                "scenario_edge_rub": round(edge, 6),
                "guaranteed_payoff_pv_scenario_rub": round(guaranteed_payoff, 6),
                "financing_rate_scenario": financing_rate,
                "snapshot_capacity_packages": capacity,
                "oldest_update_age_s": round((now - min(stamps)).total_seconds(), 1),
                "timestamp_skew_s": round((max(stamps) - min(stamps)).total_seconds(), 1),
                "legs": [
                    {
                        "secid": r["SECID"],
                        "name": r["LATNAME"],
                        "quantity": q,
                        "strike": r["STRIKE"],
                        "side": "buy" if q > 0 else "sell",
                        "price": r["OFFER"] if q > 0 else r["BID"],
                        "price_point_rub": pv,
                        "minstep": r["MINSTEP"],
                        "stepprice": r["STEPPRICE"],
                        "fee": r["BUYSELLFEE"],
                        "exercise_fee": r["EXERCISEFEE"],
                        "timestamp": r["SYSTIME"],
                    }
                    for r, q in legs
                ],
                "status": "snapshot_candidate_unverified",
                "limitations": [
                    "Broker fee, assignment, funding/margin liquidity and simultaneous fills not certified.",
                    "SYSTIME is the last row update, not proof that a standing quote has expired.",
                ],
            }
        )

    for key, chain in groups.items():
        chain.sort(key=lambda r: r["STRIKE"])
        for low, high in itertools.combinations(chain, 2):
            if low["STRIKE"] >= high["STRIKE"]:
                continue
            if key[-1] == "C":
                candidate("strike_monotonicity", [(low, 1), (high, -1)])
            else:
                candidate("strike_monotonicity", [(high, 1), (low, -1)])
        if key[3] == "E":
            by_strike = {r["STRIKE"]: r for r in chain}
            for low, middle in itertools.combinations(chain, 2):
                high = by_strike.get(round(2 * middle["STRIKE"] - low["STRIKE"], 8))
                if high and high["STRIKE"] > middle["STRIKE"]:
                    candidate("european_butterfly", [(low, 1), (middle, -2), (high, 1)])
    # European fixed-payoff boxes only; American exercise makes this classification unsafe.
    pairs = defaultdict(dict)
    for chain in groups.values():
        for r in chain:
            if r["exercise_style"] == "E":
                pairs[(r["UNDERLYINGASSET"], r["LASTDELDATE"])].setdefault(r["STRIKE"], {})[
                    r["OPTIONTYPE"]
                ] = r
    for (_underlying, expiry), strikes in pairs.items():
        complete = sorted(k for k, cp in strikes.items() if set(cp) == {"C", "P"})
        for k1, k2 in itertools.combinations(complete, 2):
            c1, p1 = strikes[k1]["C"], strikes[k1]["P"]
            c2, p2 = strikes[k2]["C"], strikes[k2]["P"]
            pv = c1["STEPPRICE"] / c1["MINSTEP"]
            # Use end-of-day as a conservative long-box funding horizon, not an asserted settlement time.
            years = max(
                0, (datetime.fromisoformat(expiry + "T23:59:59").replace(tzinfo=MSK) - now).total_seconds()
            ) / (365 * 86400)
            candidate(
                "european_long_box_40pct_funding",
                [(c1, 1), (c2, -1), (p2, 1), (p1, -1)],
                (k2 - k1) * pv * math.exp(-0.4 * years),
                0.4,
            )
            candidate(
                "european_reverse_box_zero_interest",
                [(c1, -1), (c2, 1), (p2, -1), (p1, 1)],
                -(k2 - k1) * pv,
                0.0,
            )
    candidates.sort(key=lambda c: c["scenario_edge_rub"], reverse=True)
    summary = []
    for asset, stats in assets.items():
        summary.append(
            {
                "asset_style": asset,
                **stats,
                "median_relative_spread_pct": round(median(spreads[asset]), 2) if spreads[asset] else None,
            }
        )
    return {
        "received_at": received_at,
        "listed": len(all_rows),
        "two_sided_positive_depth": len(ages),
        "updated_within_30s": sum(0 <= v <= 30 for v in ages),
        "median_row_update_age_s": median(ages) if ages else None,
        "same_day_expiry_quoted_excluded": expired_today,
        "tests": dict(tests),
        "candidate_counts": dict(Counter(c["family"] for c in candidates)),
        "candidates": candidates,
        "assets": sorted(summary, key=lambda r: r.get("volume", 0), reverse=True),
        "certified_arbitrages": 0,
        "status": "diagnostic_not_execution_certificate",
    }


if __name__ == "__main__":
    import sys

    folder = Path(sys.argv[1])
    result = screen(
        json.loads((folder / "options-snapshot.json").read_text(encoding="utf-8")),
        json.loads((folder / "options-receipt.json").read_text())["received_at"],
    )
    (folder / "screen.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps({k: v for k, v in result.items() if k not in {"candidates", "assets"}}, ensure_ascii=False)
    )
    print(json.dumps(result["candidates"][:8], ensure_ascii=False))

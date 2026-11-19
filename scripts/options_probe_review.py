"""Reprice screened packages against observed L2; does not assert fillability."""

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.options_snapshot_screen import rows


def levels(record):
    if record.get("status") != 200:
        raise ValueError("missing book")
    payload = record["payload"]
    if rows(payload["dataversion"])[0]["trade_date"] != "2026-11-19":
        raise ValueError("wrong trading date")
    data = rows(payload["orderbook"])
    bids = sorted(
        [
            (r["PRICE"], r["QUANTITY"])
            for r in data
            if r["BUYSELL"] == "B" and r["PRICE"] > 0 and r["QUANTITY"] > 0
        ],
        reverse=True,
    )
    asks = sorted(
        [
            (r["PRICE"], r["QUANTITY"])
            for r in data
            if r["BUYSELL"] == "S" and r["PRICE"] > 0 and r["QUANTITY"] > 0
        ]
    )
    if not bids or not asks or bids[0][0] > asks[0][0]:
        raise ValueError("empty or crossed book")
    return bids, asks


def fill_cost(book_levels, quantity):
    remaining = quantity
    cost = 0
    for price, depth in book_levels:
        take = min(remaining, depth)
        cost += take * price
        remaining -= take
        if remaining == 0:
            return cost
    raise ValueError("insufficient visible depth")


def main():
    folder = Path("output/options-research-20261119")
    screen = json.loads((folder / "screen.json").read_text(encoding="utf-8"))
    plan = json.loads((folder / "probe-plan.json").read_text(encoding="utf-8"))
    rounds = []
    pair_series = {p["underlying"]: [] for p in plan["pairs"]}
    for file in sorted(folder.glob("book-probe-*.json")):
        books = json.loads(file.read_text(encoding="utf-8"))
        observations = []
        for index, candidate in enumerate(screen["candidates"]):
            result = {
                "candidate_index": index,
                "family": candidate["family"],
                "underlying": candidate["underlying"],
                "expiry": candidate["expiry"],
                "sizes": {},
            }
            for size in (1, 5, 10, 25):
                try:
                    cost = 0
                    timestamps = []
                    for leg in candidate["legs"]:
                        record = books[leg["secid"]]
                        bid, ask = levels(record)
                        q = leg["quantity"]
                        cost += (
                            math.copysign(1, q)
                            * fill_cost(ask if q > 0 else bid, abs(q) * size)
                            * leg["price_point_rub"]
                        )
                        timestamps.append(datetime.fromisoformat(record["received_at"]))
                    payoff = candidate["guaranteed_payoff_pv_scenario_rub"]
                    if candidate["financing_rate_scenario"] == 0.4:
                        expiry = datetime.fromisoformat(candidate["expiry"] + "T23:59:59").replace(
                            tzinfo=timezone(timedelta(hours=3))
                        )
                        years = max(0, (expiry - max(timestamps)).total_seconds()) / (365 * 86400)
                        strikes = [leg["strike"] for leg in candidate["legs"]]
                        payoff = (
                            (max(strikes) - min(strikes))
                            * candidate["legs"][0]["price_point_rub"]
                            * math.exp(-0.4 * years)
                        )
                    result["sizes"][str(size)] = {
                        "quote_cost_per_package_rub": round(cost / size, 6),
                        "scenario_residual_per_package_rub": round(
                            payoff - cost / size - candidate["buffer_rub"], 6
                        ),
                        "receipt_span_seconds": round((max(timestamps) - min(timestamps)).total_seconds(), 3),
                    }
                except (KeyError, ValueError) as exc:
                    result["sizes"][str(size)] = {"unavailable": str(exc)}
            observations.append(result)
        rounds.append(
            {
                "file": file.name,
                "received_at": max(r.get("received_at", "") for r in books.values()),
                "positive_candidates_size_1": sum(
                    r["sizes"]["1"].get("scenario_residual_per_package_rub", -1) > 0 for r in observations
                ),
                "observations": observations,
            }
        )
        for pair in plan["pairs"]:
            point = {}
            try:
                for label, secid in (
                    ("underlying", pair["underlying"]),
                    ("call", pair["call"]),
                    ("put", pair["put"]),
                ):
                    bid, ask = levels(books[secid])
                    point[label] = {
                        "bid": bid[0][0],
                        "ask": ask[0][0],
                        "mid": (bid[0][0] + ask[0][0]) / 2,
                        "received_at": books[secid]["received_at"],
                    }
                pair_series[pair["underlying"]].append(point)
            except (KeyError, ValueError):
                pass
    result = {
        "status": "observed_quotes_not_execution_or_alpha_proof",
        "rounds": rounds,
        "pair_series": pair_series,
        "limitations": [
            "Non-atomic HTTP snapshots; request times and receipt times recorded, no order submission.",
            "Broker fees, incremental margin funding and leg execution risk are not included.",
            "A short sampling window cannot establish reaction to news, shock causality or statistical profitability.",
        ],
    }
    (folder / "probe-review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "rounds": [(r["received_at"], r["positive_candidates_size_1"]) for r in rounds],
                "first_candidate_by_round": [r["observations"][0] for r in rounds],
                "pairs": pair_series,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

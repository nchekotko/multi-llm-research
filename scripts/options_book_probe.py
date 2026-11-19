"""Read-only bounded multi-leg book probe. No orders, no LLM calls."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from application.algopack import AlgoPack
from scripts.options_snapshot_screen import instruments, rows, valid_quote


def main():
    out = Path("output/options-research-20261119")
    screen = json.loads((out / "screen.json").read_text(encoding="utf-8"))
    options = instruments(json.loads((out / "options-snapshot.json").read_text(encoding="utf-8")))
    byid = {r["SECID"]: r for r in options}
    futures = {
        r["SECID"]: r for r in rows(json.loads((out / "forts-snapshot.json").read_text(encoding="utf-8"))["marketdata"])
    }
    spot = {r["SECID"]: r for r in rows(json.loads((out / "shares.json").read_text(encoding="utf-8"))["marketdata"])}
    selected = {leg["secid"] for c in screen["candidates"] for leg in c["legs"]}
    pairs = []
    for underlying in ["SiZ6", "RIZ6", "GAZP", "SBER", "CHMF"]:
        base = (futures if underlying in futures else spot).get(underlying)
        if not base or not base.get("BID") or not base.get("OFFER"):
            continue
        price = (base["BID"] + base["OFFER"]) / 2
        lookup = {}
        for r in options:
            if (
                r["UNDERLYINGASSET"] == underlying
                and valid_quote(r)
                and "2026-11-19" < r["LASTDELDATE"] <= "2026-12-31"
            ):
                lookup.setdefault((r["LASTDELDATE"], r["STRIKE"]), {})[r["OPTIONTYPE"]] = r
        eligible = [(k, v) for k, v in lookup.items() if set(v) == {"C", "P"}]
        if not eligible:
            continue
        # Fixed selection on the initial snapshot; no later outcome-dependent reselection.
        key, cp = min(eligible, key=lambda kv: (abs(kv[0][1] / price - 1), kv[0][0]))
        pairs.append(
            {
                "underlying": underlying,
                "expiry": key[0],
                "strike": key[1],
                "call": cp["C"]["SECID"],
                "put": cp["P"]["SECID"],
            }
        )
        selected.update([cp["C"]["SECID"], cp["P"]["SECID"]])
    (out / "probe-plan.json").write_text(
        json.dumps(
            {
                "pairs": pairs,
                "candidate_count": len(screen["candidates"]),
                "rounds": 6,
                "spacing_seconds": 15,
                "option_ids": sorted(selected),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    requests = {
        secid: f"engines/futures/markets/options/boards/ROPD/securities/{secid}/orderbook.json"
        for secid in selected
    }
    underlyings = {byid[x]["UNDERLYINGASSET"] for x in selected}
    for underlying in underlyings:
        if underlying in futures:
            requests[underlying] = (
                f"engines/futures/markets/forts/boards/RFUD/securities/{underlying}/orderbook.json"
            )
        elif underlying in spot:
            requests[underlying] = (
                f"engines/stock/markets/shares/boards/TQBR/securities/{underlying}/orderbook.json"
            )
    client = AlgoPack()

    def fetch(item):
        secid, path = item
        started = datetime.now(timezone.utc).isoformat()
        try:
            response = client.client.get(path, params={"iss.meta": "off"}, timeout=15)
            response.raise_for_status()
            data = response.json()
            return secid, {
                "started_at": started,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "status": response.status_code,
                "payload": data,
            }
        except Exception as exc:
            return secid, {"started_at": started, "error": type(exc).__name__}

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i in range(6):
                begun = time.monotonic()
                result = dict(pool.map(fetch, requests.items()))
                (out / f"book-probe-{i}.json").write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8"
                )
                print(
                    json.dumps(
                        {
                            "round": i,
                            "books": len(result),
                            "errors": sum("error" in x for x in result.values()),
                        }
                    ),
                    flush=True,
                )
                if i < 5:
                    time.sleep(max(0, 15 - (time.monotonic() - begun)))
    finally:
        client.close()


if __name__ == "__main__":
    main()

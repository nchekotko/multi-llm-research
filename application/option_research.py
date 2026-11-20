"""Authenticated bounded replay jobs over immutable option collection evidence."""

import hashlib
import json
import shutil
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, HTTPException

from .option_backtest import FAMILIES, ReplaySpec, replay, timestamp
from .options_math import instruments


def decode(row):
    raw = zlib.decompress(row["raw"])
    if hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise ValueError("snapshot_integrity_failed")
    return json.loads(raw), len(raw)


def load_frames(scanner, spec, cutoff):
    with scanner.connect() as db:
        selected = list(
            db.execute(
                "SELECT id,at,sha256 FROM cycles WHERE kind='books' AND at<=? ORDER BY at DESC LIMIT ?",
                (cutoff, spec.max_cycles),
            )
        )
    cache = {}
    size = 0
    for selected_row in reversed(selected):
        with scanner.connect() as db:
            row = db.execute("SELECT * FROM cycles WHERE id=?", (selected_row["id"],)).fetchone()
            data, amount = decode(row)
            size += amount
            discovery_id = data["discovery"]
            if discovery_id not in cache:
                discovery = db.execute("SELECT * FROM cycles WHERE id=?", (discovery_id,)).fetchone()
                if discovery is None:
                    raise ValueError("discovery_evidence_missing")
                source, n = decode(discovery)
                size += n
                cache = {
                    discovery_id: (
                        {r["SECID"]: r for r in instruments(source["source"]["payload"])},
                        discovery["sha256"],
                        source["source"]["received_at"],
                    )
                }
        if size > 128 * 1024**2:
            raise ValueError("replay_read_budget_exceeded")
        refs, discovery_hash, known_at = cache[discovery_id]
        if timestamp(known_at) > timestamp(row["at"]):
            raise ValueError("future_reference_metadata")
        yield {
            "id": row["id"],
            "at": row["at"],
            "sha256": row["sha256"],
            "discovery_sha256": discovery_hash,
            "books": data["books"],
            "refs": {s: r for s, r in refs.items() if s in data["books"]},
        }


def load_events(scanner, cutoff):
    with scanner.connect() as db:
        events = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT body FROM news_events WHERE first_seen<=? ORDER BY first_seen", (cutoff,)
            )
        ]
    return [
        {
            "id": e["id"],
            "available_at": e["analysis_completed_at"],
            "assets": sorted({s["asset"] for s in e["selected"]}),
        }
        for e in events
        if e["status"] == "prospective" and not e.get("linkage_correction")
    ]


class OptionResearch:
    def __init__(self, scanner):
        self.scanner = scanner
        self.folder = scanner.folder / "backtests"
        self.folder.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        for file in self.folder.glob("*.json"):
            item = json.loads(file.read_text(encoding="utf-8"))
            if item["status"] == "running":
                item["status"] = "interrupted"
                self.save(item)

    def save(self, item):
        path = self.folder / (item["id"] + ".json")
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(item, ensure_ascii=False, default=lambda x: x.isoformat(), allow_nan=False),
            encoding="utf-8",
        )
        temp.replace(path)

    def status(self):
        files = sorted(self.folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
        runs = []
        for file in files:
            item = json.loads(file.read_text(encoding="utf-8"))
            result = item.get("result", {})
            runs.append(
                {k: v for k, v in item.items() if k != "result"}
                | {
                    "summary": {
                        k: result.get(k) for k in ("completed", "censored", "strategy_pnl_rub", "rejections")
                    }
                }
            )
        with self.scanner.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM cycles WHERE kind='books'").fetchone()[0]
        return {
            "families": FAMILIES,
            "defaults": ReplaySpec().model_dump(mode="json"),
            "runs": runs,
            "book_cycles": count,
            "models": {
                "european": "Generalized Black-Scholes / Black-76",
                "american": "CRR, 32 steps; scenario, no assignment engine",
                "continuation": "Trailing return baseline, not a trained news predictor",
                "volatility": "Trailing realized variance forecast",
                "surface": "Smile curvature and near/far IV gap",
            },
            "certified_edge": False,
            "llm_calls": 0,
        }

    def submit(self, spec):
        if not self.lock.acquire(blocking=False):
            raise ValueError("replay_already_running")
        if shutil.disk_usage(self.folder).free < 1024**3:
            self.lock.release()
            raise ValueError("disk_reserve")
        now = datetime.now(timezone.utc).isoformat()
        item = {"id": uuid4().hex, "status": "running", "at": now, "spec": spec.model_dump(mode="json")}
        try:
            self.save(item)
        except Exception:
            self.lock.release()
            raise

        def work():
            try:
                events = load_events(self.scanner, now)
                result = replay(load_frames(self.scanner, spec, now), spec, events)
                result["news_manifest"] = events
                result["engine_sha256"] = {
                    name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                    for name in ("option_models.py", "option_backtest.py", "option_research.py")
                }
                item.update(status="completed", result=result)
            except Exception as exc:
                item.update(
                    status="failed", error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                )
            finally:
                try:
                    self.save(item)
                finally:
                    self.lock.release()

        threading.Thread(target=work, daemon=True).start()
        return {k: v for k, v in item.items() if k != "result"}


def install_option_research(app, authorize, scanner):
    service = OptionResearch(scanner)
    app.state.option_research = service

    @app.get("/v1/options/research", dependencies=[Depends(authorize)])
    def status():
        return service.status()

    @app.post("/v1/options/research", dependencies=[Depends(authorize)], status_code=202)
    def submit(spec: ReplaySpec):
        try:
            return service.submit(spec)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/options/research/{identity}", dependencies=[Depends(authorize)])
    def report(identity: str):
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise HTTPException(404, "not_found")
        path = service.folder / (identity + ".json")
        if not path.exists():
            raise HTTPException(404, "not_found")
        return json.loads(path.read_text(encoding="utf-8"))

    return service

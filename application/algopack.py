"""Authenticated bounded MOEX downloads; immutable snapshots, resumable page cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import Field, model_validator

from .config import digest
from .contracts import Contract, utcnow

PROJECT = Path(__file__).resolve().parents[1]
MSK = timezone(timedelta(hours=3))
PRODUCTS = ("tradestats", "obstats", "orderstats", "candles_1m")


class DownloadSpec(Contract):
    symbols: list[str] = Field(default_factory=list, max_length=20)
    start: date
    end: date  # exclusive
    products: list[str] = Field(default_factory=lambda: list(PRODUCTS), min_length=1, max_length=4)
    whole_futures_market: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if not self.start < self.end or (self.end - self.start).days > 180:
            raise ValueError("download range must be 1..180 days; end exclusive")
        if self.end > datetime.now(MSK).date():
            raise ValueError("only completed calendar days may be downloaded")
        if len(self.symbols) != len(set(self.symbols)) or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", s) for s in self.symbols
        ):
            raise ValueError("invalid or duplicate symbols")
        if not set(self.products) <= set(PRODUCTS) or len(set(self.products)) != len(self.products):
            raise ValueError("invalid products")
        if self.whole_futures_market:
            if self.symbols or "candles_1m" in self.products or (self.end - self.start).days > 31:
                raise ValueError("whole FO market: no symbols or candles, maximum 31 days")
        elif not self.symbols:
            raise ValueError("specify symbols or whole_futures_market")
        return self


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    reserve = int(os.environ.get("MULTILLM_MIN_FREE_BYTES", "0"))
    if reserve and shutil.disk_usage(path.parent).free < reserve + len(payload.encode("utf-8")):
        raise OSError("insufficient free disk space for an atomic research artifact")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def parse_time(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=MSK).timestamp())


def row_time(row: dict) -> int:
    return parse_time(row["tradedate"] + " " + row["tradetime"])


@contextmanager
def job_lock(folder):
    """OS releases the lock after a crash; persisted pages can then be resumed."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("this immutable job is already running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class AlgoPack:
    def __init__(self, root=None):
        self.root = Path(root) if root else PROJECT / "var/algopack"
        config = json.loads((PROJECT / "config/algopack.json").read_text(encoding="utf-8"))
        key = os.environ.get(config["credential_name"])
        if not key:
            for line in Path(config["credential_file"]).read_text(encoding="utf-8-sig").splitlines():
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == config["credential_name"]:
                    key = value.strip().strip('"').strip("'")
                    break
        if not key:
            raise ValueError("configured AlgoPack credential is missing")
        ctx = ssl.create_default_context()
        if config.get("ca_bundle"):
            ctx.load_verify_locations(cafile=config["ca_bundle"])
        self.client = httpx.Client(
            base_url="https://apim.moex.com/iss/",
            verify=ctx,
            trust_env=False,
            follow_redirects=False,
            timeout=30,
            headers={
                "Authorization": "Bearer " + key,
                "Accept": "application/json",
                "User-Agent": "multi-llm-moex-research/1.0",
            },
        )

    def close(self):
        self.client.close()

    def page(self, path, params):
        query = {"iss.meta": "off", **params}
        key = digest({"path": path, "params": query})
        cached = self.root / "pages" / (key + ".json")
        if cached.exists():
            result = json.loads(cached.read_text(encoding="utf-8"))
            if digest(result["payload"]) != result["payload_sha256"]:
                raise ValueError("cached AlgoPack page hash mismatch")
            return result
        for attempt in range(3):
            response = self.client.get(path + ".json", params=query)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(1 + attempt)
                continue
            if response.status_code != 200:
                raise ValueError(f"AlgoPack HTTP {response.status_code}; download incomplete")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("AlgoPack returned non-JSON; access not confirmed") from exc
            if not isinstance(payload, dict):
                raise ValueError("unexpected AlgoPack response")
            result = {
                "path": path,
                "params": query,
                "received_at": utcnow(),
                "payload": payload,
                "payload_sha256": digest(payload),
                "tls_verified": True,
            }
            save_json(cached, result)
            time.sleep(0.06)
            return result
        raise ValueError("AlgoPack retries exhausted")

    def table(self, path, block, params, max_pages=1000):
        rows, receipts, seen = [], [], set()
        offset = 0
        for _ in range(max_pages):
            page = self.page(path, {**params, "start": offset, "limit": 1000})
            table = page["payload"].get(block)
            if not isinstance(table, dict) or "columns" not in table or "data" not in table:
                raise ValueError("required AlgoPack table missing")
            data = table["data"]
            receipts.append({k: v for k, v in page.items() if k != "payload"})
            if not data:
                return rows, receipts
            fingerprint = digest(data)
            if fingerprint in seen:
                raise ValueError("AlgoPack pagination repeated a page; refusing incomplete download")
            seen.add(fingerprint)
            rows.extend(dict(zip(table["columns"], row, strict=True)) for row in data)
            offset += len(data)  # server may ignore requested limit; always continue to empty page
        raise ValueError("AlgoPack page cap reached; narrow query")

    def download(self, spec: DownloadSpec, progress=lambda x: None):
        folder = self.root / "datasets" / digest(spec.model_dump(mode="json"))
        with job_lock(folder):
            return self._download(spec, progress)

    def _download(self, spec: DownloadSpec, progress=lambda x: None):
        identity = digest(spec.model_dump(mode="json"))
        folder = self.root / "datasets" / identity
        manifest_path = folder / "manifest.json"
        if manifest_path.exists():
            read_dataset(self.root, identity)
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        folder.mkdir(parents=True, exist_ok=True)
        plan_path = folder / "request.json"
        if not plan_path.exists():
            save_json(plan_path, {"created_at": utcnow(), "request": spec.model_dump(mode="json")})
        all_rows = {p: [] for p in spec.products}
        receipts = []
        if spec.whole_futures_market:
            units = [(None, spec.start + timedelta(days=n)) for n in range((spec.end - spec.start).days)]
        else:
            units = [(s, None) for s in spec.symbols]
        for symbol, day in units:
            for product in spec.products:
                if product == "candles_1m":
                    path = f"engines/futures/markets/forts/boards/RFUD/securities/{symbol}/candles"
                    block = "candles"
                    params = {
                        "from": str(spec.start),
                        "till": str(spec.end - timedelta(days=1)),
                        "interval": 1,
                    }
                else:
                    path = "datashop/algopack/fo/" + product + ("/" + symbol if symbol else "")
                    block = "data"
                    params = (
                        {"date": str(day)}
                        if day
                        else {"from": str(spec.start), "till": str(spec.end - timedelta(days=1))}
                    )
                rows, pages = self.table(path, block, params)
                if symbol and product == "candles_1m":
                    rows = [dict(row, secid=symbol) for row in rows]
                for row in rows:
                    observed_date = row["begin"][:10] if product == "candles_1m" else row["tradedate"]
                    if not str(spec.start) <= observed_date < str(spec.end) or (
                        symbol and row["secid"] != symbol
                    ):
                        raise ValueError("AlgoPack returned rows outside requested instrument/date scope")
                all_rows[product].extend(rows)
                receipts.extend(pages)
                progress(
                    {
                        "symbol": symbol or "ALL_FO",
                        "date": str(day) if day else None,
                        "product": product,
                        "rows": len(rows),
                        "pages": len(pages),
                    }
                )
        products = {}
        for product, rows in all_rows.items():
            timestamp = (
                (lambda r: r["begin"])
                if product == "candles_1m"
                else (lambda r: r["tradedate"] + " " + r["tradetime"])
            )
            keys = [(r["secid"], timestamp(r)) for r in rows]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate AlgoPack instrument/time rows")
            rows.sort(key=lambda r: (timestamp(r), r["secid"]))
            path = folder / (product + ".json")
            save_json(path, rows)
            products[product] = {
                "file": path.name,
                "rows": len(rows),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "instruments": sorted({r["secid"] for r in rows}),
                "first": timestamp(rows[0]) if rows else None,
                "last": timestamp(rows[-1]) if rows else None,
            }
        manifest = {
            "dataset_id": identity,
            "request": spec.model_dump(mode="json"),
            "completed_at": utcnow(),
            "products": products,
            "receipts": receipts,
            "coverage_claim": "all returned pages, not proof of complete exchange history",
        }
        save_json(manifest_path, manifest)
        return manifest


def read_dataset(root: Path, dataset_id: str):
    if not re.fullmatch(r"[a-f0-9]{64}", dataset_id):
        raise ValueError("invalid dataset ID")
    folder = Path(root) / "datasets" / dataset_id
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if digest(manifest["request"]) != dataset_id:
        raise ValueError("dataset request digest mismatch")
    result = {}
    for product, meta in manifest["products"].items():
        if product not in PRODUCTS or meta["file"] != product + ".json":
            raise ValueError("invalid dataset product path")
        raw = (folder / meta["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise ValueError("AlgoPack dataset content mismatch")
        result[product] = json.loads(raw)
        if len(result[product]) != meta["rows"]:
            raise ValueError("AlgoPack dataset row count mismatch")
    return manifest, result

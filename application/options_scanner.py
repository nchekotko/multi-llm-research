"""Bounded prospective options research. Read-only market data; never submits orders."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .algopack import AlgoPack, job_lock
from .options_math import instruments, rows, screen, valid_quote

MSK = timezone(timedelta(hours=3))
VERSION = "options-forward-v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def adjacent_request_order(universe, paths):
    """Keep an underlying request near its chain without adding requests or relaxing freshness."""
    groups = {}
    for secid, ref in universe.items():
        groups.setdefault(ref["UNDERLYINGASSET"], []).append(secid)
    ordered = []
    for underlying, chain in groups.items():
        middle = len(chain) // 2
        ordered.extend(chain[:middle])
        if underlying in paths:
            ordered.append(underlying)
        ordered.extend(chain[middle:])
    ordered.extend(paths)
    return [(key, paths[key]) for key in dict.fromkeys(ordered) if key in paths]


class OptionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    interval_seconds: int = Field(30, ge=15, le=600)
    discovery_seconds: int = Field(900, ge=300, le=86400)
    max_options: int = Field(60, ge=12, le=100)
    assets: list[str] = Field(
        default_factory=lambda: ["CHMF", "GAZR", "SBRF", "Si", "RTS", "BR", "NG", "CNY"],
        min_length=1,
        max_length=12,
    )
    broker_rub_per_contract: float | None = Field(None, ge=0, le=10000)
    slippage_ticks: int = Field(1, ge=0, le=20)
    capital_rate_annual: float = Field(0.4, ge=0, le=5)
    latency_seconds: float = Field(1, ge=0.1, le=60)
    horizon_seconds: int = Field(300, ge=30, le=3600)
    max_book_age_seconds: int = Field(30, ge=5, le=120)
    max_receipt_skew_seconds: float = Field(3, ge=0.1, le=20)
    max_gap_seconds: int = Field(90, ge=15, le=600)
    min_signal_rub: float = Field(0.5, ge=0, le=10000)
    max_storage_mb: int = Field(1024, ge=32, le=4096)
    min_free_mb: int = Field(1024, ge=512, le=8192)


class SettingsChange(BaseModel):
    revision: int = Field(ge=0)
    settings: OptionsSettings


def book(record, settings):
    """Dataversion freshness is checked separately from the age of a standing quote."""
    if record.get("error"):
        raise ValueError("source_error")
    received = datetime.fromisoformat(record["received_at"])
    data = record["payload"]
    version = rows(data["dataversion"])[0]
    if version["trade_date"] != received.astimezone(MSK).date().isoformat():
        raise ValueError("wrong_session")
    stamp = datetime.strptime(str(version["seqnum"]), "%Y%m%d%H%M%S").replace(tzinfo=MSK)
    age = (received - stamp).total_seconds()
    if not -2 <= age <= settings.max_book_age_seconds:
        raise ValueError("stale_feed")
    levels = rows(data["orderbook"])
    bids = sorted(
        [
            (r["PRICE"], r["QUANTITY"])
            for r in levels
            if r["BUYSELL"] == "B" and r["PRICE"] > 0 and r["QUANTITY"] > 0
        ],
        reverse=True,
    )
    asks = sorted(
        [
            (r["PRICE"], r["QUANTITY"])
            for r in levels
            if r["BUYSELL"] == "S" and r["PRICE"] > 0 and r["QUANTITY"] > 0
        ]
    )
    if not bids or not asks or bids[0][0] > asks[0][0]:
        raise ValueError("empty_or_crossed")
    return bids, asks


def fill(levels, quantity):
    total = 0.0
    left = quantity
    for price, depth in levels:
        take = min(left, depth)
        total += take * price
        left -= take
        if left <= 0:
            return total
    raise ValueError("insufficient_depth")


def package_cost(legs, books, settings, reverse=False):
    cost = 0.0
    stamps = []
    for leg in legs:
        record = books[leg["secid"]]
        bids, asks = book(record, settings)
        quantity = leg["quantity"] * (-1 if reverse else 1)
        cost += (
            math.copysign(1, quantity)
            * fill(asks if quantity > 0 else bids, abs(quantity))
            * leg["price_point_rub"]
        )
        stamps.append(datetime.fromisoformat(record["received_at"]))
    if (max(stamps) - min(stamps)).total_seconds() > settings.max_receipt_skew_seconds:
        raise ValueError("asynchronous_legs")
    return cost


def trading_cost(legs, settings):
    return sum(
        abs(leg["quantity"])
        * (leg["fee"] + (settings.broker_rub_per_contract or 0) + settings.slippage_ticks * leg["stepprice"])
        for leg in legs
    )


class OptionsScanner:
    def __init__(self, root, source_factory=AlgoPack):
        self.folder = Path(root) / "options"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "research.sqlite3"
        self.source_factory = source_factory
        self.lock = threading.RLock()
        self.universe = {}
        self.discovery_at = 0.0
        self.selection_revision = None
        self.previous = {}
        self.runtime = "starting"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(revision INTEGER PRIMARY KEY, at TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, at TEXT, kind TEXT, sha256 TEXT, raw BLOB);
                CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY, at TEXT, cycle TEXT, family TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY, signal TEXT, state TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS events(at TEXT, kind TEXT);
                CREATE INDEX IF NOT EXISTS eval_state ON evaluations(state);
                CREATE INDEX IF NOT EXISTS signal_time ON signals(at);
            """)
            if not db.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
                db.execute("INSERT INTO settings VALUES(0,?,?)", (now(), OptionsSettings().model_dump_json()))
        from .news_reactions import NewsReactions

        self.news = NewsReactions(self)
        self.base_universe = {}

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM settings ORDER BY revision DESC LIMIT 1").fetchone()
        return row["revision"], OptionsSettings.model_validate_json(row["body"])

    def configure(self, change):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT MAX(revision) FROM settings").fetchone()[0]
            if revision != change.revision:
                raise ValueError("settings_revision_conflict")
            db.execute(
                "INSERT INTO settings VALUES(?,?,?)", (revision + 1, now(), change.settings.model_dump_json())
            )
            self.discovery_at = 0
        return self.status()

    def store_cycle(self, kind, payload, settings):
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        compressed = zlib.compress(raw)
        # Never silently evict evidence: stop collection when capacity is exhausted.
        if self.path.stat().st_size + 2 * len(
            compressed
        ) > settings.max_storage_mb * 1024**2 or shutil.disk_usage(
            self.folder
        ).free < settings.min_free_mb * 1024**2 + 2 * len(compressed):
            raise ValueError("storage_limit_collection_paused")
        identity = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO cycles VALUES(?,?,?,?,?)",
                (identity, now(), kind, hashlib.sha256(raw).hexdigest(), compressed),
            )
        return identity

    @staticmethod
    def fetch(source, path):
        started = now()
        begun = time.monotonic()
        try:
            params = {"iss.meta": "off"}
            if path.endswith("/securities.json"):
                params["iss.only"] = "securities,marketdata,dataversion"
            response = source.client.get(path, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            return {
                "started_at": started,
                "received_at": now(),
                "duration_ms": round((time.monotonic() - begun) * 1000, 2),
                "http_status": response.status_code,
                "http_date": response.headers.get("date"),
                "payload": payload,
            }
        except Exception as exc:
            return {"started_at": started, "received_at": now(), "error": type(exc).__name__}

    def discover(self, source, settings):
        record = self.fetch(source, "engines/futures/markets/options/securities.json")
        if record.get("error"):
            raise ValueError("discovery_failed")
        payload = record["payload"]
        refs = instruments(payload)
        self.all_refs = refs
        eligible = [
            r
            for r in refs
            if r["ASSETCODE"] in settings.assets
            and r["LASTTRADEDATE"] > datetime.now(MSK).date().isoformat()
            and valid_quote(r)
        ]
        eligible_ids = {r["SECID"] for r in eligible}
        diagnostic = screen(payload, record["received_at"])
        chosen = []
        # Seed structural candidates, with a fixed budget left for ordinary chains.
        for candidate in diagnostic["candidates"]:
            ids = [leg["secid"] for leg in candidate["legs"]]
            merged = list(dict.fromkeys(chosen + ids))
            if set(ids) <= eligible_ids and len(merged) <= settings.max_options // 2:
                chosen = merged
        for asset in settings.assets:
            chain = [
                r for r in eligible if r["ASSETCODE"] == asset and (r.get("UNDERLYINGSETTLEPRICE") or 0) > 0
            ]
            chain.sort(key=lambda r: (r["LASTDELDATE"], abs(r["STRIKE"] / r["UNDERLYINGSETTLEPRICE"] - 1)))
            for ref in chain[: max(4, settings.max_options // len(settings.assets))]:
                if ref["SECID"] not in chosen and len(chosen) < settings.max_options:
                    chosen.append(ref["SECID"])
        self.universe = {r["SECID"]: r for r in eligible if r["SECID"] in chosen}
        self.base_universe = dict(self.universe)
        self.discovery_id = self.store_cycle(
            "discovery", {"source": record, "selected": chosen, "version": VERSION}, settings
        )
        self.discovery_at = time.monotonic()

    def collect(self, settings, revision):
        source = self.source_factory()
        try:
            if (
                not self.universe
                or revision != self.selection_revision
                or time.monotonic() - self.discovery_at >= settings.discovery_seconds
            ):
                self.discover(source, settings)
                self.selection_revision = revision
            try:
                self.news.poll(self.all_refs, settings, revision)
            except Exception as exc:
                self.news.health = {"status": "news_ingestion_error_" + type(exc).__name__}
            self.universe = {**self.base_universe, **self.news.required_refs()}
            paths = {
                secid: f"engines/futures/markets/options/boards/ROPD/securities/{secid}/orderbook.json"
                for secid in self.universe
            }
            for ref in self.universe.values():
                underlying = ref["UNDERLYINGASSET"]
                if ref["UNDERLYINGTYPE"] == "F":
                    paths[underlying] = (
                        f"engines/futures/markets/forts/boards/RFUD/securities/{underlying}/orderbook.json"
                    )
                elif ref["UNDERLYINGTYPE"] == "S":
                    paths[underlying] = (
                        f"engines/stock/markets/shares/boards/TQBR/securities/{underlying}/orderbook.json"
                    )

            def get(item):
                secid, path = item
                result = self.fetch(source, path)
                time.sleep(0.35)  # bounded fan-out, no retry storm
                return secid, result

            with ThreadPoolExecutor(max_workers=3) as pool:
                books = dict(pool.map(get, adjacent_request_order(self.universe, paths)))
        finally:
            source.close()
        cycle = self.store_cycle(
            "books",
            {
                "version": VERSION,
                "settings_revision": revision,
                "settings": settings.model_dump(),
                "discovery": self.discovery_id,
                "books": books,
            },
            settings,
        )
        self.evaluate_pending(books, cycle)
        self.news.observe(books, cycle)
        self.detect(books, settings, revision, cycle)
        self.previous = books
        self.runtime = "collecting"

    def detect(self, books, settings, revision, cycle):
        joined = []
        features = []
        for secid, ref in self.universe.items():
            try:
                bids, asks = book(books[secid], settings)
                stamp = (
                    datetime.fromisoformat(books[secid]["received_at"])
                    .astimezone(MSK)
                    .replace(tzinfo=None)
                    .isoformat()
                )
                joined.append(
                    {
                        **ref,
                        "BID": bids[0][0],
                        "OFFER": asks[0][0],
                        "BIDDEPTH": bids[0][1],
                        "OFFERDEPTH": asks[0][1],
                        "SYSTIME": stamp,
                    }
                )
                depth_bid, depth_ask = sum(q for _, q in bids[:3]), sum(q for _, q in asks[:3])
                imbalance = (depth_bid - depth_ask) / (depth_bid + depth_ask)
                if abs(imbalance) >= 0.8:
                    features.append(
                        {
                            "family": "book_imbalance",
                            "secid": secid,
                            "imbalance": imbalance,
                            "status": "observation_no_directional_edge_proven",
                        }
                    )
                if secid in self.previous:
                    previous_time = datetime.fromisoformat(self.previous[secid]["received_at"])
                    if (
                        datetime.fromisoformat(books[secid]["received_at"]) - previous_time
                    ).total_seconds() > settings.max_gap_seconds:
                        continue
                    old_bid, old_ask = book(self.previous[secid], settings)
                    old_depth = sum(q for _, q in old_bid[:3] + old_ask[:3])
                    if depth_bid + depth_ask < 0.3 * old_depth:
                        features.append(
                            {
                                "family": "liquidity_vacuum",
                                "secid": secid,
                                "depth_ratio": (depth_bid + depth_ask) / old_depth,
                                "status": "observation",
                            }
                        )
                    underlying = ref["UNDERLYINGASSET"]
                    ub, ua = book(books[underlying], settings)
                    pb, pa = book(self.previous[underlying], settings)
                    move = (ub[0][0] + ua[0][0] - pb[0][0] - pa[0][0]) / 2
                    if (bids[0][0], asks[0][0]) == (old_bid[0][0], old_ask[0][0]) and abs(move) > (
                        ua[0][0] - ub[0][0]
                    ):
                        features.append(
                            {
                                "family": "quote_lag",
                                "secid": secid,
                                "underlying_move": move,
                                "option_spread": asks[0][0] - bids[0][0],
                                "status": "observation_delta_and_event_not_estimated",
                            }
                        )
            except (KeyError, ValueError, IndexError):
                continue
        candidates = []
        if joined:
            columns = list(joined[0])
            table = {"columns": columns, "data": [[r.get(k) for k in columns] for r in joined]}
            candidates = screen({"securities": table, "marketdata": table}, now())["candidates"]
        with self.connect() as db:
            for candidate in candidates[:30]:
                try:
                    cost = package_cost(candidate["legs"], books, settings)
                except (KeyError, ValueError, IndexError):
                    continue
                if candidate["scenario_edge_rub"] < settings.min_signal_rub:
                    continue
                identity = uuid4().hex
                at = now()
                capital = sum(
                    abs(leg["quantity"])
                    * max(self.universe[leg["secid"]].get(k) or 0 for k in ("IMBUY", "IMP", "IMNP"))
                    for leg in candidate["legs"]
                )
                data = {
                    **candidate,
                    "id": identity,
                    "at": at,
                    "cycle": cycle,
                    "settings_revision": revision,
                    "settings": settings.model_dump(),
                    "entry_quote_cost": cost,
                    "capital_proxy_rub": capital,
                    "status": "candidate_not_executed",
                    "all_costs_known": False,
                    "broker_cost_known": settings.broker_rub_per_contract is not None,
                    "version": VERSION,
                }
                db.execute(
                    "INSERT INTO signals VALUES(?,?,?,?,?)",
                    (identity, at, cycle, candidate["family"], json.dumps(data)),
                )
                db.execute(
                    "INSERT INTO evaluations VALUES(?,?,?,?)",
                    (identity, identity, "await_entry", json.dumps(data)),
                )
            for feature in features[:100]:
                identity = uuid4().hex
                db.execute(
                    "INSERT INTO signals VALUES(?,?,?,?,?)",
                    (
                        identity,
                        now(),
                        cycle,
                        feature["family"],
                        json.dumps({**feature, "cycle": cycle, "settings_revision": revision}),
                    ),
                )

    def evaluate_pending(self, books, cycle):
        current = datetime.now(timezone.utc)
        with self.connect() as db:
            for row in db.execute(
                "SELECT * FROM evaluations WHERE state IN ('await_entry','await_exit')"
            ).fetchall():
                data = json.loads(row["body"])
                settings = OptionsSettings.model_validate(data["settings"])
                elapsed = (current - datetime.fromisoformat(data.get("entry_at", data["at"]))).total_seconds()
                target = (
                    settings.latency_seconds if row["state"] == "await_entry" else settings.horizon_seconds
                )
                if elapsed < target:
                    continue
                state = row["state"]
                try:
                    if elapsed > target + settings.max_gap_seconds:
                        raise ValueError("sampling_gap")
                    # Enter/exit only using later observations, never the signal's own snapshot.
                    if cycle == data["cycle"]:
                        raise ValueError("same_snapshot")
                    origin = datetime.fromisoformat(data.get("entry_at", data["at"]))
                    if any(
                        (datetime.fromisoformat(books[leg["secid"]]["started_at"]) - origin).total_seconds()
                        < target
                        for leg in data["legs"]
                    ):
                        continue
                    cost = package_cost(data["legs"], books, settings, reverse=state == "await_exit")
                    if state == "await_entry":
                        if "guaranteed_payoff_pv_scenario_rub" in data:
                            remaining = (
                                data["guaranteed_payoff_pv_scenario_rub"]
                                - cost
                                - trading_cost(data["legs"], settings)
                            )
                            if remaining < settings.min_signal_rub:
                                data["entry_residual_rub"] = remaining
                                db.execute(
                                    "UPDATE evaluations SET state='signal_expired',body=? WHERE id=?",
                                    (json.dumps(data), row["id"]),
                                )
                                continue
                        data.update(
                            entry_at=current.isoformat(),
                            entry_cycle=cycle,
                            simulated_entry_cost=cost,
                            observed_latency_seconds=elapsed,
                        )
                        state = "await_exit"
                    else:
                        funding = (
                            data["capital_proxy_rub"] * settings.capital_rate_annual * elapsed / (365 * 86400)
                        )
                        expenses = 2 * trading_cost(data["legs"], settings) + funding
                        data.update(
                            exit_at=current.isoformat(),
                            exit_cycle=cycle,
                            simulated_exit_cost=cost,
                            gross_pnl=-data["simulated_entry_cost"] - cost,
                            scenario_cost_rub=expenses,
                            scenario_pnl_rub=-data["simulated_entry_cost"] - cost - expenses,
                            holding_seconds=elapsed,
                            funding_proxy_rub=funding,
                            execution_assumption="all_legs_cross_visible_depth_non_atomic_not_fill_proof",
                        )
                        state = (
                            "scenario_complete"
                            if settings.broker_rub_per_contract is not None
                            else "cost_incomplete"
                        )
                except (KeyError, ValueError, IndexError) as exc:
                    data["failure"] = str(exc) if isinstance(exc, ValueError) else "missing_book"
                    state = "entry_unobservable" if state == "await_entry" else "exit_unobservable"
                db.execute(
                    "UPDATE evaluations SET state=?,body=? WHERE id=?", (state, json.dumps(data), row["id"])
                )

    def run(self, stop):
        try:
            with job_lock(self.folder):
                while not stop.is_set():
                    revision, settings = self.settings()
                    local = datetime.now(MSK)
                    if not settings.enabled:
                        self.runtime = "disabled"
                    elif not 9 <= local.hour < 24:
                        self.runtime = "outside_collection_window"
                    else:
                        try:
                            self.collect(settings, revision)
                        except Exception as exc:
                            self.runtime = (
                                str(exc)
                                if isinstance(exc, ValueError)
                                and str(exc) in {"storage_limit_collection_paused", "discovery_failed"}
                                else "collection_error_" + type(exc).__name__
                            )
                            with self.connect() as db:
                                db.execute("INSERT INTO events VALUES(?,?)", (now(), self.runtime))
                    stop.wait(settings.interval_seconds if settings.enabled else 2)
        except ValueError:
            self.runtime = "collector_already_running"

    def status(self):
        revision, settings = self.settings()
        with self.connect() as db:
            counts = {
                r["kind"]: r["n"] for r in db.execute("SELECT kind,COUNT(*) n FROM cycles GROUP BY kind")
            }
            families = {
                r["family"]: r["n"]
                for r in db.execute("SELECT family,COUNT(*) n FROM signals GROUP BY family")
            }
            evaluations = [
                dict(r) | {"body": json.loads(r["body"])}
                for r in db.execute("SELECT * FROM evaluations ORDER BY rowid DESC LIMIT 30")
            ]
            latest = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM signals WHERE family NOT IN ('quote_lag','book_imbalance','liquidity_vacuum') ORDER BY rowid DESC LIMIT 30"
                )
            ]
            states = {
                r["state"]: r["n"]
                for r in db.execute("SELECT state,COUNT(*) n FROM evaluations GROUP BY state")
            }
            last = db.execute(
                "SELECT at FROM cycles WHERE kind='books' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT 5")]
        return {
            "version": VERSION,
            "runtime": self.runtime,
            "revision": revision,
            "settings": settings.model_dump(),
            "cycles": counts,
            "families": families,
            "evaluation_states": states,
            "latest": latest,
            "evaluations": evaluations,
            "last_collection": last[0] if last else None,
            "storage_mb": round(self.path.stat().st_size / 1024**2, 2),
            "selected_options": len(self.universe),
            "events": events,
            "model": None,
            "llm_calls": 0,
            "certified_edge": False,
            "limitations": [
                "Снимки HTTP, не поток заявок; субсекундное исполнение не доказано.",
                "Комбинации проверяются по доступной глубине, одновременное исполнение не гарантировано.",
                "Пассивные fills, новостная причинность и прогноз волатильности пока не оцениваются.",
                "Стоимость ГО — сценарная верхняя оценка по ногам, не портфельный расчёт брокера.",
                "Нет торговых заявок. Результаты сценарные; независимая OOS-проверка ещё не накоплена.",
            ],
        }


def install_options(app, authorize, root):
    scanner = OptionsScanner(root)
    app.state.options_scanner = scanner

    @app.get("/v1/options", dependencies=[Depends(authorize)])
    def status():
        return scanner.status()

    @app.get("/v1/options/news", dependencies=[Depends(authorize)])
    def news():
        return scanner.news.status()

    @app.put("/v1/options/settings", dependencies=[Depends(authorize)])
    def settings(change: SettingsChange):
        try:
            return scanner.configure(change)
        except ValueError as exc:
            raise HTTPException(409, "Настройки изменены; обновите страницу.") from exc

    @app.get("/v1/options/cycles/{identity}", dependencies=[Depends(authorize)])
    def evidence(identity: str):
        with scanner.connect() as db:
            row = db.execute("SELECT * FROM cycles WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise HTTPException(404, "snapshot not found")
        raw = zlib.decompress(row["raw"])
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise HTTPException(409, "snapshot integrity failed")
        return {"id": identity, "sha256": row["sha256"], "payload": json.loads(raw)}

    return scanner

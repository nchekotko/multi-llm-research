"""Bounded read-only access. Never import or execute code from the source project."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import digest
from .contracts import DatasetSnapshot, HourBar, ResearchSpec, utcnow
from .security import PolicyDenied
from .validation import GateFailed

TABLES = {
    "quant_lab": {"instruments", "bars", "bar_coverage"},
    "tradfi_1h": {"bars", "chunks"},
    "crypto_lab": {"bars", "funding_events", "perp_metrics", "depth_snapshots"},
    "spread_lab": {"daily_bars", "funding_events", "snapshots"},
    "earnings_lab": {"events", "features", "outcomes"},
    "bonds_lab": {"bond_snapshots"},
    "attention": {"series", "coverage"},
}


class EconomicCalendar:
    def __init__(self, config_path: str | Path | None = None):
        # This path belongs to the operator, never to a submitted TaskSpec.
        path = (
            Path(config_path)
            if config_path
            else Path(__file__).resolve().parents[1] / "config/economic-calendar.json"
        )
        config = json.loads(path.read_text(encoding="utf-8"))
        self.root = Path(config["root"]).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("connector root must be a directory")

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise PolicyDenied("connector path escapes configured root")
        return path

    @contextmanager
    def connection(self, database: str, check=None):
        if database not in TABLES:
            raise PolicyDenied("database is not on the research allowlist")
        path = self._path(f"data/{database}.db")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 12

        def progress():
            if check:
                check()
            return int(time.monotonic() > deadline)

        def authorize(action, arg1, arg2, db, trigger):
            if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and (arg1 in TABLES[database] or arg1 == "sqlite_master"):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_progress_handler(progress, 10000)
        conn.set_authorizer(authorize)
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    def catalog(self) -> dict:
        databases = []
        for name, allowed in TABLES.items():
            try:
                path = self._path(f"data/{name}.db")
                with self.connection(name) as conn:
                    schema = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
                        )
                        if row["name"] in allowed
                    ]
                databases.append(
                    {"database": name, "bytes": path.stat().st_size, "read_only": True, "tables": schema}
                )
            except FileNotFoundError:
                databases.append({"database": name, "missing": True})
        return {
            "connector": "economic-calendar/1.0",
            "databases": databases,
            "research_profile": "moex_futures_1h_reference",
            "execution_certified": False,
            "limitations": [
                "Source project and its .env, user/trading databases and Python modules are not loaded.",
                "Daily continuous FUT:* series are features only, never execution prices.",
                "Features/outcomes and date-only attention data have no verified intraday publication timestamps.",
            ],
        }

    def preview(self, database: str, table: str, limit: int = 20) -> dict:
        if database not in TABLES or table not in TABLES[database]:
            raise PolicyDenied("table is not on the research allowlist")
        if not 1 <= limit <= 100:
            raise ValueError("preview limit must be 1..100")
        # Both identifiers are constants selected from the allowlist, not user SQL.
        with self.connection(database) as conn:
            rows = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (limit,))]
        return {
            "database": database,
            "table": table,
            "rows": rows,
            "truncated": len(rows) == limit,
            "point_in_time_verified": False,
            "content_is_untrusted_evidence": True,
        }

    def futures(self) -> list[dict]:
        with self.connection("tradfi_1h") as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT symbol,COUNT(*) rows,MIN(ts) first_ts,MAX(ts) last_ts FROM bars "
                    "WHERE source=? GROUP BY symbol ORDER BY symbol",
                    ("moex",),
                )
            ]

    def hypotheses(self, include_text=False) -> list[dict]:
        text = self._path("docs/strategies-register.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        starts = [i for i, line in enumerate(lines) if line.startswith("### ")]
        result = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(lines)
            # A following level-one/two heading also terminates the section.
            end = next((i for i in range(start + 1, end) if re.match(r"^#{1,2} ", lines[i])), end)
            body = "\n".join(lines[start:end])
            title = lines[start][4:].strip()
            stable_title = re.split(r"\s[—–]\s|`", title)[0].strip()
            upper = title.upper()
            status = "unclassified"
            for token, mapped in [
                ("ОТКЛОН", "rejected"),
                ("REJECT", "rejected"),
                ("НА ГЕЙТ", "at_gate"),
                ("КАНДИДАТ", "candidate"),
                ("В РАБОТ", "in_progress"),
            ]:
                if token in upper:
                    status = mapped
                    break
            item = {
                "hypothesis_id": "hyp_" + hashlib.sha256(stable_title.encode()).hexdigest()[:16],
                "title": title,
                "status": status,
                "revision": hashlib.sha256(body.encode()).hexdigest(),
                "source": "docs/strategies-register.md",
                "line_start": start + 1,
                "line_end": end,
                "content_is_untrusted_evidence": True,
            }
            if include_text:
                item["text"] = body
            result.append(item)
        ids = [x["hypothesis_id"] for x in result]
        if len(ids) != len(set(ids)):
            raise GateFailed("registry has ambiguous duplicate titles")
        return result

    def hypothesis(self, spec: ResearchSpec) -> dict | None:
        if spec.hypothesis_id is None:
            return None
        item = next((x for x in self.hypotheses(True) if x["hypothesis_id"] == spec.hypothesis_id), None)
        if item is None or item["revision"] != spec.hypothesis_revision:
            raise GateFailed("registry entry missing or revision changed; pin the current revision")
        if item["status"] == "rejected" and not spec.revisit_reason:
            raise GateFailed("rejected hypothesis requires an explicit reason for a new experiment")
        return item

    def snapshot(self, spec: ResearchSpec, check=lambda: None) -> DatasetSnapshot:
        start = int(datetime.combine(spec.start, datetime.min.time(), timezone.utc).timestamp()) - 3 * 3600
        end = int(datetime.combine(spec.end, datetime.min.time(), timezone.utc).timestamp()) - 3 * 3600
        query = {
            "database": "tradfi_1h",
            "table": "bars",
            "source": "moex",
            "tf": "1h",
            "start_ts_inclusive": start,
            "end_ts_exclusive": end,
            "symbols": [x.symbol for x in spec.legs],
            "max_rows_per_leg": 40000,
        }
        series = {}
        with self.connection("tradfi_1h", check) as conn:
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='bars'"
            ).fetchone()[0]
            for leg in spec.legs:
                check()
                rows = conn.execute(
                    "SELECT ts,open,high,low,close,volume FROM bars WHERE source=? AND symbol=? "
                    "AND ts>=? AND ts<? ORDER BY ts LIMIT 40001",
                    ("moex", leg.symbol, start, end),
                ).fetchall()
                if len(rows) > 40000:
                    raise GateFailed("dataset row cap exceeded; narrow the period")
                series[leg.symbol] = [
                    HourBar(
                        ts=row["ts"],
                        available_at=row["ts"] + 3600,
                        **{k: row[k] for k in ("open", "high", "low", "close", "volume")},
                    )
                    for row in rows
                ]
        payload = {key: [bar.model_dump(mode="json") for bar in bars] for key, bars in series.items()}
        return DatasetSnapshot(
            captured_at=utcnow(),
            query=query,
            series=series,
            content_sha256=digest(payload),
            source_metadata={
                "schema_sha256": digest(schema),
                "bar_timing": "assumed_start_plus_3600",
                "historical_publication_time_verified": False,
                "file_bytes_at_capture": self._path("data/tradfi_1h.db").stat().st_size,
            },
        )

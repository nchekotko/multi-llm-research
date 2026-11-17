from __future__ import annotations

import contextlib
import json
import platform
import sqlite3
import time
from importlib.metadata import version as package_version
from pathlib import Path

from .config import canonical, digest
from .contracts import CandidateArtifact, TaskSpec, new_id, utcnow
from .security import ensure_no_secrets


class Conflict(ValueError):
    pass


class BudgetExceeded(ValueError):
    pass


MIGRATION_1 = """
CREATE TABLE runs (
 id TEXT PRIMARY KEY, task_json TEXT NOT NULL, config_json TEXT NOT NULL,
 task_hash TEXT NOT NULL, config_hash TEXT NOT NULL, code_hash TEXT NOT NULL, schema_hash TEXT NOT NULL,
 idempotency_key TEXT UNIQUE, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deadline REAL NOT NULL,
 cancel_requested INTEGER NOT NULL DEFAULT 0, owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
 final_id TEXT, repairs INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE nodes (
 run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL, status TEXT NOT NULL,
 artifact_id TEXT, PRIMARY KEY(run_id,node_id)
);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL,
 kind TEXT NOT NULL, version INTEGER NOT NULL, body_json TEXT NOT NULL, sha256 TEXT NOT NULL,
 parents_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,node_id,version)
);
CREATE TABLE calls (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL, family TEXT NOT NULL, provider TEXT NOT NULL,
 model_id TEXT NOT NULL, revision TEXT NOT NULL, request_json TEXT NOT NULL,
 prompt_hash TEXT NOT NULL, status TEXT NOT NULL, reserved_microusd INTEGER NOT NULL,
 actual_microusd INTEGER, input_tokens INTEGER, output_tokens INTEGER, response_json TEXT,
 latency_ms INTEGER, created_at TEXT NOT NULL, error_code TEXT, provider_request_id TEXT
);
CREATE TABLE events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
 at TEXT NOT NULL, event TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE approvals (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), package_hash TEXT NOT NULL,
 actor TEXT NOT NULL, decision TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX calls_run ON calls(run_id,node_id);
CREATE INDEX artifacts_run ON artifacts(run_id);
CREATE INDEX events_run ON events(run_id,seq);
PRAGMA user_version = 1;
"""

MIGRATION_2 = """
ALTER TABLE runs ADD COLUMN environment_json TEXT NOT NULL DEFAULT '{}';
PRAGMA user_version = 2;
"""


def environment_snapshot() -> dict:
    lock = Path(__file__).resolve().parent.parent / "requirements.lock.txt"
    import hashlib

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": {name: package_version(name) for name in ("fastapi", "pydantic", "httpx", "uvicorn")},
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
    }


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in MIGRATION_1.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                version = 1
            if version == 1:
                for statement in MIGRATION_2.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            elif version != 2:
                raise RuntimeError(f"unsupported database schema version: {version}")

    @contextlib.contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_run(
        self,
        task: TaskSpec,
        config: dict,
        code_hash: str,
        schema_hash: str,
        idempotency_key: str | None = None,
    ) -> str:
        task_data = task.model_dump(mode="json")
        ensure_no_secrets(task_data)
        ensure_no_secrets(config)
        ensure_no_secrets(idempotency_key)
        now, run_id = utcnow(), new_id()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM runs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["task_hash"] != digest(task_data) or existing["config_hash"] != digest(config):
                    raise Conflict("idempotency key already used with different input/config")
                return existing["id"]
            conn.execute(
                """INSERT INTO runs
                (id,task_json,config_json,task_hash,config_hash,code_hash,schema_hash,idempotency_key,
                 status,created_at,updated_at,deadline,environment_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    canonical(task_data),
                    canonical(config),
                    digest(task_data),
                    digest(config),
                    code_hash,
                    schema_hash,
                    idempotency_key,
                    "queued",
                    now,
                    now,
                    time.time() + task.limits.max_seconds,
                    canonical(environment_snapshot()),
                ),
            )
            self._event(conn, run_id, "run_created", {"task_sha256": digest(task_data)})
        return run_id

    @staticmethod
    def _event(conn, run_id: str, event: str, payload: dict):
        ensure_no_secrets(payload)
        conn.execute(
            "INSERT INTO events(run_id,at,event,payload_json) VALUES (?,?,?,?)",
            (run_id, utcnow(), event, canonical(payload)),
        )

    def event(self, run_id: str, event: str, payload: dict):
        with self.connection() as conn:
            self._event(conn, run_id, event, payload)

    def run(self, run_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("run not found")
        return dict(row)

    def list_runs(self) -> list[dict]:
        with self.connection() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id,status,reason,created_at,updated_at,final_id FROM runs ORDER BY created_at DESC LIMIT 200"
                )
            ]

    def runnable(self) -> list[str]:
        with self.connection() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM runs WHERE status IN ('queued','running') AND lease_until < ? ORDER BY created_at",
                    (time.time(),),
                )
            ]

    def claim(self, run_id: str, owner: str, lease_seconds: float = 15) -> bool:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run not found")
            if row["status"] not in {"queued", "running"} or row["lease_until"] >= time.time():
                return False
            conn.execute(
                "UPDATE runs SET owner=?,lease_until=?,status='running',updated_at=? WHERE id=?",
                (owner, time.time() + lease_seconds, utcnow(), run_id),
            )
            # A crashed request may have incurred costs. Keep its full reservation in accounting.
            conn.execute(
                "UPDATE calls SET status='unknown',error_code='worker_lost' WHERE run_id=? AND status='reserved'",
                (run_id,),
            )
            self._event(conn, run_id, "worker_claimed", {"owner": owner})
        return True

    def heartbeat(self, run_id: str, owner: str):
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE runs SET lease_until=? WHERE id=? AND owner=?", (time.time() + 15, run_id, owner)
            )
            if not cur.rowcount:
                raise Conflict("worker lost lease")

    def release_lease(self, run_id: str, owner: str):
        with self.connection() as conn:
            conn.execute("UPDATE runs SET owner=NULL,lease_until=0 WHERE id=? AND owner=?", (run_id, owner))

    def status(self, run_id: str, status: str, reason: str = "", owner: str | None = None):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run not found")
            if owner is not None and row["owner"] != owner:
                raise Conflict("worker lost ownership")
            if row["cancel_requested"]:
                status, reason = "cancelled", "cancel requested"
            conn.execute(
                "UPDATE runs SET status=?,reason=?,updated_at=? WHERE id=?",
                (status, reason, utcnow(), run_id),
            )
            self._event(conn, run_id, "status_changed", {"status": status, "reason": reason})

    def cancel(self, run_id: str):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run not found")
            if row[0] in {"completed", "rejected", "cancelled"}:
                raise Conflict("run is already final")
            conn.execute(
                "UPDATE runs SET cancel_requested=1,status='cancelled',reason='cancel requested',updated_at=? WHERE id=?",
                (utcnow(), run_id),
            )
            self._event(conn, run_id, "cancel_requested", {})

    def resume(self, run_id: str):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run not found")
            if row["status"] in {
                "completed",
                "rejected",
                "cancelled",
                "waiting_approval",
                "budget_exceeded",
                "timed_out",
                "validation_failed",
                "inconclusive",
            }:
                raise Conflict("run cannot resume in this state; use a new task for changed input/limits")
            if row["lease_until"] >= time.time():
                raise Conflict("worker still owns this run")
            conn.execute(
                "UPDATE runs SET status='queued',reason='',updated_at=? WHERE id=?", (utcnow(), run_id)
            )
            self._event(conn, run_id, "resume_requested", {})

    def node(self, run_id: str, node_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM nodes WHERE run_id=? AND node_id=?", (run_id, node_id)
            ).fetchone()
            return dict(row) if row else None

    def commit_artifact(
        self,
        run_id: str,
        node_id: str,
        kind: str,
        body: dict,
        parents: list[str],
        owner: str,
        checkpoint: bool = True,
    ) -> CandidateArtifact:
        ensure_no_secrets(body)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run["owner"] != owner or run["lease_until"] < time.time() or run["cancel_requested"]:
                raise Conflict("artifact commit rejected: lease lost or cancellation")
            for parent in parents:
                if not conn.execute(
                    "SELECT 1 FROM artifacts WHERE id=? AND run_id=?", (parent, run_id)
                ).fetchone():
                    raise Conflict("artifact parent is missing or belongs to another run")
            ver = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM artifacts WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()[0]
            artifact_id, now = new_id(), utcnow()
            hashed = digest(body)
            conn.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                (artifact_id, run_id, node_id, kind, ver, canonical(body), hashed, canonical(parents), now),
            )
            if checkpoint:
                conn.execute(
                    "INSERT INTO nodes VALUES (?,?,?,?) ON CONFLICT(run_id,node_id) DO UPDATE SET status='completed',artifact_id=excluded.artifact_id",
                    (run_id, node_id, "completed", artifact_id),
                )
            self._event(
                conn,
                run_id,
                "artifact_committed",
                {"artifact_id": artifact_id, "node_id": node_id, "sha256": hashed},
            )
        return CandidateArtifact(
            artifact_id=artifact_id,
            kind=kind,
            version=ver,
            uri="artifact://" + artifact_id,
            sha256=hashed,
            parent_ids=parents,
            created_by=node_id,
            created_at=now,
        )

    def artifact(self, run_id: str, artifact_id: str) -> tuple[CandidateArtifact, dict]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id=? AND run_id=?", (artifact_id, run_id)
            ).fetchone()
        if row is None:
            raise KeyError("artifact not found")
        body = json.loads(row["body_json"])
        if digest(body) != row["sha256"]:
            raise Conflict("artifact integrity check failed")
        meta = CandidateArtifact(
            artifact_id=row["id"],
            kind=row["kind"],
            version=row["version"],
            uri="artifact://" + row["id"],
            sha256=row["sha256"],
            parent_ids=json.loads(row["parents_json"]),
            created_by=row["node_id"],
            created_at=row["created_at"],
        )
        return meta, body

    def artifacts(self, run_id: str) -> list[CandidateArtifact]:
        with self.connection() as conn:
            ids = [
                r[0]
                for r in conn.execute("SELECT id FROM artifacts WHERE run_id=? ORDER BY rowid", (run_id,))
            ]
        return [self.artifact(run_id, aid)[0] for aid in ids]

    def checkpoint(self, run_id: str, node_id: str) -> tuple[CandidateArtifact, dict] | None:
        node = self.node(run_id, node_id)
        return self.artifact(run_id, node["artifact_id"]) if node and node["status"] == "completed" else None

    def calls(self, run_id: str, node_id: str | None = None) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM calls WHERE run_id=?"
                + (" AND node_id=?" if node_id else "")
                + " ORDER BY rowid",
                (run_id, node_id) if node_id else (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def reserve_call(
        self, run_id: str, node_id: str, model, request: dict, prompt_hash: str, reserved: int, owner: str
    ) -> str:
        ensure_no_secrets(request)
        call_id = new_id()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row["owner"] != owner or row["lease_until"] < time.time() or row["cancel_requested"]:
                raise Conflict("call rejected: lease lost or cancellation")
            task = TaskSpec.model_validate_json(row["task_json"])
            stats = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_microusd,reserved_microusd)),0) FROM calls WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if stats[0] >= task.limits.max_calls or stats[1] + reserved > int(
                task.limits.max_cost_usd * 1000000
            ):
                raise BudgetExceeded("call count or reserved cost exceeds run limit")
            if time.time() >= row["deadline"]:
                raise TimeoutError("run deadline reached")
            conn.execute(
                """INSERT INTO calls
                (id,run_id,node_id,candidate_id,family,provider,model_id,revision,request_json,prompt_hash,
                 status,reserved_microusd,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    call_id,
                    run_id,
                    node_id,
                    model.candidate_id,
                    model.family,
                    model.provider,
                    model.model_id,
                    model.revision,
                    canonical(request),
                    prompt_hash,
                    "reserved",
                    reserved,
                    utcnow(),
                ),
            )
            self._event(
                conn,
                run_id,
                "call_reserved",
                {
                    "call_id": call_id,
                    "node_id": node_id,
                    "candidate_id": model.candidate_id,
                    "reserved_microusd": reserved,
                },
            )
        return call_id

    def finish_call(
        self,
        call_id: str,
        *,
        status: str,
        actual: int | None = None,
        response: dict | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        error_code: str | None = None,
        provider_request_id: str | None = None,
        minimum_reservation: int = 0,
    ):
        if response is not None:
            ensure_no_secrets(response)
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE calls SET status=?,actual_microusd=?,response_json=?,input_tokens=?,output_tokens=?,latency_ms=?,error_code=?,provider_request_id=?,reserved_microusd=MAX(reserved_microusd,?) WHERE id=? AND status='reserved'",
                (
                    status,
                    actual,
                    canonical(response) if response is not None else None,
                    input_tokens,
                    output_tokens,
                    latency_ms,
                    error_code,
                    provider_request_id,
                    minimum_reservation,
                    call_id,
                ),
            )
            if not cur.rowcount:
                return
            row = conn.execute("SELECT run_id FROM calls WHERE id=?", (call_id,)).fetchone()
            self._event(
                conn,
                row[0],
                "call_finished",
                {"call_id": call_id, "status": status, "actual_microusd": actual, "error_code": error_code},
            )

    def invalidate_call(self, call_id: str, code: str):
        with self.connection() as conn:
            conn.execute(
                "UPDATE calls SET status='invalid',error_code=? WHERE id=? AND status='succeeded'",
                (code, call_id),
            )
            row = conn.execute("SELECT run_id,node_id FROM calls WHERE id=?", (call_id,)).fetchone()
            self._event(
                conn,
                row[0],
                "model_contract_rejected",
                {"call_id": call_id, "node_id": row[1], "reason": code},
            )

    def set_final(self, run_id: str, artifact_id: str, owner: str):
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE runs SET final_id=? WHERE id=? AND owner=? AND cancel_requested=0",
                (artifact_id, run_id, owner),
            )
            if not cur.rowcount:
                raise Conflict("final commit rejected")

    def approval(self, run_id: str, package_hash: str, actor: str, accept: bool):
        ensure_no_secrets(actor)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run not found")
            if row["status"] != "waiting_approval" or row["cancel_requested"]:
                raise Conflict("run is not awaiting approval")
            artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (row["final_id"],)).fetchone()
            if (
                artifact["sha256"] != package_hash
                or digest(json.loads(artifact["body_json"])) != package_hash
            ):
                raise Conflict("approval is not bound to the current package hash")
            conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?)",
                (new_id(), run_id, package_hash, actor, "accepted" if accept else "rejected", utcnow()),
            )
            conn.execute(
                "UPDATE runs SET status=?,reason='',updated_at=? WHERE id=?",
                ("completed" if accept else "rejected", utcnow(), run_id),
            )
            self._event(
                conn,
                run_id,
                "human_release",
                {"actor": actor, "package_hash": package_hash, "accepted": accept},
            )

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self.connection() as conn:
            return [
                {**dict(r), "payload": json.loads(r["payload_json"])}
                for r in conn.execute(
                    "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after)
                )
            ]

    def approvals(self, run_id: str) -> list[dict]:
        with self.connection() as conn:
            return [
                dict(r) for r in conn.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY at", (run_id,))
            ]

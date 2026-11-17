import hmac
import os
import threading
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .config import AppConfig, load_config
from .contracts import TaskSpec
from .engine import Engine
from .liquidity import LiquiditySpec
from .market_scanner import ScanSpec
from .replay import replay
from .security import PolicyDenied
from .storage import Conflict, Store
from .validation import GateFailed


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=100)
    accept: bool


def create_app(root="var", config: AppConfig | None = None, token: str | None = None, start_worker=True):
    token = token or os.environ.get("MULTILLM_API_TOKEN")
    if not token or len(token) < 24:
        raise RuntimeError("set MULTILLM_API_TOKEN to a random value of at least 24 characters")
    store = Store(root)
    engine = Engine(store, config or load_config(os.environ.get("MULTILLM_CONFIG")))
    stop = threading.Event()

    def worker():
        while not stop.wait(0.2):
            for run_id in store.runnable():
                if stop.is_set():
                    return
                try:
                    engine.run(run_id)
                except Exception:
                    # Engine persists a sanitized error and state; the queue continues.
                    continue

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=worker, daemon=True)
        console_thread = threading.Thread(target=app.state.console_jobs.work, args=(stop,), daemon=True)
        if start_worker:
            thread.start()
            console_thread.start()
        yield
        stop.set()
        if start_worker:
            thread.join(timeout=2)
            console_thread.join(timeout=2)

    app = FastAPI(title="Multi-LLM Core", version="0.1.0", lifespan=lifespan, root_path=os.environ.get("MULTILLM_ROOT_PATH", ""))
    app.state.engine = engine
    security = HTTPBearer(auto_error=False)

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]):
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="bearer token required")

    def checked_run(run_id: str):
        try:
            return store.run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(GateFailed)
    async def gate_handler(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PolicyDenied)
    async def policy_handler(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        from fastapi.responses import JSONResponse

        # Pydantic's default `input` field can contain a whole confidential document.
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/v1/research/catalog", dependencies=[Depends(authorize)])
    def research_catalog():
        from .economic_calendar import EconomicCalendar

        return EconomicCalendar().catalog()

    @app.get("/v1/algopack/datasets", dependencies=[Depends(authorize)])
    def algopack_datasets():
        import json

        from .algopack import PROJECT

        return [
            {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "receipts"}
            for path in sorted((PROJECT / "var/algopack/datasets").glob("*/manifest.json"))
        ]

    @app.post("/v1/algopack/liquidity-backtest", dependencies=[Depends(authorize)])
    def liquidity_backtest(spec: LiquiditySpec):
        from .liquidity import run_liquidity

        try:
            return run_liquidity(spec, source_state=root)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(
                status_code=409, detail="invalid or unavailable immutable study inputs"
            ) from exc

    @app.post("/v1/algopack/market-scan", dependencies=[Depends(authorize)])
    def market_scan(spec: ScanSpec):
        from .market_scanner import run_scan

        try:
            return run_scan(spec)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(
                status_code=409, detail="invalid or unavailable immutable scan inputs"
            ) from exc

    @app.get("/v1/algopack/{kind}/{identity}", dependencies=[Depends(authorize)])
    def algopack_result(kind: str, identity: str):
        import json
        import re

        from .algopack import PROJECT

        if kind not in {"studies", "scans"} or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise HTTPException(status_code=404, detail="result not found")
        path = PROJECT / "var/algopack" / kind / identity / "result.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="result not found")
        from .config import digest

        result = json.loads(path.read_text(encoding="utf-8"))
        seal = path.with_name("integrity.json")
        if (
            not seal.exists()
            or digest(result) != json.loads(seal.read_text(encoding="utf-8"))["result_sha256"]
        ):
            raise HTTPException(status_code=409, detail="result integrity check failed")
        return result

    @app.get("/v1/research/futures", dependencies=[Depends(authorize)])
    def research_futures():
        from .economic_calendar import EconomicCalendar

        return EconomicCalendar().futures()

    @app.get("/v1/research/hypotheses", dependencies=[Depends(authorize)])
    def research_hypotheses():
        from .economic_calendar import EconomicCalendar

        return EconomicCalendar().hypotheses()

    @app.get("/v1/research/hypotheses/{hypothesis_id}", dependencies=[Depends(authorize)])
    def research_hypothesis(hypothesis_id: str):
        from .economic_calendar import EconomicCalendar

        item = next(
            (x for x in EconomicCalendar().hypotheses(True) if x["hypothesis_id"] == hypothesis_id), None
        )
        if item is None:
            raise HTTPException(status_code=404, detail="hypothesis not found")
        return item

    @app.get("/v1/research/preview/{database}/{table}", dependencies=[Depends(authorize)])
    def research_preview(database: str, table: str, limit: int = Query(default=20, ge=1, le=100)):
        from .economic_calendar import EconomicCalendar

        return EconomicCalendar().preview(database, table, limit)

    @app.post("/v1/runs", status_code=202, dependencies=[Depends(authorize)])
    def submit(task: TaskSpec, idempotency_key: str | None = Header(default=None, max_length=200)):
        run_id = engine.submit(task, idempotency_key)
        return {"run_id": run_id, "status": store.run(run_id)["status"]}

    @app.get("/v1/runs", dependencies=[Depends(authorize)])
    def list_runs():
        return store.list_runs()

    @app.get("/v1/runs/{run_id}", dependencies=[Depends(authorize)])
    def status(run_id: str):
        run = checked_run(run_id)
        return {k: run[k] for k in ("id", "status", "reason", "created_at", "updated_at", "final_id")}

    @app.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(authorize)])
    def cancel(run_id: str):
        checked_run(run_id)
        store.cancel(run_id)
        return status(run_id)

    @app.post("/v1/runs/{run_id}/resume", dependencies=[Depends(authorize)])
    def resume(run_id: str):
        checked_run(run_id)
        store.resume(run_id)
        return status(run_id)

    @app.get("/v1/runs/{run_id}/manifest", dependencies=[Depends(authorize)])
    def manifest(run_id: str):
        checked_run(run_id)
        return engine.manifest(run_id)

    @app.get("/v1/runs/{run_id}/events", dependencies=[Depends(authorize)])
    def events(run_id: str, after: int = 0):
        checked_run(run_id)
        return store.events(run_id, after)

    @app.get("/v1/runs/{run_id}/artifacts/{artifact_id}", dependencies=[Depends(authorize)])
    def artifact(run_id: str, artifact_id: str):
        checked_run(run_id)
        try:
            meta, body = store.artifact(run_id, artifact_id)
            return {"metadata": meta.model_dump(mode="json"), "body": body}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc

    @app.get("/v1/runs/{run_id}/result", dependencies=[Depends(authorize)])
    def result(run_id: str):
        run = checked_run(run_id)
        if not run["final_id"]:
            raise HTTPException(status_code=409, detail="no final package yet")
        meta, body = store.artifact(run_id, run["final_id"])
        return {
            "run_status": run["status"],
            "package_sha256": meta.sha256,
            "package": body,
            "approvals": store.approvals(run_id),
        }

    @app.post("/v1/runs/{run_id}/approval", dependencies=[Depends(authorize)])
    def approve(run_id: str, decision: ApprovalRequest):
        checked_run(run_id)
        if decision.accept:
            replay(store, run_id)
        store.approval(run_id, decision.package_sha256, decision.actor, decision.accept)
        return status(run_id)

    @app.post("/v1/runs/{run_id}/replay", dependencies=[Depends(authorize)])
    def replay_run(run_id: str):
        checked_run(run_id)
        return replay(store, run_id)

    @app.get("/v1/schemas", dependencies=[Depends(authorize)])
    def schemas():
        from .algopack import DownloadSpec
        from .contracts import CONTRACTS

        contracts = {**CONTRACTS, **{c.__name__: c for c in (DownloadSpec, LiquiditySpec, ScanSpec)}}
        return {name: contract.model_json_schema() for name, contract in contracts.items()}

    from .console import install_console

    install_console(app, authorize, engine, root)
    return app

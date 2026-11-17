"""Local research console: summaries and a persisted, bounded analytics job queue."""
import json
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .algopack import PROJECT, AlgoPack, DownloadSpec, save_json
from .contracts import utcnow
from .liquidity import LiquiditySpec, run_liquidity, verify_study
from .market_scanner import ScanSpec, run_scan, verify_scan


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["download", "liquidity", "scan", "liquidity_replay", "scan_replay"]
    parameters: dict


class ConsoleJobs:
    def __init__(self, root):
        self.state_root = Path(root)
        self.root = Path(root) / "console-jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        for path in self.root.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["status"] == "running":
                value.update(status="interrupted", error="Сервер перезапущен. Повторите задачу: страницы выгрузки сохранены.")
                save_json(path, value)

    def list(self):
        result = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("*.json")]
        return sorted(result, key=lambda x:x["created_at"], reverse=True)[:100]

    def submit(self, request):
        if request.kind in {"download", "liquidity", "scan"}:
            cls = {"download": DownloadSpec, "liquidity": LiquiditySpec, "scan": ScanSpec}[request.kind]
            parameters = cls.model_validate(request.parameters).model_dump(mode="json")
        else:
            import re
            identity = request.parameters.get("identity", "")
            if not re.fullmatch(r"[a-f0-9]{64}", identity):
                raise ValueError("invalid identity")
            parameters = {"identity": identity}
        with self.lock:
            for old in self.list():
                if old["status"] in {"queued", "running"} and old["kind"] == request.kind and old["parameters"] == parameters:
                    return old
            job = {"id": uuid4().hex, "kind": request.kind, "parameters": parameters,
                   "status": "queued", "created_at": utcnow(), "progress": None}
            save_json(self.root / (job["id"]+".json"), job)
            return job

    def work(self, stop):
        while not stop.wait(0.3):
            pending = [j for j in self.list() if j["status"] == "queued"]
            if not pending:
                continue
            job = pending[-1]
            path = self.root / (job["id"]+".json")
            job.update(status="running", started_at=utcnow())
            save_json(path, job)
            try:
                kind, params = job["kind"], job["parameters"]
                if kind == "download":
                    client = AlgoPack()
                    def progress(value, current_job=job, current_path=path):
                        current_job["progress"] = value
                        save_json(current_path, current_job)
                    try:
                        result = client.download(DownloadSpec.model_validate(params), progress)
                        result = {"dataset_id":result["dataset_id"], "products":result["products"]}
                    finally:
                        client.close()
                elif kind == "liquidity":
                    result = run_liquidity(LiquiditySpec.model_validate(params), source_state=self.state_root)
                elif kind == "scan":
                    result = run_scan(ScanSpec.model_validate(params))
                elif kind == "liquidity_replay":
                    result = verify_study(PROJECT / "var/algopack", params["identity"])
                else:
                    result = verify_scan(PROJECT / "var/algopack", params["identity"])
                job.update(status="completed", result=result)
            except Exception as exc:
                job.update(status="failed", error=f"{type(exc).__name__}: проверьте конфигурацию источника, параметры и доступность данных.")
            job["finished_at"] = utcnow()
            save_json(path, job)


def install_console(app, authorize, engine, root):
    assets = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=assets), name="console-assets")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(assets / "index.html", headers={"Cache-Control":"no-store"})

    jobs = ConsoleJobs(root)
    app.state.console_jobs = jobs

    @app.get("/v1/console/overview", dependencies=[Depends(authorize)])
    def overview():
        datasets, studies, scans = [], [], []
        for path in sorted((PROJECT / "var/algopack/datasets").glob("*/manifest.json")):
            m = json.loads(path.read_text(encoding="utf-8"))
            datasets.append({k:v for k,v in m.items() if k != "receipts"})
        for path in sorted((PROJECT / "var/algopack/studies").glob("*/result.json")):
            m = json.loads(path.read_text(encoding="utf-8"))
            studies.append({"id":path.parent.name,"decision":m["decision"],"policy":m["policy"],
                            "results":[{k:v for k,v in r.items() if k not in {"fills","equity"}} for r in m["results"]]})
        for path in sorted((PROJECT / "var/algopack/scans").glob("*/result.json")):
            m = json.loads(path.read_text(encoding="utf-8"))
            scans.append({"id":path.parent.name, **{k:v for k,v in m.items() if k in {"status","universe_size","events_count","family_counts","exclusions"}}})
        runs = engine.store.list_runs()
        for run in runs:
            task = json.loads(engine.store.run(run["id"])["task_json"])
            run.update(goal=task["goal"],task_type=task["task_type"])
        models = [{"model":m.model_id,"family":m.family,"provider":m.provider,"roles":[q.role for q in m.qualified if q.passed]}
                  for m in engine.config.models if m.enabled]
        return {"datasets":datasets,"studies":studies,"scans":scans,"runs":runs,"jobs":jobs.list(),"models":models,
                "qualification_scope":engine.config.qualification_scope,"now":utcnow()}

    @app.post("/v1/console/jobs", status_code=202, dependencies=[Depends(authorize)])
    def submit_job(request: JobRequest):
        try:
            return jobs.submit(request)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422,detail="Проверьте параметры задачи и диапазон дат.") from exc

    @app.get("/v1/console/jobs", dependencies=[Depends(authorize)])
    def list_jobs():
        return jobs.list()

    @app.get("/v1/console/templates", dependencies=[Depends(authorize)])
    def templates():
        result = {}
        for name in ["moex-spread", "algopack-si-download", "algopack-market-download", "algopack-liquidity", "algopack-market-scan"]:
            path = PROJECT / "examples" / (name+".json")
            if path.exists():
                result[name] = json.loads(path.read_text(encoding="utf-8"))
        return result

"""Versioned operator settings and measured usage; no credential values leave the server."""
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Query
from pydantic import Field

from .algopack import PROJECT, DownloadSpec, save_json
from .config import AppConfig, RoleSettings
from .contracts import Contract, Limits, ResearchSpec, utcnow
from .liquidity import LiquiditySpec
from .market_scanner import ScanSpec
from .security import ensure_no_secrets
from .storage import Conflict

ROLES = {"planner": "Планировщик", "executor": "Аналитик / исполнитель", "reviewer": "Независимый рецензент", "reporter": "Автор отчёта"}
MODULES = [
    ("planner", "LLM", "Планирует общий workflow. В часовом исследовании план фиксируется кодом."),
    ("executor", "LLM", "Анализирует ограниченную сводку результатов; не рассчитывает PnL."),
    ("reviewer", "LLM", "Проверяет выводы другой модельной семьи и пробелы доказательств."),
    ("reporter", "LLM", "Готовит отчёт общего workflow. Итог MOEX-профиля собирается кодом."),
    ("economic", "Данные", "Семь allowlisted SQLite-баз; запросы только для чтения."),
    ("registry", "Данные", "Реестр гипотез Economic Calendar с фиксированными ревизиями."),
    ("download", "Данные", "AlgoPack: статистика и минутные свечи, кэш и контроль целостности."),
    ("research", "Расчёт", "Часовые сигналы, train/OOS, сценарные издержки и журнал сделок."),
    ("liquidity", "Расчёт", "Минутное переисполнение с доступной глубиной, потоком и издержками."),
    ("scan", "Расчёт", "Пять детекторов аномалий; использует только доступные на момент сигнала данные."),
    ("validation", "Контроль", "Проверки схем, времени доступности, полноты данных и ограничений исследования."),
    ("replay", "Контроль", "Повторный расчёт по сохранённым артефактам без вызова моделей."),
    ("budget", "Контроль", "Лимиты по умолчанию для новых исследований из интерфейса."),
    ("queue", "Сервис", "Durable workflow и последовательная очередь аналитики."),
    ("news", "Данные", "Новости Economic Calendar, все группы опционов MOEX и последующее исследование реакции. Детерминированная привязка, без LLM и торговых поручений."),
    ("options", "Расчёт", "Сбор стаканов, структурные отклонения, признаки ликвидности и последующая проверка исполнения. Настройки — в разделе «Опционы: поиск edge». LLM не вызывается."),
]
NAMES = {**ROLES, "economic":"Базы Economic Calendar", "registry":"Реестр гипотез", "download":"Загрузчик AlgoPack", "research":"Часовой бэктест", "liquidity":"Исполнение и ликвидность", "scan":"Сканер рынка", "validation":"Валидатор и финальный допуск", "replay":"Replay", "budget":"Бюджеты и лимиты", "queue":"Очередь задач"}
NAMES["options"] = "Опционный сканер"
NAMES["news"] = "Новости и реакция опционов"


def initial_defaults():
    liquidity = LiquiditySpec(dataset_id="a"*64, source_run_id="a"*32).model_dump(mode="json", exclude={"schema_version","dataset_id","source_run_id"})
    scan = ScanSpec(dataset_id="a"*64, as_of="2026-01-01T00:00:00+03:00").model_dump(mode="json", exclude={"schema_version","dataset_id","as_of"})
    research = {k:v.default for k,v in ResearchSpec.model_fields.items() if k in {"lookback","entry_z","exit_z","fee_rub_per_contract","slippage_ticks","latency_seconds","entry_hour_msk","last_entry_hour_msk","flatten_hour_msk"}}
    return {"liquidity":liquidity, "scan":scan, "research":research,
            "download":{"products":["candles_1m","tradestats","obstats","orderstats"]},
            "budget":Limits(max_calls=4, max_repairs=1).model_dump(mode="json", exclude={"schema_version"})}


class Change(Contract):
    revision: int = Field(ge=0)
    settings: dict


class ModuleControl:
    def __init__(self, engine, root):
        self.engine = engine
        self.path = Path(root) / "module-settings.json"
        self.lock = threading.RLock()
        if self.path.exists():
            self.value = json.loads(self.path.read_text(encoding="utf-8"))
            engine.config = AppConfig.model_validate(self.value["config"])
        else:
            self.value = {"revision":0,"config":engine.config.model_dump(mode="json"),"defaults":initial_defaults(),"history":[]}

    def defaults(self):
        with self.lock:
            return json.loads(json.dumps(self.value["defaults"]))

    def change(self, module, change):
        ensure_no_secrets(change.settings)
        with self.lock:
            if change.revision != self.value["revision"]:
                raise Conflict("Настройки изменены в другой вкладке. Обновите страницу.")
            value = json.loads(json.dumps(self.value))
            config = AppConfig.model_validate(value["config"])
            if module in ROLES:
                settings = RoleSettings.model_validate(change.settings).model_dump(mode="json")
                before = config.role_settings.get(module, RoleSettings()).model_dump(mode="json")
                raw = config.model_dump(mode="json")
                raw["role_settings"][module] = settings
                raw["config_version"] = "console-" + str(value["revision"]+1)
                config = AppConfig.model_validate(raw)
                value["config"] = config.model_dump(mode="json")
            elif module in value["defaults"]:
                before = value["defaults"][module]
                if set(change.settings) - set(initial_defaults()[module]):
                    raise ValueError("unknown configurable field")
                settings = {**before, **change.settings}
                if module == "liquidity":
                    LiquiditySpec.model_validate({"dataset_id":"a"*64,"source_run_id":"a"*32,**settings})
                elif module == "scan":
                    ScanSpec.model_validate({"dataset_id":"a"*64,"as_of":"2026-01-01T00:00:00+03:00",**settings})
                elif module == "research":
                    template = json.loads((PROJECT/"examples/moex-spread.json").read_text(encoding="utf-8"))["research"]
                    ResearchSpec.model_validate({**template, **settings})
                elif module == "budget":
                    settings = Limits.model_validate(settings).model_dump(mode="json",exclude={"schema_version"})
                elif module == "download":
                    DownloadSpec.model_validate({"symbols":["SiZ6"],"start":"2026-01-01","end":"2026-01-02",**settings})
                value["defaults"][module] = settings
            else:
                raise ValueError("module settings are not editable")
            value["revision"] += 1
            value["history"].append({"revision":value["revision"],"at":utcnow(),"module":module,"before":before,"after":settings})
            value["history"] = value["history"][-100:]
            save_json(self.path, value)
            self.value = value
            self.engine.config = config
            return {"revision":value["revision"],"applies_to":"new_tasks", "settings":settings}

    def usage(self, days):
        since = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat() if days else ""
        fields = """COUNT(*) calls, SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) succeeded,
        SUM(CASE WHEN status IN ('failed','invalid') THEN 1 ELSE 0 END) errors,
        COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens,
        SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END) missing_token_usage,
        COALESCE(SUM(actual_microusd),0)/1000000.0 known_cost_usd,
        COALESCE(SUM(CASE WHEN json_extract(response_json,'$.reported_cost_usd') IS NOT NULL THEN actual_microusd ELSE 0 END),0)/1000000.0 reported_cost_usd,
        COALESCE(SUM(CASE WHEN actual_microusd IS NULL THEN reserved_microusd ELSE 0 END),0)/1000000.0 unresolved_reserved_usd,
        SUM(CASE WHEN actual_microusd IS NULL THEN 1 ELSE 0 END) unresolved_calls,
        COALESCE(SUM(latency_ms),0) latency_total_ms, COUNT(latency_ms) latency_count, MAX(created_at) last_call"""
        with self.engine.store.connection() as conn:
            groups = [dict(r) for r in conn.execute("SELECT json_extract(request_json,'$.role') role, model_id, provider, " + fields + " FROM calls WHERE created_at>=? GROUP BY role,model_id,provider", (since,))]
            total = dict(conn.execute("SELECT " + fields + " FROM calls WHERE created_at>=?", (since,)).fetchone())
            recent = [dict(r) for r in conn.execute("SELECT id,run_id,node_id,model_id,provider,json_extract(request_json,'$.role') role,status,input_tokens,output_tokens,actual_microusd, reserved_microusd,latency_ms,created_at,error_code FROM calls WHERE created_at>=? ORDER BY created_at DESC LIMIT 100", (since,))]
            nodes = [dict(r) for r in conn.execute("SELECT node_id,status,COUNT(*) count FROM nodes GROUP BY node_id,status")]
        return {"days":days,"scope":"current_server_state","total":total,"groups":groups,"recent":recent,"nodes":nodes,
                "note":"Учтены вызовы этого каталога состояния, включая перенесённую историю. Это не баланс OpenRouter. Неопределённые расходы показываются резервом; расходы по тарифам не равны подтверждённому счёту."}

    def snapshot(self, days=0):
        with self.lock:
            config = self.engine.config
            revision = self.value["revision"]
            defaults = self.defaults()
            history = list(self.value["history"][-20:])
        result = []
        for identity, kind, description in MODULES:
            role = identity in ROLES
            models = [m for m in config.models if m.enabled and any(q.role==identity and q.passed and q.quality>=config.minimum_quality for q in m.qualified)] if role else []
            settings = config.role_settings.get(identity, RoleSettings()) if role else None
            choices = [{"candidate_id":m.candidate_id,"model_id":m.model_id,"family":m.family,"provider":m.provider,
                        "max_output_tokens":m.max_output_tokens,"timeout_seconds":m.timeout_seconds,"temperature":m.temperature,"reasoning_effort":m.reasoning_effort,
                        "input_per_million_usd":str(m.input_per_million_usd),"output_per_million_usd":str(m.output_per_million_usd)} for m in models]
            selected = next((m for m in choices if m["candidate_id"]==settings.candidate_id), None) if role else None
            if role and selected is None and settings.candidate_id is None and models:
                winner = min(models,key=lambda m:(m.expected_accepted_cost_usd,m.expected_latency_seconds,m.candidate_id))
                selected = next(m for m in choices if m["candidate_id"]==winner.candidate_id)
            result.append({"id":identity,"title":NAMES[identity],"kind":kind,"description":description,
                           "editable":role or identity in defaults,"settings":settings.model_dump(mode="json") if role else defaults.get(identity),
                           "choices":choices,"selected_model":selected,"enabled":settings.enabled if role else True,
                           "prompt_keys":([identity]+(["research_analyst"] if identity=="executor" else ["research_reviewer"] if identity=="reviewer" else [])) if role else []})
        return {"revision":revision,"modules":result,"usage":self.usage(days),"history":history,"qualification_scope":config.qualification_scope,"defaults":defaults}


def install_modules(app, authorize, engine, root):
    control = ModuleControl(engine, root)
    app.state.module_control = control

    @app.get("/v1/console/modules", dependencies=[Depends(authorize)])
    def modules(days: int = Query(default=0,ge=0,le=365)):
        return control.snapshot(days)

    @app.put("/v1/console/modules/{identity}", dependencies=[Depends(authorize)])
    def update(identity: str, change: Change):
        try:
            return control.change(identity, change)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, "Проверьте параметры и допуск модели к роли.") from exc

    @app.get("/v1/console/modules/{identity}/prompts", dependencies=[Depends(authorize)])
    def prompts(identity: str):
        from .roles import PROMPTS
        module = next((m for m in control.snapshot()["modules"] if m["id"]==identity), None)
        if module is None:
            raise HTTPException(404,"module not found")
        return {"base":{key:PROMPTS[key] for key in module["prompt_keys"]},"operator":module["settings"].get("instructions","") if identity in ROLES else ""}
    return control

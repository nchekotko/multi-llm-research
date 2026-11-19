"""Prospective event studies. News is evidence, never instructions or a trading order."""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from .options_math import rows, valid_quote
from .options_scanner import book

ALIASES = {
    "ASTR": ["группа астра", "астра"],
    "DIAS": ["диасофт"],
    "MSNG": ["мосэнерго"],
    "MTLR": ["мечел"],
    "SPBE": ["спб биржа"],
    "SVCB": ["совкомбанк"],
    "ISKJ": ["артген", "искч"],
    "VKCO": ["вконтакте", "vk company", "вк "],
    "TRNF": ["транснефть"],
    "GAZR": ["газпром", "gazprom"],
    "SBRF": ["сбербанк", "sber"],
    "SBPR": ["сбербанк"],
    "CHMF": ["северстал", "severstal"],
    "ALRS": ["алроса", "alrosa"],
    "LKOH": ["лукойл", "lukoil"],
    "ROSN": ["роснефт", "rosneft"],
    "SIBN": ["газпром нефть"],
    "NOTK": ["новатэк", "novatek"],
    "GMKN": ["норникел", "норильский никель", "nornickel"],
    "PLZL": ["полюс", "polyus"],
    "YDEX": ["яндекс", "yandex"],
    "OZON": ["ozon", "озон"],
    "TCSI": ["т-банк", "т-технолог", "тинькофф"],
    "VTBR": ["втб"],
    "AFLT": ["аэрофлот"],
    "AFKS": ["афк система"],
    "MOEX": ["мосбирж", "московская биржа"],
    "MTSI": ["мтс"],
    "RTKM": ["ростелеком"],
    "NLMK": ["нлмк"],
    "MAGN": ["ммк"],
    "MGNT": ["магнит"],
    "SMLT": ["самолет", "самолёт"],
    "POSI": ["positive technologies", "группа позитив"],
    "RUAL": ["русал", "rusal"],
    "IRAO": ["интер рао"],
    "FEES": ["россети"],
    "HYDR": ["русгидро"],
    "SNGR": ["сургутнефтегаз"],
    "SNGP": ["сургутнефтегаз"],
    "TATN": ["татнефть"],
    "TATP": ["татнефть"],
    "BR": ["нефт", "brent", "crude", "opec", "oil ", "опек"],
    "NG": ["natural gas", "природный газ", "природного газа", "lng", "спг", "henry hub"],
    "GOLD": ["золото", "gold"],
    "GL": ["золото", "gold"],
    "SILV": ["серебр", "silver"],
    "SL": ["серебр", "silver"],
    "PLT": ["платин", "platinum"],
    "WHEAT": ["пшениц", "wheat"],
    "Si": ["доллар", "usd", "рубл", "ruble"],
    "Eu": ["евро", "eur", "ецб"],
    "CNY": ["юан", "yuan", "cny"],
    "UCNY": ["юан", "yuan"],
    "ED": ["eur", "евро", "ецб"],
    "RTS": ["индекс ртс", "rts"],
    "IMX": ["индекс мосбирж", "imoex"],
    "MIX": ["индекс мосбирж", "imoex"],
    "MXI": ["индекс мосбирж", "imoex"],
    "SPYF": ["s&p", "s&p500"],
    "NASD": ["nasdaq"],
    "DAX": ["dax"],
    "HANG": ["hang seng", "гонконг"],
    "STOX": ["euro stoxx"],
}
INDEX = {"RTS", "IMX", "MIX", "MXI", "SPYF", "NASD", "DAX", "HANG", "STOX"}
FX = {"Si", "Eu", "CNY", "UCNY", "ED"}
COMMODITY = {"BR", "NG", "GOLD", "GL", "SILV", "SL", "PLT", "WHEAT"}


def stamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def group(ref):
    asset = ref["ASSETCODE"]
    if asset in INDEX or ref["UNDERLYINGTYPE"] == "I":
        return "indices"
    if asset in FX or ref["UNDERLYINGTYPE"] == "C":
        return "currencies"
    if asset in COMMODITY or ref["UNDERLYINGTYPE"] == "G":
        return "commodities"
    return "equities" if ref["UNDERLYINGTYPE"] == "S" or asset in ALIASES else "unclassified"


def matches(item, ref):
    text = (item["title"] + " " + item.get("summary", "")).lower()
    asset = ref["ASSETCODE"]
    needles = ALIASES.get(asset, [])
    reasons = [word for word in needles if word in text]
    for symbol in (asset, ref["UNDERLYINGASSET"]):
        if len(symbol) >= 3 and re.search(r"(?<!\w)" + re.escape(symbol.lower()) + r"(?!\w)", text):
            reasons.append("ticker:" + symbol)
    if asset == "GAZR" and any(x in text for x in ["газпромбанк", "газпром нефть", "газпромнефть"]):
        reasons = []
    if asset == "OZON" and any(x in text for x in ["озон фарм", "ozon pharma"]):
        reasons = []
    if asset == "MTSI" and any(x in text for x in ["мтс-банк", "мтс банк", "mts bank"]):
        reasons = []
    if group(ref) in {"indices", "currencies"} and any(
        x in text
        for x in [
            "ключевую ставку",
            "ключевой ставк",
            "решение цб",
            "фрс",
            "federal reserve",
            "инфляц",
            "inflation",
        ]
    ):
        reasons.append("macro_exposure_proxy")
    return sorted(set(reasons))


class NewsReactions:
    def __init__(self, scanner):
        self.scanner = scanner
        self.feed_path = scanner.folder.parent / "news-bridge/feed.json"
        self.health = {"status": "waiting_for_bridge"}
        with scanner.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS news_events(id TEXT PRIMARY KEY, first_seen TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS news_studies(id TEXT PRIMARY KEY, event TEXT, state TEXT, body TEXT);
                CREATE INDEX IF NOT EXISTS news_study_event ON news_studies(event);
            """)

    def poll(self, refs, settings, revision):
        if self.scanner.path.stat().st_size + 8 * 1024**2 >= settings.max_storage_mb * 1024**2:
            self.health = {"status": "storage_limit"}
            return
        if not self.feed_path.exists():
            return
        if self.feed_path.stat().st_size > 8 * 1024**2:
            self.health = {"status": "bridge_size_limit"}
            return
        feed = json.loads(self.feed_path.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        exported = stamp(feed.get("exported_at"))
        self.health = {
            "status": "ok" if exported and 0 <= (now - exported).total_seconds() < 240 else "stale_bridge",
            "sources": feed.get("health", {}),
            "exported_at": feed.get("exported_at"),
        }
        representatives = {ref["ASSETCODE"]: ref for ref in refs}
        with self.scanner.connect() as db:
            active_count = db.execute(
                "SELECT COUNT(*) FROM news_studies WHERE state IN ('await_entry','await_exit')"
            ).fetchone()[0]
            for item in sorted(feed.get("items", []), key=lambda x: x.get("first_seen_at", ""), reverse=True)[
                :300
            ]:
                if item.get("publication_precision") == "unknown":
                    item = dict(item, published_at=None)
                content = {k: item.get(k) for k in ("title", "summary", "url", "published_at")}
                identity = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
                if db.execute("SELECT 1 FROM news_events WHERE id=?", (identity,)).fetchone():
                    continue
                received = datetime.now(timezone.utc)
                first = stamp(item.get("first_seen_at"))
                published = stamp(item.get("published_at"))
                age = (received - first).total_seconds() if first else None
                fresh = (
                    not item.get("baseline", True)
                    and age is not None
                    and 0 <= age <= 180
                    and self.health["status"] == "ok"
                )
                if (
                    published
                    and item.get("publication_precision") != "day"
                    and (received - published).total_seconds() > 86400
                ):
                    fresh = False
                linked = []
                reasons_by_asset = {asset: matches(item, ref) for asset, ref in representatives.items()}
                for ref in refs:
                    reason = reasons_by_asset[ref["ASSETCODE"]]
                    if not reason:
                        continue
                    if (
                        ref["LASTTRADEDATE"]
                        <= received.astimezone(timezone(timedelta(hours=3))).date().isoformat()
                    ):
                        continue
                    if reason:
                        linked.append((ref, reason))
                coverage = Counter(group(ref) for ref, _ in linked)
                # Balanced selection by asset, style and call/put; never rank by subsequent return.
                buckets = {}
                for ref, reason in linked:
                    if valid_quote(ref) and (ref.get("UNDERLYINGSETTLEPRICE") or 0) > 0:
                        key = (ref["ASSETCODE"], ref["margin_style"], ref["OPTIONTYPE"])
                        rank = (ref["LASTDELDATE"], abs(ref["STRIKE"] / ref["UNDERLYINGSETTLEPRICE"] - 1))
                        if key not in buckets or rank < buckets[key][0]:
                            buckets[key] = (rank, ref, reason)
                selected = [value[1:] for _, value in sorted(buckets.items())][:12]
                event = {
                    **item,
                    "id": identity,
                    "ingested_at": received.isoformat(),
                    "analysis_completed_at": datetime.now(timezone.utc).isoformat(),
                    "groups": dict(coverage),
                    "linked_series": len(linked),
                    "selected": [
                        {
                            "secid": ref["SECID"],
                            "asset": ref["ASSETCODE"],
                            "group": group(ref),
                            "style": ref["margin_style"] + ref["exercise_style"],
                            "reason": reason,
                        }
                        for ref, reason in selected
                    ],
                    "status": "prospective" if fresh else "archive_no_historical_execution",
                    "method": "entity_rules_v2_no_direction_forecast",
                    "cost_profile": "ALOR_1x_exchange_scenario_unconfirmed",
                }
                db.execute(
                    "INSERT INTO news_events VALUES(?,?,?)",
                    (identity, item.get("first_seen_at"), json.dumps(event)),
                )
                for ref, reason in selected:
                    state = (
                        "await_entry"
                        if fresh and active_count < 40
                        else ("capacity_excluded" if fresh else "archive_only")
                    )
                    baseline = None
                    previous = self.scanner.previous.get(ref["SECID"])
                    if previous and first and stamp(previous["received_at"]) <= first:
                        try:
                            bids, asks = book(previous, settings)
                            baseline = {"mid": (bids[0][0] + asks[0][0]) / 2, "at": previous["received_at"]}
                        except (ValueError, KeyError, IndexError):
                            pass
                    study = {
                        "event_id": identity,
                        "secid": ref["SECID"],
                        "ref": ref,
                        "group": group(ref),
                        "reason": reason,
                        "settings": settings.model_dump(),
                        "revision": revision,
                        "ready_at": event["analysis_completed_at"],
                        "baseline": baseline,
                        "first_seen_at": item.get("first_seen_at"),
                        "observations": [],
                        "cost_profile": event["cost_profile"],
                        "direction": "long_each_selected_call_and_put_separate_not_a_forecast",
                        "certified_edge": False,
                    }
                    db.execute(
                        "INSERT INTO news_studies VALUES(?,?,?,?)",
                        (identity + ":" + ref["SECID"], identity, state, json.dumps(study)),
                    )
                    active_count += int(state == "await_entry")

    def required_refs(self):
        with self.scanner.connect() as db:
            return {
                d["secid"]: d["ref"]
                for row in db.execute(
                    "SELECT body FROM news_studies WHERE state IN ('await_entry','await_exit') ORDER BY rowid LIMIT 40"
                )
                for d in [json.loads(row[0])]
            }

    def observe(self, books, cycle):
        from .options_scanner import OptionsSettings

        current = datetime.now(timezone.utc)
        with self.scanner.connect() as db:
            pending = db.execute(
                "SELECT * FROM news_studies WHERE state IN ('await_entry','await_exit')"
            ).fetchall()
            for row in pending:
                data = json.loads(row["body"])
                settings = OptionsSettings.model_validate(data["settings"])
                ref = data["ref"]
                state = row["state"]
                origin = stamp(data.get("entry_at", data["ready_at"]))
                target = settings.latency_seconds if state == "await_entry" else settings.horizon_seconds
                if state == "await_entry" and (current - origin).total_seconds() < target:
                    continue
                try:
                    if (current - origin).total_seconds() > target + settings.max_gap_seconds:
                        raise ValueError("sampling_gap")
                    record = books[data["secid"]]
                    if (stamp(record["started_at"]) - origin).total_seconds() < (
                        target if state == "await_entry" else 0
                    ):
                        continue
                    bids, asks = book(record, settings)
                    source_at = datetime.strptime(
                        str(rows(record["payload"]["dataversion"])[0]["seqnum"]), "%Y%m%d%H%M%S"
                    ).replace(tzinfo=timezone(timedelta(hours=3)))
                    if (source_at - origin).total_seconds() < (target if state == "await_entry" else 0):
                        continue
                    point = ref["STEPPRICE"] / ref["MINSTEP"]
                    if point <= 0 or not ref.get("BUYSELLFEE") or not ref.get("STEPPRICE"):
                        raise ValueError("missing_contract_costs")
                    observation = {
                        "at": record["received_at"],
                        "source_at": source_at.isoformat(),
                        "cycle": cycle,
                        "bid": bids[0][0],
                        "ask": asks[0][0],
                        "mid": (bids[0][0] + asks[0][0]) / 2,
                        "bid_size": bids[0][1],
                        "ask_size": asks[0][1],
                    }
                    underlying = books.get(ref["UNDERLYINGASSET"])
                    if underlying:
                        try:
                            ub, ua = book(underlying, settings)
                            if (
                                abs(
                                    (
                                        stamp(underlying["received_at"]) - stamp(record["received_at"])
                                    ).total_seconds()
                                )
                                <= settings.max_receipt_skew_seconds
                            ):
                                observation["underlying_mid"] = (ub[0][0] + ua[0][0]) / 2
                        except (ValueError, KeyError, IndexError):
                            pass
                    data["observations"].append(observation)
                    if state == "await_entry":
                        data.update(
                            entry_at=record["received_at"],
                            entry_ask=asks[0][0],
                            entry_cycle=cycle,
                            observed_delay_seconds=(
                                stamp(record["received_at"]) - stamp(data["first_seen_at"])
                            ).total_seconds(),
                        )
                        state = "await_exit"
                    else:
                        gross = (bids[0][0] - data["entry_ask"]) * point
                        exchange = 2 * ref["BUYSELLFEE"]
                        broker = exchange  # ALOR scenario: each side equals exchange fee. Not a confirmed account tariff.
                        slippage = 2 * settings.slippage_ticks * ref["STEPPRICE"]
                        capital = max(ref.get("IMBUY") or 0, data["entry_ask"] * point)
                        duration = (stamp(record["received_at"]) - origin).total_seconds()
                        funding = capital * settings.capital_rate_annual * duration / (365 * 86400)
                        data.update(
                            exit_cycle=cycle,
                            gross_pnl_rub=gross,
                            exchange_fee_rub=exchange,
                            broker_fee_scenario_rub=broker,
                            slippage_rub=slippage,
                            capital_cost_scenario_rub=funding,
                            scenario_pnl_rub=gross - exchange - broker - slippage - funding,
                            observed_holding_seconds=duration,
                            option_mid_change=observation["mid"] - data["observations"][0]["mid"],
                            baseline_missing=data["baseline"] is None,
                        )
                        if "underlying_mid" in observation and "underlying_mid" in data["observations"][0]:
                            data["underlying_change"] = (
                                observation["underlying_mid"] - data["observations"][0]["underlying_mid"]
                            )
                        if duration >= target and (source_at - origin).total_seconds() >= target:
                            state = "scenario_complete_unconfirmed_tariff"
                except (KeyError, ValueError, IndexError, ZeroDivisionError) as exc:
                    data["failure"] = str(exc) if isinstance(exc, ValueError) else "missing_book_or_metadata"
                    state = "entry_unobservable" if state == "await_entry" else "exit_unobservable"
                db.execute(
                    "UPDATE news_studies SET state=?,body=? WHERE id=?", (state, json.dumps(data), row["id"])
                )

    def status(self):
        with self.scanner.connect() as db:
            events = [
                json.loads(row[0])
                for row in db.execute("SELECT body FROM news_events ORDER BY first_seen DESC LIMIT 80")
            ]
            studies = [
                dict(row) | {"body": json.loads(row["body"])}
                for row in db.execute("SELECT * FROM news_studies ORDER BY rowid DESC LIMIT 120")
            ]
            counts = {
                row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM news_studies GROUP BY state")
            }
        return {
            "health": self.health,
            "events": events,
            "studies": studies,
            "counts": counts,
            "coverage": dict(Counter(group(ref) for ref in getattr(self.scanner, "all_refs", []))),
            "limitations": [
                "Реакция на получение агрегированной новости; не причинный эффект первичной публикации.",
                "Архив не используется для исторических входов.",
                "Call и put проверяются отдельно без прогноза направления; результаты нельзя складывать в стратегию.",
                "АЛОР: сценарий 1 × биржевой сбор брокеру; тариф счёта не подтверждён.",
                "Хедж и пассивная очередь не моделируются; расчёт покупки одного опциона и последующей продажи.",
                "Точное время публикации SmartLab в этой ленте отсутствует; доступны дата и наше первое наблюдение.",
            ],
        }

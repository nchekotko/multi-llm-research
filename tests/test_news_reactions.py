import json
from datetime import datetime, timedelta, timezone

import pytest

from application.news_reactions import group, matches
from application.options_scanner import MSK, OptionsScanner, OptionsSettings


def reference(asset="CHMF", underlying_type="S"):
    expiry = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
    return {
        "SECID": "TEST",
        "ASSETCODE": asset,
        "UNDERLYINGASSET": asset,
        "UNDERLYINGTYPE": underlying_type,
        "LASTTRADEDATE": expiry,
        "LASTDELDATE": expiry,
        "margin_style": "P",
        "exercise_style": "E",
        "OPTIONTYPE": "C",
        "STRIKE": 100,
        "UNDERLYINGSETTLEPRICE": 100,
        "BID": 10,
        "OFFER": 11,
        "BIDDEPTH": 2,
        "OFFERDEPTH": 2,
        "STEPPRICE": 0.01,
        "MINSTEP": 0.01,
        "BUYSELLFEE": 0.08,
        "IMBUY": 20,
    }


def feed(scanner, baseline=False, title="Северсталь: новый отчёт"):
    at = datetime.now(timezone.utc).isoformat()
    scanner.news.feed_path.parent.mkdir(parents=True, exist_ok=True)
    scanner.news.feed_path.write_text(
        json.dumps(
            {
                "exported_at": at,
                "health": {"test": "ok"},
                "items": [
                    {
                        "id": "source-1",
                        "title": title,
                        "summary": "",
                        "url": "https://example.org/1",
                        "source": "test",
                        "first_seen_at": at,
                        "baseline": baseline,
                        "publication_precision": "day",
                        "published_at": at,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def quote(bid=10, ask=11):
    at = datetime.now(timezone.utc)
    return {
        "started_at": at.isoformat(),
        "received_at": at.isoformat(),
        "payload": {
            "dataversion": {
                "columns": ["trade_date", "seqnum"],
                "data": [
                    [at.astimezone(MSK).date().isoformat(), int(at.astimezone(MSK).strftime("%Y%m%d%H%M%S"))]
                ],
            },
            "orderbook": {
                "columns": ["BUYSELL", "PRICE", "QUANTITY"],
                "data": [["B", bid, 2], ["S", ask, 2]],
            },
        },
    }


def study(scanner):
    with scanner.connect() as db:
        row = db.execute("SELECT * FROM news_studies LIMIT 1").fetchone()
        return dict(row) | {"body": json.loads(row["body"])}


@pytest.mark.parametrize(
    "asset,kind,expected",
    [
        ("CHMF", "S", "equities"),
        ("Si", "F", "currencies"),
        ("BR", "F", "commodities"),
        ("IMX", "I", "indices"),
        ("NEW", "F", "unclassified"),
    ],
)
def test_groups(asset, kind, expected):
    assert group(reference(asset, kind)) == expected


def test_entity_exclusions_and_macro_proxy():
    assert not matches({"title": "Газпромбанк объявил прибыль"}, reference("GAZR"))
    assert matches({"title": "Банк России изменил ключевую ставку"}, reference("Si", "F"))
    assert matches({"title": "Brent crude oil rises"}, reference("BR", "F"))
    assert not matches({"title": "Опасные природные явления"}, reference("NG", "F"))
    assert not matches({"title": "Банк России направил предписание аудитору"}, reference("Si", "F"))
    assert matches({"title": "Запрет импорта СПГ"}, reference("NG", "F"))
    assert not matches({"title": "МТС-банк рассматривает buyback"}, reference("MTSI"))


def test_unknown_publication_time_never_uses_parser_clock(tmp_path):
    scanner = OptionsScanner(tmp_path)
    feed(scanner)
    payload = json.loads(scanner.news.feed_path.read_text(encoding="utf-8"))
    payload["items"][0]["publication_precision"] = "unknown"
    for minute in (1, 2):
        payload["items"][0]["published_at"] = f"2026-11-19T15:0{minute}:00+00:00"
        scanner.news.feed_path.write_text(json.dumps(payload), encoding="utf-8")
        scanner.news.poll([reference()], OptionsSettings(), 0)
    events = scanner.news.status()["events"]
    assert len(events) == 1
    assert events[0]["published_at"] is None


def test_archive_never_creates_past_execution_and_dedup(tmp_path):
    scanner = OptionsScanner(tmp_path)
    feed(scanner, baseline=True)
    scanner.news.poll([reference()], OptionsSettings(), 0)
    scanner.news.poll([reference()], OptionsSettings(), 0)
    assert study(scanner)["state"] == "archive_only"
    assert not scanner.news.required_refs()
    assert len(scanner.news.status()["events"]) == 1


def test_prospective_reaction_costs_and_restart(tmp_path):
    scanner = OptionsScanner(tmp_path)
    feed(scanner)
    scanner.news.poll([reference()], OptionsSettings(horizon_seconds=30), 0)
    row = study(scanner)
    assert row["state"] == "await_entry"
    data = row["body"]
    data["ready_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with scanner.connect() as db:
        db.execute("UPDATE news_studies SET body=?", (json.dumps(data),))
    scanner.news.observe({"TEST": quote()}, "entry")
    assert study(scanner)["state"] == "await_exit"
    data = study(scanner)["body"]
    data["entry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
    with scanner.connect() as db:
        db.execute("UPDATE news_studies SET body=?", (json.dumps(data),))
    restarted = OptionsScanner(tmp_path)
    restarted.news.observe({"TEST": quote(bid=13, ask=14)}, "exit")
    row = study(restarted)
    data = row["body"]
    assert row["state"] == "scenario_complete_unconfirmed_tariff"
    assert data["gross_pnl_rub"] == 2
    assert data["exchange_fee_rub"] == 0.16
    assert data["broker_fee_scenario_rub"] == 0.16
    assert 1.65 < data["scenario_pnl_rub"] < 1.66
    assert data["baseline_missing"]
    assert data["certified_edge"] is False


def test_signal_processing_time_precedes_entry(tmp_path):
    scanner = OptionsScanner(tmp_path)
    feed(scanner)
    scanner.news.poll([reference()], OptionsSettings(), 0)
    scanner.news.observe({"TEST": quote()}, "too-soon")
    assert study(scanner)["state"] == "await_entry"

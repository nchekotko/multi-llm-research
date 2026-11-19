import pytest

from scripts.options_probe_review import fill_cost, levels


def test_multilevel_fill_accounts_for_depth():
    assert fill_cost([(10, 2), (13, 3)], 4) == 46
    with pytest.raises(ValueError, match="insufficient"):
        fill_cost([(10, 2)], 3)


def test_crossed_or_wrong_date_books_are_rejected():
    record = {
        "status": 200,
        "payload": {
            "dataversion": {"columns": ["trade_date"], "data": [["2026-11-19"]]},
            "orderbook": {"columns": ["BUYSELL", "PRICE", "QUANTITY"], "data": [["B", 12, 1], ["S", 11, 1]]},
        },
    }
    with pytest.raises(ValueError, match="crossed"):
        levels(record)
    record["payload"]["dataversion"]["data"] = [["2026-11-18"]]
    with pytest.raises(ValueError, match="wrong trading date"):
        levels(record)


def test_european_box_payoff_is_constant():
    for spot in (0, 600, 650, 700, 750, 800, 10000):
        payoff = max(spot - 650, 0) - max(spot - 750, 0) + max(750 - spot, 0) - max(650 - spot, 0)
        assert payoff == 100

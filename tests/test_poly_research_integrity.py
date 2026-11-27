"""Real-data and execution-capacity regressions for cached Poly research."""
import pandas as pd
import pytest

from scripts import cross_market_table, poly_all_markets, poly_ob_markets


def _snapshot(path, slug, outcome, start=1700000000, settlement=""):
    path.write_text(
        "collection_timestamp_utc,slug,best_ask,outcome_prices,window_start_unix,"
        "ask_depth_usdc,ask_depth_shares,implied_probability,liquidity,volume,settlement\n"
        f"2023-11-14T22:30:00Z,{slug},0.99,\"{outcome}\",{start},100,100,0.99,100,100,{settlement}\n")


def test_poly_all_excludes_missing_official_outcome_without_spot(tmp_path, monkeypatch):
    resolved = tmp_path / "resolved.csv"
    missing = tmp_path / "missing.csv"
    _snapshot(resolved, "resolved", "[1, 0]", settlement="1")
    _snapshot(missing, "missing", "[0.5, 0.5]")
    monkeypatch.setattr(poly_all_markets, "_path", lambda asset, tf: resolved)
    assert poly_all_markets.load("btc", "15m").slug.tolist() == ["resolved"]
    monkeypatch.setattr(poly_all_markets, "_path", lambda asset, tf: missing)
    assert poly_all_markets.load("btc", "15m").empty


def test_poly_ob_excludes_missing_official_outcome_without_spot(tmp_path, monkeypatch):
    resolved = tmp_path / "resolved.csv"
    _snapshot(resolved, "resolved", "[1, 0]", settlement="1")
    monkeypatch.setattr(poly_ob_markets, "_file", lambda asset, tf, kind: resolved)
    built = poly_ob_markets.build("btc", "15m")
    assert built.slug.tolist() == ["resolved"]
    assert built.up_won.tolist() == [True]


def _run_row(module, depth, price=0.99):
    row = {"slug": "m1", "t": pd.Timestamp("2026-01-01", tz="UTC"),
           "minute": 14.0, "up_won": True, "ask_depth_usdc": depth,
           "liquidity": 100.0, "volume": 100.0}
    if module is poly_ob_markets:
        row["up_price"] = price
    else:
        row["best_ask"] = price
    return pd.DataFrame([row])


@pytest.mark.parametrize("module", [poly_ob_markets, poly_all_markets])
def test_depth_caps_stake_and_uses_production_fee(module):
    result = module.run(_run_row(module, 10.0), (98, 100), (13, 15), 0.0, 0.30)
    quantity = 10.0 / 0.99
    expected = module.START + quantity * 0.01 - 0.01
    assert result["final"] == pytest.approx(expected)
    assert result["fees"] == 0.01


@pytest.mark.parametrize("module", [poly_ob_markets, poly_all_markets])
def test_missing_depth_has_per_contract_fee_but_no_equity_claim(module):
    result = module.run(_run_row(module, float("nan"), 0.50),
                        (0, 100), (13, 15), 0.0, 0.30)
    assert result["net_c"] == pytest.approx(48.0)
    assert result["final"] is None
    assert result["dd"] is None
    assert result["fees"] is None


@pytest.mark.parametrize("module", [poly_ob_markets, poly_all_markets])
def test_first_snapshot_columns_stay_coherent_when_depth_is_missing(module):
    rows = []
    for index, (price, depth) in enumerate(((0.99, float("nan")), (0.98, 100.0))):
        row = _run_row(module, depth, price).iloc[0].to_dict()
        row["t"] = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=index)
        rows.append(row)
    result = module.run(pd.DataFrame(rows), (0, 100), (13, 15), 0.0, 0.30)
    assert result["n"] == 1
    assert result["final"] is None
    assert result["fees"] is None
    assert result["net_c"] == pytest.approx(1.0)


@pytest.mark.parametrize("module", [poly_ob_markets, poly_all_markets])
def test_all_official_columns_must_agree_and_later_column_can_settle(module):
    conflict = pd.DataFrame({"slug": ["m1"], "settlement": [1], "result": [0]})
    later_only = pd.DataFrame({"slug": ["m1"], "settlement": [None], "result": [1]})
    assert module._official_outcomes(conflict).isna().all()
    assert module._official_outcomes(later_only).tolist() == [True]


def test_shared_execution_helper_preserves_depth_fees_and_official_outcomes():
    from execution_realism import evaluate_poly_quotes, official_poly_outcomes

    frame = _run_row(poly_ob_markets, 10.0)
    result = evaluate_poly_quotes(
        frame, "up_price", (98, 100), (13, 15), 0.0, 0.30,
        start=104.48, price_cap=0.995)
    quantity = 10.0 / 0.99
    assert result["final"] == pytest.approx(104.48 + quantity * 0.01 - 0.01)
    assert result["fees"] == 0.01

    conflict = pd.DataFrame({"slug": ["m1"], "settlement": [1], "result": [0]})
    later_only = pd.DataFrame({"slug": ["m1"], "settlement": [None], "result": [1]})
    assert official_poly_outcomes(conflict).isna().all()
    assert official_poly_outcomes(later_only).tolist() == [True]


def test_cross_market_table_uses_poly_fee_and_depth(monkeypatch):
    frame = pd.concat([_run_row(poly_ob_markets, 100.0, 0.99)] * 5, ignore_index=True)
    frame["slug"] = [f"m{index}" for index in range(5)]
    monkeypatch.setattr(cross_market_table.P, "build", lambda asset, tf: frame)
    rows = cross_market_table.poly_rows("btc")
    assert len(rows) == 1
    for row in rows:
        assert row["fees"] == pytest.approx(0.05)
        assert row["cap"] == 100.0
        assert row["final"] == pytest.approx(105.21976477505322)

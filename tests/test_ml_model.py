"""Tests for the tiny torch fair-value model (ml_model.py)."""

import ml_model as m

# Skip (not error) on a torch-less checkout: the missing package is optional.
import pytest
pytest.importorskip("torch")


def _row(strike, spot=100000.0, settle=100500.0, kalshi=None):
    """A rung row labelled by whether settlement cleared the strike."""
    if kalshi is None:
        kalshi = 0.8 if strike < settle else 0.2
    return {
        "strike": strike, "spot": spot, "sigma": 0.0007,
        "kalshi": kalshi, "poly": kalshi, "spread": 0.01,
        "fair": kalshi, "minutes_left": 30.0,
        "settled_yes": 1.0 if settle > strike else 0.0,
    }


def test_row_to_features_has_six_values():
    row = _row(100000)
    f = m.row_to_features(row, 100000.0, 0.0007, 30.0)
    assert len(f) == 6
    # moneyness log(spot/strike) = log(1) = 0 at the money
    assert f[0] == 0.0


def test_train_learns_itm_higher_than_otm():
    rows = [_row(90000), _row(100000), _row(110000)]
    model = m.train_labeled(rows, epochs=2000, lr=0.05)
    p_itm = model.predict_proba(rows[0], 100000.0, 0.0007, 30.0)
    p_otm = model.predict_proba(rows[2], 100000.0, 0.0007, 30.0)
    assert 0.0 <= p_itm <= 1.0 and 0.0 <= p_otm <= 1.0
    assert p_itm > p_otm


def test_single_class_raises():
    rows = [_row(90000), _row(95000)]  # both settled_yes=1
    try:
        m.train_labeled(rows, epochs=10, lr=0.05)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for single-class data")


def test_save_load_roundtrip(tmp_path):
    rows = [_row(90000), _row(110000)]
    model = m.train_labeled(rows, epochs=2000, lr=0.05)
    before = model.predict_proba(rows[0], 100000.0, 0.0007, 30.0)
    path = tmp_path / "ml_test.pt"
    model.save(str(path))
    loaded = m.MLFairValue.load(str(path))
    after = loaded.predict_proba(rows[0], 100000.0, 0.0007, 30.0)
    assert abs(after - before) < 1e-6

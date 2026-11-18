"""D1: Polymarket settlement writes to local-first DB.

Tests that apply_poly_settlement atomically writes the idempotent pnl_ledger
row and bot_state snapshot in one SQLite transaction, that duplicate calls
don't double-credit, and that the D1 column migration is idempotent.
"""
import local_db


def _db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)


def test_apply_poly_settlement_idempotent_once_only(tmp_path):
    """Settlement writes exactly one ledger row even if called twice."""
    db = _db(tmp_path)
    state = {"cash": 35.0, "positions": [], "realized_pnl": 0.0}

    first = db.apply_poly_settlement(
        "poly_settle:slug-a:order-1:1:0.95", "slug-a", 2.50, state,
        venue="polymarket_us", mode="dry",
    )
    assert first is True, f"expected True on first apply, got {first!r}"

    second = db.apply_poly_settlement(
        "poly_settle:slug-a:order-1:1:0.95", "slug-a", 2.50, state,
        venue="polymarket_us", mode="dry",
    )
    assert second is False, f"expected False on duplicate, got {second!r}"

    # Exactly one row in pnl_ledger.
    conn = db._get_conn()  # noqa: SLF001 - test reaches the same handle
    rows = conn.execute(
        "SELECT COUNT(*) FROM pnl_ledger WHERE idempotency_key = ?",
        ("poly_settle:slug-a:order-1:1:0.95",),
    ).fetchone()
    assert rows[0] == 1, f"expected 1 ledger row, got {rows[0]}"


def test_apply_poly_settlement_returns_none_on_error(tmp_path, monkeypatch):
    """apply_poly_settlement returns None when the DB write fails."""
    db = _db(tmp_path)
    state = {"cash": 35.0, "positions": []}

    # Force an error by closing the connection so writes fail.
    db._get_conn().close()  # noqa: SLF001
    result = db.apply_poly_settlement(
        "poly_settle:slug-b:order-2:2:0.80", "slug-b", -1.00, state,
        venue="polymarket_us", mode="dry",
    )
    assert result is None, f"expected None on DB error, got {result!r}"


def test_apply_poly_settlement_stores_venue_mode_origin(tmp_path):
    """Ledger row carries venue/mode/origin_host for venue separation."""
    db = _db(tmp_path)
    state = {"cash": 35.0, "positions": []}
    db.apply_poly_settlement(
        "poly_settle:slug-c:order-3:1:0.90", "slug-c", 1.00, state,
        venue="polymarket_us", mode="live",
    )
    conn = db._get_conn()  # noqa: SLF001
    row = conn.execute(
        "SELECT venue, mode, origin_host FROM pnl_ledger "
        "WHERE idempotency_key = ?",
        ("poly_settle:slug-c:order-3:1:0.90",),
    ).fetchone()
    assert row is not None
    assert row["venue"] == "polymarket_us"
    assert row["mode"] == "live"
    assert row["origin_host"] and row["origin_host"] != ""


def test_d1_columns_migration_is_idempotent(tmp_path):
    """ALTER TABLE migration doesn't fail when columns already exist."""
    db = _db(tmp_path)
    # Run initialization twice — second must not raise.
    db2 = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)
    # Both instances should work and expose the new columns.
    conn = db2._get_conn()  # noqa: SLF001
    for table in ("signals", "orders", "bot_state", "pnl_ledger", "backtest_runs"):
        cols = [r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()]
        for col in ("venue", "mode", "origin_host", "schema_version"):
            assert col in cols, f"{table} missing column {col}; have {cols}"


def test_apply_settlement_pnl_stores_venue_mode_origin(tmp_path):
    """Legacy apply_settlement_pnl also accepts venue/mode/origin_host."""
    db = _db(tmp_path)
    db.apply_settlement_pnl(
        "settle:legacy:1", "KXBTC15M", 0.50, fee_dollars=0.05,
        source="local", venue="kalshi", mode="dry",
    )
    conn = db._get_conn()  # noqa: SLF001
    row = conn.execute(
        "SELECT venue, mode, origin_host FROM pnl_ledger WHERE idempotency_key = ?",
        ("settle:legacy:1",),
    ).fetchone()
    assert row is not None
    assert row["venue"] == "kalshi"
    assert row["mode"] == "dry"


def test_get_total_realized_pnl_filters_by_venue(tmp_path):
    """Both traders share one DB: Kalshi's total must not include Poly's rows."""
    db = _db(tmp_path)
    db.apply_settlement_pnl("kalshi-1", "KXBTC15M-X", 1.25, venue="", mode="dry")
    db.apply_poly_settlement(
        "poly_settle:slug-b:order-2:1", "slug-b", 4.00, {"cash": 35.0},
        venue="polymarket_us", mode="dry",
    )
    # Default (Kalshi) view excludes Poly.
    assert db.get_total_realized_pnl() == 1.25
    assert db.get_total_realized_pnl(venue="polymarket_us") == 4.00

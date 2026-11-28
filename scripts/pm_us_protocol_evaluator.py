"""Canonical dry-research PM US protocol evaluator; never a live order path."""
import hashlib
import json
import math
from datetime import date
from pathlib import Path

import execution_realism


MANIFEST_PATH = Path(__file__).resolve().parent.parent / "config" / "pm_us_protocol_evaluator_v1.json"
EVALUATOR_PATH = Path(__file__).resolve()


def _load_manifest(path=None):
    try:
        manifest = json.loads(Path(path or MANIFEST_PATH).read_text())
        expected = {
            "start": "2026-11-13T00:00:00Z",
            "end": "2026-11-27T00:00:00Z",
            "mode": "dry_research",
        }
        inference = {
            "seed": 17008, "repetitions": 10000,
            "minimum_markets": 300, "minimum_utc_days": 14,
        }
        if manifest["schema_version"] != 2 or manifest["utc_scope"] != expected:
            return None
        evidence = manifest["required_evidence"]
        if evidence["fill_provenance"] != "book_simulated" or evidence["real_fill"] is not False:
            return None
        if manifest["inference"] != inference or manifest["study"]["exact_m"] != 100:
            return None
        if manifest["evaluator"]["path"] != "scripts/pm_us_protocol_evaluator.py":
            return None
        digest = hashlib.sha256(EVALUATOR_PATH.read_bytes()).hexdigest()
        if manifest["evaluator"]["sha256"] != digest:
            return None
        promotion = manifest["promotion"]
        if (promotion["human_versioned_evaluation_required"] is not False
                or promotion["technical_gates_required"] is not True
                or promotion["live_orders"] != "separately_human_gated"
                or promotion["positive_result_implied"] is not False):
            return None
        return manifest
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _eligible_evidence(row, start, end):
    try:
        if row["venue"] != "POLYMARKET_US" or row["asset"] != "BTC":
            return False
        if row["fill_provenance"] != "book_simulated" or row["real_fill"] is not False:
            return False
        if row["outcome_provenance"] != "official_settlement":
            return False
        if row["fee_provenance"] != "production" or row["depth_provenance"] != "official_book":
            return False
        outcome = float(row["official_outcome"])
        fee = float(row["production_fee_dollars"])
        depth = float(row["real_depth"])
        quantity = float(row["quantity"])
        venue_ts = float(row["venue_ts"])
        arrival_ts = float(row["order_arrival_ts"])
        entry_ts = float(row["entry_ts"])
        entry_price = float(row["entry_price"])
        settled_day = date.fromisoformat(row["settled_day"])
        if outcome not in (0.0, 1.0) or not start <= settled_day < end:
            return False
        values = (fee, depth, quantity, venue_ts, arrival_ts, entry_ts, entry_price)
        if not all(math.isfinite(value) for value in values):
            return False
        if fee < 0 or depth <= 0 or quantity <= 0 or depth < quantity:
            return False
        return (0.0 < entry_price < 1.0 and 0.0 < entry_ts <= venue_ts
                and arrival_ts >= venue_ts)
    except (KeyError, TypeError, ValueError):
        return False


def evaluate_rows(rows, *, manifest_path=None):
    """Return strict clustered bound, or None before every eligibility gate passes."""
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        return None
    start = date.fromisoformat(manifest["utc_scope"]["start"][:10])
    end = date.fromisoformat(manifest["utc_scope"]["end"][:10])
    if any(not _eligible_evidence(row, start, end) for row in rows):
        return None
    return execution_realism.protocol_clustered_lower_bound(
        rows, exact_m=manifest["study"]["exact_m"])

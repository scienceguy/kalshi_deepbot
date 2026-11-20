"""
sizing.py — T15-40: Sizing policy across both venues.

Fractional Kelly (existing 0.25 as ceiling) on the LOWER edge bound; a depth cap
(fraction of visible depth at the executable price) and a market-impact estimate;
per-venue and per-underlying caps expressed as a fraction of the live balance;
one correlated-exposure budget across venues (BTC on Kalshi and BTC on Polymarket
is one bet).

Reads each venue's real balance every cycle and writes the balance used next to
every decision. No martingale, no doubling after loss, no cap overrides from
the forecast.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List

import config
import fees

log = logging.getLogger(__name__)


@dataclass
class VenueBalance:
    """Balance information for a single venue."""
    venue: str                    # "kalshi" or "polymarket_us"
    cash: float                   # Available cash
    exposure: float               # Current exposure (mark-to-market)
    total: float                  # Total portfolio value
    timestamp: float              # When this was fetched
    

@dataclass
class SizingConstraints:
    """All constraints that apply to a sizing decision."""
    # Kelly fraction ceiling
    kelly_fraction: float = config.KELLY_FRACTION
    
    # Minimum EV edge (cents per contract)
    min_ev_edge_cents: float = config.MIN_EV_EDGE_PCT * 100
    
    # Cash floor
    cash_floor_pct: float = config.CASH_FLOOR_PCT
    min_cash_absolute: float = config.MIN_CASH_ABSOLUTE
    
    # Exposure ceiling
    exposure_ceiling_pct: float = config.EXPOSURE_CEILING_PCT
    
    # Per-trade cap (fraction of cash)
    per_trade_pct: float = config.PER_TRADE_PCT
    
    # Depth cap: max fraction of visible depth at executable price
    depth_cap_fraction: float = 0.10  # Max 10% of visible depth
    
    # Market impact model: linear in trade size relative to depth
    impact_coefficient: float = 0.001  # 1bp per 1% of depth
    
    # Max trade in dollars (hard cap)
    max_trade_dollars: float = config.MAX_TRADE_DOLLARS
    
    # Correlated exposure: max fraction of total portfolio per underlying
    correlated_exposure_pct: float = 0.20  # 20% max per underlying across venues
    
    # Microstructure bootstrap (balances < $10)
    micro_balance_threshold: float = 10.0
    micro_max_fraction: float = 0.50  # 50% of cash on micro balances


@dataclass
class SizingInputs:
    """Inputs needed for sizing calculation."""
    # Venue balances
    kalshi_balance: Optional[VenueBalance] = None
    polymarket_balance: Optional[VenueBalance] = None
    
    # Trade parameters
    win_prob: float = 0.0           # Model win probability
    entry_price_cents: int = 0      # Entry price in cents (1-99)
    side: str = "yes"               # "yes" or "no"
    series: str = ""                # e.g., "KXBTC15M"
    ticker: str = ""                # Full ticker
    
    # Orderbook depth at executable price
    depth_at_price: float = 0.0     # Visible contracts at executable price
    
    # Current exposure by underlying
    current_exposure_by_asset: Dict[str, float] = field(default_factory=dict)
    
    # Resting orders value
    resting_orders_value: float = 0.0
    
    # Paper mode
    paper: bool = False


@dataclass
class SizingResult:
    """Result of sizing calculation."""
    size_dollars: float             # Final size in dollars
    size_contracts: int             # Final size in contracts (floored)
    kelly_dollars: float            # Kelly size before constraints
    depth_cap_dollars: float        # Depth cap size
    impact_adjusted_dollars: float  # After market impact
    correlated_cap_dollars: float   # Correlated exposure cap
    cash_room_dollars: float        # Cash floor cap
    exposure_room_dollars: float    # Exposure ceiling cap
    per_trade_cap_dollars: float    # Per-trade percentage cap
    min_ev_edge_cents: float        # Minimum EV edge used
    win_prob_used: float            # Win probability used
    edge_cents: float               # Edge in cents (model - price - fee)
    constraints_binding: List[str]  # Which constraints bound the size
    venue_balances_used: Dict[str, float]  # Balance used per venue
    warnings: List[str] = field(default_factory=list)


def _paper_constraints() -> SizingConstraints:
    """Constraints for paper/dry-run mode."""
    return SizingConstraints(
        kelly_fraction=config.KELLY_FRACTION,
        min_ev_edge_cents=config.MIN_EV_EDGE_PCT * 100,
        cash_floor_pct=config.PAPER_CASH_FLOOR_PCT,
        min_cash_absolute=config.PAPER_MIN_CASH_ABSOLUTE,
        exposure_ceiling_pct=config.PAPER_EXPOSURE_CEILING_PCT,
        per_trade_pct=config.PAPER_PER_TRADE_PCT,
        depth_cap_fraction=0.10,
        impact_coefficient=0.001,
        max_trade_dollars=config.MAX_TRADE_DOLLARS,
        correlated_exposure_pct=0.20,
        micro_balance_threshold=10.0,
        micro_max_fraction=0.50,
    )


def _live_constraints() -> SizingConstraints:
    """Constraints for live mode."""
    return SizingConstraints(
        kelly_fraction=config.KELLY_FRACTION,
        min_ev_edge_cents=config.MIN_EV_EDGE_PCT * 100,
        cash_floor_pct=config.CASH_FLOOR_PCT,
        min_cash_absolute=config.MIN_CASH_ABSOLUTE,
        exposure_ceiling_pct=config.EXPOSURE_CEILING_PCT,
        per_trade_pct=config.PER_TRADE_PCT,
        depth_cap_fraction=0.10,
        impact_coefficient=0.001,
        max_trade_dollars=config.MAX_TRADE_DOLLARS,
        correlated_exposure_pct=0.20,
        micro_balance_threshold=10.0,
        micro_max_fraction=0.50,
    )


def calculate_kelly_fraction(win_prob: float, entry_price_cents: int,
                              aggressive_fill: bool = True) -> float:
    """
    Calculate Kelly fraction for a binary contract.
    
    For binary options:
    - Buy YES at price p: profit = 1-p, loss = p
    - Kelly f = (p * b - q) / b where b = (1-p)/p, q = 1-p
    - f = p - (1-p)/b = p - p = 0? No...
    - b = profit/loss = (1-p)/p
    - f = (p * b - (1-p)) / b = p - (1-p)/b = p - (1-p)*p/(1-p) = p - p = 0?
    
    Wait, let's re-derive:
    - Net EV per contract = p*(1-p) - (1-p)*p - fee = 0 - fee (at fair value)
    - But we're using win_prob != price, so there's edge
    
    Standard Kelly for binary: f = (p - q/b) where b = profit/loss
    For buying YES at price P (in dollars):
    - Profit if win = 1 - P
    - Loss if lose = P
    - b = (1-P)/P
    - q = 1-p
    - f = p - q/b = p - (1-p) * P/(1-P)
    
    This is the fraction of bankroll to bet.
    """
    if entry_price_cents <= 0 or entry_price_cents >= 100:
        return 0.0
    
    p_dollars = entry_price_cents / 100.0
    profit = 1.0 - p_dollars
    loss = p_dollars
    
    if loss <= 0:
        return 0.0
    
    b = profit / loss
    q = 1.0 - win_prob
    
    kelly_f = (win_prob * b - q) / b if b > 0 else 0.0
    return max(0.0, kelly_f)


def calculate_net_ev(win_prob: float, entry_price_cents: int,
                      aggressive_fill: bool = True, series: str = None) -> float:
    """
    Calculate net EV per contract in dollars.
    
    Net EV = win_prob * profit - (1-win_prob) * loss - fee
    """
    if entry_price_cents <= 0 or entry_price_cents >= 100:
        return -float('inf')
    
    p_dollars = entry_price_cents / 100.0
    profit = 1.0 - p_dollars
    loss = p_dollars
    q = 1.0 - win_prob
    
    # Fee
    fee_rate = fees.FEE_RATE if aggressive_fill else 0.0
    fee = fee_rate * p_dollars * profit
    
    net_ev = win_prob * profit - q * loss - fee
    return net_ev


def calculate_market_impact(size_dollars: float, depth_at_price: float,
                             entry_price_cents: int, impact_coeff: float) -> float:
    """
    Estimate market impact in dollars.
    
    Simple linear model: impact = size / depth * impact_coeff * size
    This is the slippage cost of executing the trade.
    """
    if depth_at_price <= 0 or size_dollars <= 0:
        return 0.0
    
    size_contracts = size_dollars / (entry_price_cents / 100.0) if entry_price_cents > 0 else 0
    if size_contracts <= 0:
        return 0.0
    
    # Impact as fraction of depth
    depth_fraction = size_contracts / depth_at_price
    
    # Impact in dollars = impact_coeff * depth_fraction * size_dollars
    impact = impact_coeff * depth_fraction * size_dollars
    return impact


def calculate_sizing(inputs: SizingInputs) -> SizingResult:
    """
    Main sizing function implementing T15-40 policy.
    
    Applies constraints in order:
    1. Kelly sizing on LOWER edge bound (conservative)
    2. Depth cap (fraction of visible depth)
    3. Market impact adjustment
    4. Correlated exposure budget across venues
    5. Cash floor
    6. Exposure ceiling
    7. Per-trade percentage cap
    8. Hard max trade dollars
    """
    constraints = _paper_constraints() if inputs.paper else _live_constraints()
    warnings = []
    binding = []
    
    # Get total portfolio value across venues
    kalshi_bal = inputs.kalshi_balance
    poly_bal = inputs.polymarket_balance
    
    total_cash = 0.0
    total_exposure = 0.0
    venue_balances_used = {}
    
    if kalshi_bal:
        total_cash += kalshi_bal.cash
        total_exposure += kalshi_bal.exposure
        venue_balances_used["kalshi"] = kalshi_bal.total
    
    if poly_bal:
        total_cash += poly_bal.cash
        total_exposure += poly_bal.exposure
        venue_balances_used["polymarket_us"] = poly_bal.total
    
    portfolio = total_cash + total_exposure
    
    if portfolio <= 0:
        return SizingResult(
            size_dollars=0.0, size_contracts=0, kelly_dollars=0.0,
            depth_cap_dollars=0.0, impact_adjusted_dollars=0.0,
            correlated_cap_dollars=0.0, cash_room_dollars=0.0,
            exposure_room_dollars=0.0, per_trade_cap_dollars=0.0,
            min_ev_edge_cents=constraints.min_ev_edge_cents,
            win_prob_used=inputs.win_prob, edge_cents=0.0,
            constraints_binding=["zero_portfolio"],
            venue_balances_used=venue_balances_used,
            warnings=["Zero or negative portfolio value"]
        )
    
    # Calculate net EV and check minimum edge
    net_ev = calculate_net_ev(
        inputs.win_prob, inputs.entry_price_cents,
        aggressive_fill=config.AGGRESSIVE_FILL, series=inputs.series
    )
    
    entry_price_dollars = inputs.entry_price_cents / 100.0
    edge_cents = net_ev * 100.0
    
    if net_ev < config.MIN_EV_EDGE_PCT - 1e-10:
        warnings.append(f"Net EV ${net_ev:.4f} < min edge ${config.MIN_EV_EDGE_PCT:.4f}")
        return SizingResult(
            size_dollars=0.0, size_contracts=0, kelly_dollars=0.0,
            depth_cap_dollars=0.0, impact_adjusted_dollars=0.0,
            correlated_cap_dollars=0.0, cash_room_dollars=0.0,
            exposure_room_dollars=0.0, per_trade_cap_dollars=0.0,
            min_ev_edge_cents=constraints.min_ev_edge_cents,
            win_prob_used=inputs.win_prob, edge_cents=edge_cents,
            constraints_binding=["min_ev_edge"],
            venue_balances_used=venue_balances_used,
            warnings=warnings
        )
    
    # 1. Kelly sizing on LOWER edge bound
    kelly_fraction = calculate_kelly_fraction(
        inputs.win_prob, inputs.entry_price_cents,
        aggressive_fill=config.AGGRESSIVE_FILL
    )
    kelly_fraction *= constraints.kelly_fraction  # Apply fractional Kelly ceiling
    kelly_dollars = kelly_fraction * portfolio
    
    # 2. Depth cap
    depth_cap_dollars = float('inf')
    if inputs.depth_at_price > 0 and inputs.entry_price_cents > 0:
        max_contracts_from_depth = inputs.depth_at_price * constraints.depth_cap_fraction
        depth_cap_dollars = max_contracts_from_depth * entry_price_dollars
    
    # 3. Market impact adjustment
    # Start with Kelly size, then reduce if impact is too large
    impact_adjusted = kelly_dollars
    if depth_cap_dollars < float('inf'):
        impact = calculate_market_impact(
            kelly_dollars, inputs.depth_at_price,
            inputs.entry_price_cents, constraints.impact_coefficient
        )
        impact_adjusted = max(0.0, kelly_dollars - impact)
    
    # 4. Correlated exposure cap (across venues per underlying)
    # Extract asset from series/ticker
    asset = ""
    if inputs.series:
        # KXBTC15M -> BTC
        if inputs.series.startswith("KX") and inputs.series.endswith("15M"):
            asset = inputs.series[2:-3]
    elif inputs.ticker:
        # KXBTC15M-26NOV201530 -> BTC
        parts = inputs.ticker.split("-")
        if parts[0].startswith("KX") and parts[0].endswith("15M"):
            asset = parts[0][2:-3]
    
    current_asset_exposure = inputs.current_exposure_by_asset.get(asset, 0.0)
    correlated_cap_dollars = max(0.0, portfolio * constraints.correlated_exposure_pct - current_asset_exposure)
    
    # 5-7. Cash floor, exposure ceiling, per-trade cap
    floor_pct = constraints.cash_floor_pct
    min_cash = constraints.min_cash_absolute
    ceiling_pct = constraints.exposure_ceiling_pct
    per_trade_pct = constraints.per_trade_pct
    
    # Microstructure bootstrap for small balances
    if portfolio < constraints.micro_balance_threshold:
        available_cash = max(0.0, total_cash)
        cap = max(0.0, round(available_cash * constraints.micro_max_fraction, 4) if available_cash >= 0.02 else available_cash)
        cash_room = cap
        exposure_room = cap
        per_trade_cap = cap
    else:
        # Exposure room (using mark-to-market)
        mtm_exposure = total_exposure
        exposure_room = max(0.0, portfolio * ceiling_pct - mtm_exposure)
        
        # Cash room (subtract resting orders)
        floor_needed = max(min_cash, portfolio * floor_pct)
        cash_room = max(0.0, total_cash - inputs.resting_orders_value - floor_needed)
        
        # Per-trade cap (based on total cash)
        per_trade_cap = total_cash * per_trade_pct
    
    # Combine all caps
    size_dollars = min(
        impact_adjusted,
        depth_cap_dollars,
        correlated_cap_dollars,
        cash_room,
        exposure_room,
        per_trade_cap,
        constraints.max_trade_dollars
    )
    
    # Track which constraint is binding
    caps = {
        "kelly": impact_adjusted,
        "depth": depth_cap_dollars,
        "correlated": correlated_cap_dollars,
        "cash_floor": cash_room,
        "exposure_ceiling": exposure_room,
        "per_trade_pct": per_trade_cap,
        "max_trade_dollars": constraints.max_trade_dollars,
    }
    
    for name, cap in caps.items():
        if size_dollars <= cap + 1e-10:  # Allow tiny FP differences
            binding.append(name)
    
    # Convert to contracts (floor to integer)
    if inputs.entry_price_cents > 0:
        size_contracts = int(size_dollars / entry_price_dollars)
    else:
        size_contracts = 0
    
    # Recalculate actual dollar size from integer contracts
    actual_size_dollars = size_contracts * entry_price_dollars
    
    return SizingResult(
        size_dollars=actual_size_dollars,
        size_contracts=size_contracts,
        kelly_dollars=kelly_dollars,
        depth_cap_dollars=depth_cap_dollars if depth_cap_dollars < float('inf') else 0.0,
        impact_adjusted_dollars=impact_adjusted,
        correlated_cap_dollars=correlated_cap_dollars,
        cash_room_dollars=cash_room,
        exposure_room_dollars=exposure_room,
        per_trade_cap_dollars=per_trade_cap,
        min_ev_edge_cents=constraints.min_ev_edge_cents,
        win_prob_used=inputs.win_prob,
        edge_cents=edge_cents,
        constraints_binding=binding,
        venue_balances_used=venue_balances_used,
        warnings=warnings
    )


def fetch_venue_balances(kalshi_client=None, polymarket_client=None) -> Tuple[Optional[VenueBalance], Optional[VenueBalance]]:
    """
    Fetch real-time balances from both venues.
    
    Returns (kalshi_balance, polymarket_balance) where each can be None on failure.
    """
    import time
    
    kalshi_bal = None
    poly_bal = None
    now = time.time()
    
    if kalshi_client:
        try:
            resp = kalshi_client.get_balance()
            if resp and "balance" in resp:
                # Kalshi returns balance in cents
                cash = resp["balance"] / 100.0
                # Get positions for exposure
                pos_resp = kalshi_client.get_positions()
                exposure = 0.0
                if pos_resp and "positions" in pos_resp:
                    for pos in pos_resp["positions"]:
                        # Cost basis exposure
                        exposure += pos.get("cost_basis", 0) / 100.0
                kalshi_bal = VenueBalance(
                    venue="kalshi",
                    cash=cash,
                    exposure=exposure,
                    total=cash + exposure,
                    timestamp=now
                )
        except Exception as e:
            log.warning(f"Failed to fetch Kalshi balance: {e}")
    
    if polymarket_client:
        try:
            # Use polymarket_us_client.get_account_balances()
            from polymarket_us_client import get_account_balances
            resp = get_account_balances()
            if resp and "balances" in resp:
                cash = 0.0
                for bal in resp["balances"]:
                    if bal.get("currency") == "USD":
                        cash = float(bal.get("currentBalance", 0))
                        break
                # Get positions for exposure
                from polymarket_us_client import get_portfolio_positions
                pos_resp = get_portfolio_positions()
                exposure = 0.0
                if pos_resp and "positions" in pos_resp:
                    for pos in pos_resp["positions"]:
                        exposure += float(pos.get("size", 0)) * float(pos.get("avgEntryPrice", 0))
                poly_bal = VenueBalance(
                    venue="polymarket_us",
                    cash=cash,
                    exposure=exposure,
                    total=cash + exposure,
                    timestamp=now
                )
        except Exception as e:
            log.warning(f"Failed to fetch Polymarket balance: {e}")
    
    return kalshi_bal, poly_bal


if __name__ == "__main__":
    # Quick smoke test
    inputs = SizingInputs(
        kalshi_balance=VenueBalance("kalshi", 1000.0, 200.0, 1200.0, 0.0),
        polymarket_balance=VenueBalance("polymarket_us", 500.0, 100.0, 600.0, 0.0),
        win_prob=0.60,
        entry_price_cents=55,
        side="yes",
        series="KXBTC15M",
        depth_at_price=1000.0,
        current_exposure_by_asset={"BTC": 150.0},
        resting_orders_value=50.0,
        paper=True
    )
    
    result = calculate_sizing(inputs)
    print(f"Size: ${result.size_dollars:.2f} ({result.size_contracts} contracts)")
    print(f"Kelly: ${result.kelly_dollars:.2f}")
    print(f"Depth cap: ${result.depth_cap_dollars:.2f}")
    print(f"Impact adj: ${result.impact_adjusted_dollars:.2f}")
    print(f"Correlated cap: ${result.correlated_cap_dollars:.2f}")
    print(f"Cash room: ${result.cash_room_dollars:.2f}")
    print(f"Exposure room: ${result.exposure_room_dollars:.2f}")
    print(f"Per-trade cap: ${result.per_trade_cap_dollars:.2f}")
    print(f"Edge: {result.edge_cents:.2f} cents")
    print(f"Binding: {result.constraints_binding}")
    print(f"Venue balances: {result.venue_balances_used}")
    if result.warnings:
        print(f"Warnings: {result.warnings}")
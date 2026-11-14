#!/usr/bin/env python3
"""Polymarket US portfolio/balance reporter — READ-ONLY.

Subcommands:
  positions   - print open positions
  balances    - print account balances
  all         - both of the above
  market --slug SLUG  - print market snapshot by slug

Note: This script is READ-ONLY. No orders are placed.
"""

import argparse
import sys


def _print_banner():
    """Print the read-only banner."""
    print("READ-ONLY — no orders are placed.")


def _print_positions(client):
    """Print open positions from the Polymarket US portfolio."""
    try:
        result = client.get_portfolio_positions()
        # Normalize to a dict with a 'positions' key (list)
        if isinstance(result, dict):
            pos_list = result.get("positions", result.get("data", result.get("result", [])))
        else:
            # If it's already a list or something else, wrap it
            pos_list = result if isinstance(result, list) else []

        if not pos_list:
            print("no open positions")
            return

        for pos in pos_list:
            # Gracefully extract fields — different SDK versions may differ
            market_slug = pos.get("market_slug", pos.get("marketId", pos.get("slug", "unknown")))
            side = pos.get("side", pos.get("outcome", pos.get("outcome", "unknown")))
            contracts = pos.get("contracts", pos.get("size", pos.get("position", 0)))
            # Ensure contracts is int/float for display
            try:
                contracts = int(contracts)
            except (TypeError, ValueError):
                contracts = 0
            avg_price = pos.get("avg_price", pos.get("entry_price", pos.get("price", 0.0)))
            try:
                avg_price = float(avg_price)
            except (TypeError, ValueError):
                avg_price = 0.0

            # Current price / PnL: try to derive from position data
            current_price = pos.get("current_price", pos.get("mid_price", pos.get("price", None)))
            pnl = pos.get("pnl", pos.get("unrealized_pnl", None))

            # If current_price is available and side is known, compute PnL
            if current_price is not None:
                try:
                    current_price = float(current_price)
                except (TypeError, ValueError):
                    current_price = None
            else:
                current_price = "N/A"

            side_str = str(side).strip()
            contract_str = str(contracts).strip()
            price_str = str(current_price) if current_price not in (None, "N/A") else "N/A"
            pnl_str = str(pnl) if pnl not in (None, "") else "N/A"

            print(
                f"  {market_slug} | {side_str} | {contract_str} | "
                f"{avg_price} | {price_str} | {pnl_str}"
            )
    except Exception as e:
        print(f"Error fetching positions: {e}", file=sys.stderr)


def _print_balances(client):
    """Print account balances from the Polymarket US account."""
    try:
        balances = client.get_account_balances()
        # Normalize to a list of balance dicts
        bal_list = balances.get("balances", balances.get("data", balances.get("result", [])))
        if not bal_list:
            print("no balance data")
            return

        for bal in bal_list:
            currency = bal.get("currency", "unknown")
            # Official schema field is currentBalance; buyingPower (capital P).
            available = bal.get("currentBalance", 0.0)
            buying_power = bal.get("buyingPower", None)

            try:
                available = float(available)
            except (TypeError, ValueError):
                available = 0.0

            parts = [f"  {currency} | available: {available}"]
            if buying_power is not None:
                try:
                    buying_power = float(buying_power)
                except (TypeError, ValueError):
                    buying_power = None
                if buying_power is not None:
                    parts.append(f"buying power: {buying_power}")
            print("".join(parts))
    except Exception as e:
        print(f"Error fetching balances: {e}", file=sys.stderr)


def _print_market(client, slug):
    """Print the US production market snapshot for a given slug."""
    try:
        market = client.get_market_by_slug(slug)
        question = market.get("question", market.get("title", "unknown"))
        status = market.get("status", market.get("marketStatus", "unknown"))

        print(f"  question: {question}")
        print(f"  status: {status}")

        # Print market sides with prices
        sides = market.get("market_sides", market.get("sides", market.get("outcomes", [])))
        if sides:
            for side_data in sides:
                side_name = side_data.get("outcome", side_data.get("side", side_data.get("label", "unknown")))
                price = side_data.get("price", side_data.get("probability", side_data.get("cost", "unknown")))
                print(f"  {side_name}: {price}")

        # Best bid/ask
        best_bid = market.get("best_bid", market.get("bestBid", None))
        best_ask = market.get("best_ask", market.get("bestAsk", None))
        if best_bid is not None:
            print(f"  best bid: {best_bid}")
        if best_ask is not None:
            print(f"  best ask: {best_ask}")
    except Exception as e:
        print(f"Error fetching market {slug}: {e}", file=sys.stderr)


def main(argv=None):
    _print_banner()

    parser = argparse.ArgumentParser(
        description="Polymarket US portfolio/balance reporter (READ-ONLY)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # positions subcommand
    subparsers.add_parser("positions", help="Print open positions")

    # balances subcommand
    subparsers.add_parser("balances", help="Print account balances")

    # all subcommand
    subparsers.add_parser("all", help="Print both positions and balances")

    # market subcommand
    market_parser = subparsers.add_parser("market", help="Print market snapshot by slug")
    market_parser.add_argument("--slug", required=True, help="Market slug")

    # Use provided argv or fall back to sys.argv
    if argv is None:
        argv = sys.argv[1:]
    # Reconstruct argv as if from command line
    # Prepend the script name for argparse
    full_argv = ["polymarket_portfolio.py"] + argv if argv else sys.argv

    args = parser.parse_args(full_argv[1:])  # skip script name

    if not args.command:
        parser.print_help()
        return 1

    # --- Lazy import of polymarket_us_client ---
    # This allows the script to be imported as a module without triggering
    # the dependency; the import happens at runtime inside main().
    try:
        import polymarket_us_client  # type: ignore
    except ImportError as exc:
        print(
            f"ERROR: Could not import polymarket_us_client module: {exc}",
            file=sys.stderr,
        )
        print(
            "Ensure the polymarket_us_client module is installed or available on sys.modules.",
            file=sys.stderr,
        )
        return 1

    client = polymarket_us_client

    try:
        if args.command == "positions":
            _print_positions(client)
        elif args.command == "balances":
            _print_balances(client)
        elif args.command == "all":
            _print_positions(client)
            _print_balances(client)
        elif args.command == "market":
            _print_market(client, args.slug)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
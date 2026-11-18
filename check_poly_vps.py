import os
os.environ['BW_DIR']=os.path.expanduser('~/.secrets/bw')
from polymarket_us_client import get_account_balances, usd_cash_balance
d = get_account_balances()
print("VPS Polymarket usd_cash=", usd_cash_balance(d))

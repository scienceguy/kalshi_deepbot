import json
d=json.load(open("paper_state.json"))
print("start:",d["start_balance"])
print("pnl:",d["pnl"])
print("settlement_pnl:",d["settlement_pnl"])
print("exit_pnl:",d["exit_pnl"])
print("ledger entries:",len(d["ledger"]))
for e in d["ledger"][-4:]:
    print("  ",e)

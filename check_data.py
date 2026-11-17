# Checks the data files the site needs. Run from the repo root. Exit code 1 = something is wrong.
import json, os, sys
bad = []
def load(p):
    try: return json.load(open(p))
    except Exception as e: bad.append(f"{p}: cannot read ({e})"); return None
s = load("data/summary.json")
if isinstance(s, list):
    if len(s) < 500: bad.append(f"summary.json has only {len(s)} stocks")
    for k in ("symbol", "price", "change", "rs"):
        miss = sum(1 for r in s if k not in r)
        if miss > 0.05 * len(s): bad.append(f"summary.json: field '{k}' missing in {miss} stocks")
elif s is not None: bad.append("summary.json is not a list")
c = load("data/cpr.json") if os.path.exists("data/cpr.json") else None
if os.path.exists("data/cpr.json") and c is not None:
    rows = c.get("rows", {})
    if isinstance(s, list) and len(rows) < 0.8 * len(s): bad.append(f"cpr.json covers only {len(rows)} of {len(s)} stocks")
    for sym, d in list(rows.items())[:200]:
        if "wc" in d and len(d["wc"]) != 9: bad.append(f"cpr.json {sym}: weekly CPR levels are not 9 numbers"); break
elif not os.path.exists("data/cpr.json"): print("note: data/cpr.json not found (CPR tab will use the old layout or stay empty)")
for p in ("data/meta.json", "data/sectors.json", "data/themes.json"):
    if os.path.exists(p): load(p)
if bad:
    print("DATA CHECK FAILED"); [print(" -", b) for b in bad]; sys.exit(1)
print("data check passed")

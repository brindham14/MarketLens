"""Builds data/summary.json, quotes.json, history.json and candles/SYMBOL.json for MarketLens RS.
Input : symbols.csv  (columns: symbol,name,sector  - NSE symbols without .NS)
Run   : python update_data.py      (after market close, ~16:30 IST)
Needs : pip install yfinance pandas numpy
"""
import json, os, sys
import numpy as np, pandas as pd, yfinance as yf

OUT = "data"; os.makedirs(f"{OUT}/candles", exist_ok=True)
import io, urllib.request
H = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}
def get_csv(url):
    return pd.read_csv(io.BytesIO(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=60).read()))
rows_ = []
try:   # ALL NSE equities (series EQ/BE/SM...)
    eq = get_csv("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"); eq.columns = [c.strip() for c in eq.columns]
    eq = eq[eq["SERIES"].str.strip().isin(["EQ", "BE", "BZ", "SM", "ST"])]
    rows_ = [dict(symbol=r["SYMBOL"].strip(), name=r["NAME OF COMPANY"].strip(), sector="") for _, r in eq.iterrows()]
    print("NSE full list:", len(rows_), "stocks")
except Exception as e:
    print("could not download NSE list:", e)
try:   # sectors for the Nifty Total Market stocks
    tm = get_csv("https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv")
    sec = {r["Symbol"].strip(): r["Industry"].strip() for _, r in tm.iterrows()}
    for r in rows_: r["sector"] = sec.get(r["symbol"], "")
except Exception as e:
    print("could not download sector list:", e)
mine = pd.read_csv("symbols.csv").fillna("")           # your own file always wins / is the fallback
d = {r["symbol"]: r for r in rows_}
for r in mine.to_dict("records"): d[str(r["symbol"]).strip().upper()] = dict(symbol=str(r["symbol"]).strip().upper(), name=r["name"], sector=r["sector"])
meta = pd.DataFrame(list(d.values()))
syms = meta["symbol"].str.strip().str.upper().tolist()
info = {r.symbol.strip().upper(): r for r in meta.itertuples()}

def fetch(batch):
    return yf.download([s + ".NS" for s in batch], start="2019-01-01", group_by="ticker",
                       auto_adjust=False, threads=True, progress=False)

frames = {}
for i in range(0, len(syms), 50):
    b = syms[i:i + 50]
    try:
        raw = fetch(b)
    except Exception as e:
        print("batch failed", b[:3], e); continue
    for s in b:
        try:
            df = raw[s + ".NS"].dropna(subset=["Open", "High", "Low", "Close"])
            if len(df) >= 30: frames[s] = df
        except Exception: pass
print(f"{len(frames)}/{len(syms)} symbols downloaded")
if not frames: sys.exit("no data downloaded - nothing written")

r2 = lambda x: [round(float(v), 2) for v in x]
rows, quotes = [], {}
for s, df in frames.items():
    d = [t.strftime("%Y-%m-%d") for t in df.index]
    json.dump({"d": d, "o": r2(df.Open), "h": r2(df.High), "l": r2(df.Low), "c": r2(df.Close),
               "v": [int(v) for v in df.Volume.fillna(0)], "sample": 0},
              open(f"{OUT}/candles/{s}.json", "w"), separators=(",", ":"))
    c = df.Close.values
    if len(c) < 260: continue          # need ~1 year of history for RS rating (skips new IPOs)
    price = c[-1]; chg = (price / c[-2] - 1) * 100
    quotes[s] = {"p": round(float(price), 2), "c": round(float(chg), 2)}
    ret = lambda n: (price / c[-n - 1] - 1) * 100 if len(c) > n else np.nan
    ma = lambda n: c[-n:].mean() if len(c) >= n else np.nan
    ma200_prev = c[-221:-21].mean() if len(c) >= 221 else np.nan
    stage = 2 if (price > ma(150) > ma(200) and ma(200) > ma200_prev) else \
            4 if (price < ma(150) and price < ma(200)) else 1 if price < ma(200) else 3
    hi = df.High.values[-252:].max()
    turn = float((df.Close * df.Volume).values[-50:].mean() / 1e7)
    m = info.get(s)
    rows.append(dict(symbol=s, name=(m.name if m and m.name else s), sector=(m.sector if m else ""),
                     price=round(float(price), 2), change=round(float(chg), 2), stage=stage,
                     ret3=round(float(ret(63)), 1), ret6=round(float(ret(126)), 1), ret1=round(float(ret(252)), 1),
                     fromHigh=round(float((price / hi - 1) * 100), 1), turnover=round(turn),
                     _score=np.nansum([ret(63) * .4, ret(126) * .3, ret(252) * .3])))
import math
_num = ("price", "change", "ret3", "ret6", "ret1", "fromHigh", "turnover", "_score")
rows = [r for r in rows if all(math.isfinite(float(r[k])) for k in _num)]   # NaN would make the JSON unreadable in browsers
sc = pd.Series([r["_score"] for r in rows]).rank(pct=True) * 98 + 1
for r, v in zip(rows, sc): r["rs"] = int(round(v)); del r["_score"]
rows.sort(key=lambda r: -r["rs"])
for i, r in enumerate(rows, 1): r["rank"] = i
json.dump(rows, open(f"{OUT}/summary.json", "w"), separators=(",", ":"), allow_nan=False)
json.dump({"updated": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(), "quotes": quotes},
          open(f"{OUT}/quotes.json", "w"), separators=(",", ":"))

closes = pd.DataFrame({s: df.Close for s, df in frames.items()}).sort_index().ffill()
json.dump({"dates": [t.strftime("%Y-%m-%d") for t in closes.index],
           "stocks": {s: {"sec": info[s].sector if s in info else "",
                          "c": [None if pd.isna(v) else round(float(v), 2) for v in closes[s]]}
                      for s in closes.columns}},
          open(f"{OUT}/history.json", "w"), separators=(",", ":"))
print("done:", len(rows), "stocks")

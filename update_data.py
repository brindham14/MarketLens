"""Builds everything the MarketLens RS site reads from data/ :
   summary.json  one row per stock: price, returns, RS rating, stage, trend checks and the pattern fields
                 used by the Stock Screener / Popular Scans (VCP, cup & handle, flag, pocket pivot, Darvas ...)
   themes.json   theme / industry strength used by the Swing Trade tab
   quotes.json, history.json, candles/SYMBOL.json, candles/_NIFTY500.json

Run   : python update_data.py      (after market close, ~17:00 IST)
Needs : pip install yfinance pandas numpy
Optional input files next to this script:
   symbols.csv  columns symbol,name,sector  (your own extra stocks; always wins)
   themes.csv   columns symbol,theme        (your own themes, e.g. Cables & Wires)
"""
import io, json, math, os, sys, urllib.request, datetime as dt
import numpy as np, pandas as pd, yfinance as yf

OUT = "data"; os.makedirs(f"{OUT}/candles", exist_ok=True)
H = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}

def get_csv(url):
    return pd.read_csv(io.BytesIO(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=60).read()))

# ---------------------------------------------------------------- stock list
rows_ = []
try:   # ALL NSE equities
    eq = get_csv("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"); eq.columns = [c.strip() for c in eq.columns]
    eq = eq[eq["SERIES"].str.strip().isin(["EQ", "BE", "BZ", "SM", "ST"])]
    ldcol = next((c for c in eq.columns if "LISTING" in c.upper()), None)
    rows_ = [dict(symbol=r["SYMBOL"].strip(), name=r["NAME OF COMPANY"].strip(), sector="",
                  ld=(pd.to_datetime(r[ldcol], errors="coerce", dayfirst=True) if ldcol else pd.NaT)) for _, r in eq.iterrows()]
    print("NSE full list:", len(rows_), "stocks")
except Exception as e:
    print("could not download NSE list:", e)
TM = set()
try:   # sectors + the scan universe (Nifty Total Market) used for ranking
    tm = get_csv("https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv")
    sec = {r["Symbol"].strip(): r["Industry"].strip() for _, r in tm.iterrows()}
    TM = set(sec)
    for r in rows_: r["sector"] = sec.get(r["symbol"], "")
    print("Nifty Total Market:", len(TM), "stocks")
    for _u in ("ind_nifty500list.csv", "ind_niftymicrocap250_list.csv"):          # Nifty 500 + Microcap 250 = the reference's scan list; add any the total-market file misses
        try:
            _x = get_csv("https://nsearchives.nseindia.com/content/indices/" + _u); _x.columns = [c.strip() for c in _x.columns]
            _new = {r["Symbol"].strip(): r["Industry"].strip() for _, r in _x.iterrows() if r["Symbol"].strip() not in sec}
            sec.update(_new); TM |= set(_new)
            for r in rows_: r["sector"] = r["sector"] or sec.get(r["symbol"], "")
            print(_u, "added", len(_new), "stocks missing from the total-market list")
        except Exception as e:
            print(_u, "not available:", str(e)[:60])
except Exception as e:
    print("could not download sector list:", e)
# index membership (Nifty 50/Next 50/100/500/Midcap/Smallcap/Microcap) and F&O list - each is optional
IDX = {}
for _nm, _fn in [("Nifty 50", "ind_nifty50list"), ("Nifty Next 50", "ind_niftynext50list"), ("Nifty 100", "ind_nifty100list"),
                 ("Nifty 500", "ind_nifty500list"), ("Nifty Midcap 150", "ind_niftymidcap150list"),
                 ("Nifty Smallcap 250", "ind_niftysmallcap250list"), ("Nifty Microcap 250", "ind_niftymicrocap250_list")]:
    try:
        _c = get_csv(f"https://nsearchives.nseindia.com/content/indices/{_fn}.csv")
        IDX[_nm] = set(_c["Symbol"].astype(str).str.strip()); print(_nm, len(IDX[_nm]), "members")
    except Exception as e:
        print("could not download", _nm, e)
# fill sectors for stocks outside Nifty Total Market, using the Industry column of the other NSE index lists
try:
    _have = {r["symbol"]: r for r in rows_}
    for _fn in ["ind_nifty500list", "ind_niftymidcap150list", "ind_niftysmallcap250list", "ind_niftymicrocap250_list"]:
        try:
            _c = get_csv(f"https://nsearchives.nseindia.com/content/indices/{_fn}.csv"); _c.columns = [x.strip() for x in _c.columns]
            _ic = next((x for x in _c.columns if x.lower() == "industry"), None)
            if not _ic: continue
            for _, _r in _c.iterrows():
                _sy = str(_r["Symbol"]).strip(); _ind = str(_r[_ic]).strip()
                if _sy in _have and not _have[_sy]["sector"] and _ind and _ind.lower() != "nan": _have[_sy]["sector"] = _ind
        except Exception as e:
            print("sector fill skipped", _fn, e)
    print("stocks with a sector:", sum(1 for r in rows_ if r["sector"]), "of", len(rows_))
except Exception as e:
    print("sector fill failed:", e)
# sectors for the stocks no index list covers: NSE quote API (macro-economic sector, same names as the dropdown).
# Remembered in data/sector_cache.json, at most 500 new stocks per run, so the gap closes over a few daily runs.
def fill_sectors_nse(rows, cache_path=f"{OUT}/sector_cache.json", per_run=500, fetch=None):
    import http.cookiejar, time
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    for r in rows:
        if not r["sector"] and cache.get(r["symbol"]): r["sector"] = cache[r["symbol"]]
    todo = [r for r in rows if not r["sector"] and r["symbol"] not in cache][:per_run]
    if todo:
        op = None
        if fetch is None:
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            hd = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*", "Referer": "https://www.nseindia.com/"}
            def warm():
                try: op.open(urllib.request.Request("https://www.nseindia.com/", headers=hd), timeout=30).read()
                except Exception: pass
            warm()
            def fetch(sym):
                u = "https://www.nseindia.com/api/quote-equity?symbol=" + urllib.request.quote(sym)
                d = json.loads(op.open(urllib.request.Request(u, headers=hd), timeout=30).read())
                ii = d.get("industryInfo") or {}
                return (ii.get("macro") or ii.get("sector") or "").strip()
        bad = 0; got = 0
        for i, r in enumerate(todo):
            try:
                sec = fetch(r["symbol"]); bad = 0
                cache[r["symbol"]] = sec                     # "" = NSE has no sector for it, do not ask again
                if sec: r["sector"] = sec; got += 1
            except Exception:
                bad += 1
                if bad >= 8: print("NSE quote API not reachable - sector fill stopped"); break
            if op is not None and i % 100 == 99: warm()
            if op is not None: time.sleep(0.25)
        print("NSE sector fill:", got, "new sectors from", len(todo), "tried")
        try: json.dump(cache, open(cache_path, "w"), separators=(",", ":"))
        except Exception as e: print("could not save sector cache", e)
    print("stocks with a sector:", sum(1 for r in rows if r["sector"]), "of", len(rows))
try: fill_sectors_nse(rows_)
except Exception as e: print("NSE sector fill failed:", e)
# your own sector lists (sectors.csv: symbol,sector) win over the NSE sector - they are copied from the reference's Workspace
try:
    if os.path.exists("sectors.csv"):
        _sc = pd.read_csv("sectors.csv", comment="#").fillna(""); _m = {str(r["symbol"]).strip().upper(): str(r["sector"]).strip() for r in _sc.to_dict("records")}
        _h = {r["symbol"]: r for r in rows_}; _n = 0
        for _s, _v in _m.items():
            if _s in _h and _v: _h[_s]["sector"] = _v; _n += 1
        print("sectors.csv applied to", _n, "stocks")
except Exception as e:
    print("sectors.csv skipped:", e)
INDEX_OK = len(IDX.get("Nifty 500", ())) >= 400
FNO = set()
try:
    _f = get_csv("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"); _f.columns = [c.strip() for c in _f.columns]
    FNO = set(_f["SYMBOL"].astype(str).str.strip()) - {"Symbol", "SYMBOL"}; print("F&O stocks:", len(FNO))
except Exception as e:
    print("could not download F&O list:", e)
mine = pd.read_csv("symbols.csv").fillna("") if os.path.exists("symbols.csv") else pd.DataFrame(columns=["symbol", "name", "sector"])
d = {r["symbol"]: r for r in rows_}
for r in mine.to_dict("records"):
    d[str(r["symbol"]).strip().upper()] = dict(symbol=str(r["symbol"]).strip().upper(), name=r["name"], sector=r["sector"])
LISTED = {r["symbol"]: r.get("ld") for r in rows_ if r.get("ld") is not None and r.get("ld") == r.get("ld")}
meta = pd.DataFrame([{k: v for k, v in r.items() if k != "ld"} for r in d.values()])
syms = meta["symbol"].str.strip().str.upper().tolist()
info = {r.symbol.strip().upper(): r for r in meta.itertuples()}

# ---------------------------------------------------------------- circuit (price band) limits
BAND = {}
try:
    _sl = get_csv("https://nsearchives.nseindia.com/content/equities/sec_list.csv"); _sl.columns = [str(c).strip() for c in _sl.columns]
    _bc = next(c for c in _sl.columns if "band" in c.lower()); _sc = next(c for c in _sl.columns if c.lower().startswith("symbol"))
    for _s, _b in zip(_sl[_sc].astype(str).str.strip().str.upper(), _sl[_bc].astype(str).str.strip()):
        try: BAND[_s] = int(float(_b))
        except ValueError: pass
    print("price bands:", len(BAND), "| 2%:", sum(v == 2 for v in BAND.values()), "| 5%:", sum(v == 5 for v in BAND.values()))
except Exception as _e:
    print("price bands unavailable:", _e)

# ---------------------------------------------------------------- prices
def fetch(batch, tick=lambda s: s + ".NS"):
    return yf.download([tick(s) for s in batch], start="2019-01-01", group_by="ticker",
                       auto_adjust=False, threads=True, progress=False, actions=True)

frames = {}
for i in range(0, len(syms), 50):
    b = syms[i:i + 50]
    raw = None
    for _try in range(2):                                   # one retry per batch
        try:
            raw = fetch(b); break
        except Exception as e:
            print("batch failed", b[:3], e)
    if raw is None: continue
    for s in b:
        try:
            df = raw[s + ".NS"].dropna(subset=["Open", "High", "Low", "Close"])
            if len(df) >= 30: frames[s] = df
        except Exception: pass
print(f"{len(frames)}/{len(syms)} symbols downloaded")
# a session that is still being traded (run during market hours) is incomplete: never keep today's partial bar
_now_ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
if _now_ist.weekday() < 5 and _now_ist.hour * 60 + _now_ist.minute < 16 * 60 + 15:
    _today = pd.Timestamp(_now_ist.date())
    frames = {s: df[df.index.normalize() < _today] for s, df in frames.items()}
    frames = {s: df for s, df in frames.items() if len(df) >= 30}
    print("market still open (IST):", _now_ist.strftime("%H:%M"), "- today's partial bar removed")
# Yahoo is often a day late for most stocks: fill the missing sessions from NSE's own daily file (bhavcopy)
try:
    _base = pd.Series([df.index[-1] for df in frames.values()]).mode().iloc[0]
    _end = pd.Timestamp(_now_ist.date()) if _now_ist.hour * 60 + _now_ist.minute >= 16 * 60 + 15 else pd.Timestamp(_now_ist.date()) - pd.Timedelta(days=1)
    for _d in pd.bdate_range(_base + pd.Timedelta(days=1), _end):
        try:
            _b = get_csv(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{_d:%d%m%Y}.csv")
        except Exception as _e:
            print("bhavcopy", _d.date(), "not available:", str(_e)[:60]); continue
        _b.columns = [str(c).strip() for c in _b.columns]
        _b["SYMBOL"] = _b["SYMBOL"].astype(str).str.strip().str.upper(); _b["SERIES"] = _b["SERIES"].astype(str).str.strip()
        _b = _b[_b["SERIES"].isin(["EQ", "BE", "BZ", "SM", "ST"])].drop_duplicates("SYMBOL").set_index("SYMBOL")
        for _c in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"): _b[_c] = pd.to_numeric(_b[_c], errors="coerce")
        _n = 0
        for _s in list(frames):
            _df = frames[_s]
            if _s in _b.index and _df.index[-1] < _d:
                _r = _b.loc[_s]
                if not (_r.OPEN_PRICE > 0 and _r.HIGH_PRICE > 0 and _r.LOW_PRICE > 0 and _r.CLOSE_PRICE > 0): continue
                _row = pd.DataFrame({"Open": [_r.OPEN_PRICE], "High": [_r.HIGH_PRICE], "Low": [_r.LOW_PRICE], "Close": [_r.CLOSE_PRICE],
                                     "Adj Close": [_r.CLOSE_PRICE], "Volume": [_r.TTL_TRD_QNTY]}, index=[_d])
                frames[_s] = pd.concat([_df, _row.reindex(columns=_df.columns)]); _n += 1
        print("bhavcopy", _d.date(), "- sessions added for", _n, "stocks")
except Exception as _e:
    print("bhavcopy fill skipped:", _e)

# Yahoo sometimes misses a bonus / split / demerger: the price then "falls" 40-80% in one day, which NSE price bands do not allow.
# Roll such a day back so the history is continuous (otherwise RS and the charts show a fake crash).
def fix_jumps(df, lo=0.62, hi=1.6, window=300):
    c = df["Close"].values.astype(float); r = np.full(len(c), np.nan); r[1:] = c[1:] / c[:-1]
    adj = np.ones(len(c)); fixed = []
    for i in range(len(c) - 1, max(len(c) - 1 - window, 0), -1):
        v = r[i]
        if v == v and (v < lo or v > hi):
            adj[:i] *= v; fixed.append((df.index[i].strftime("%Y-%m-%d"), round(float(v), 2)))
    if not fixed: return df, fixed
    df = df.copy()
    for k in ("Open", "High", "Low", "Close"): df[k] = df[k].values.astype(float) * adj
    if "Volume" in df: df["Volume"] = df["Volume"].values.astype(float) / adj
    return df, fixed
SPLIT_M = {}                                     # stock -> months (YYYY-MM) with a split / bonus; the reference leaves those months out
for _s, _df in frames.items():
    try:
        if "Stock Splits" in _df:
            _ss = _df["Stock Splits"].fillna(0)
            for _d in _ss.index[_ss.values > 0]: SPLIT_M.setdefault(_s, set()).add(_d.strftime("%Y-%m"))
    except Exception: pass
_fx = {}
for _s in list(frames):
    try:
        frames[_s], _f = fix_jumps(frames[_s])
        if _f: _fx[_s] = _f
    except Exception: pass
for _s, _l in _fx.items():
    for _d, _v in _l: SPLIT_M.setdefault(_s, set()).add(_d[:7])
print("stocks with a split/bonus month:", len(SPLIT_M))
print("price jumps rolled back (unadjusted corporate actions):", len(_fx), dict(list(_fx.items())[:12]))
if not frames: sys.exit("no data downloaded - nothing written")
# safeguard: a partly failed download must not overwrite a good release (the old files stay published)
try:
    _prev = len(json.load(open(f"{OUT}/summary.json")))
    if len(frames) < 0.85 * _prev:
        sys.exit(f"only {len(frames)} stocks downloaded vs {_prev} in the last release - keeping the previous data, nothing written")
except (FileNotFoundError, ValueError):
    pass

# benchmark: Nifty 500 (falls back to Nifty 50)
bench = None
for tk in ("^CRSLDX", "^NSEI"):
    try:
        bdf = yf.download(tk, start="2019-01-01", auto_adjust=False, progress=False)
        if isinstance(bdf.columns, pd.MultiIndex): bdf.columns = bdf.columns.get_level_values(0)
        bdf = bdf.dropna(subset=["Close"])
        if len(bdf) > 300: bench = bdf; print("benchmark:", tk, len(bdf)); break
    except Exception as e:
        print("benchmark failed", tk, e)

r2 = lambda x: [round(float(v), 2) for v in x]
def write_candles(name, df):
    json.dump({"d": [t.strftime("%Y-%m-%d") for t in df.index], "o": r2(df.Open), "h": r2(df.High), "l": r2(df.Low),
               "c": r2(df.Close), "v": [int(v) for v in df.Volume.fillna(0)], "sample": 0},
              open(f"{OUT}/candles/{name}.json", "w"), separators=(",", ":"))
for s, df in frames.items(): write_candles(s, df)
if bench is not None: write_candles("NIFTY500", bench)

# ---------------------------------------------------------------- helpers
def sma(a, n): return float(np.mean(a[-n:])) if len(a) >= n else np.nan
def depth_in(h, l):            # deepest pullback % inside a window (running peak -> later low)
    rm = np.maximum.accumulate(h); return float(np.max((rm - l) / rm)) * 100

def darvas_state(h, l, c, per=252, near=5, hold=3, span=200):
    """Newest Darvas box, like the reference: ('b', sessions since breakout, top, bottom|None), ('i', sessions since top, top, bottom)
    for a complete box, ('t', sessions since top, top, None) when the top is set but the bottom is still forming, or None.
    A box top is a high within 5% of the 52-week high that no later high exceeds for 3 sessions. The bottom is the lowest low after it
    that holds 3 sessions. Any higher high before the bottom is set voids the box (a close above it is a breakout); afterwards a close
    above the top is a breakout and a close below the bottom cancels it. The bar that ends a box can itself start the next box."""
    n = len(h)
    if n < 252: return None                         # needs a full 52-week history
    rmax = pd.Series(h).rolling(per, min_periods=1).max().values
    k = max(3, n - 1 - 2 * span); last = None
    while k < n - hold:
        if h[k] < rmax[k] * (1 - near / 100) or any(h[j] > h[k] for j in range(k + 1, k + hold + 1)):
            k += 1; continue
        top = float(h[k]); lo, at, jj, q, kind = 1e18, -1, None, None, None
        for j in range(k + 1, n):
            if h[j] > top: q, kind = j, ("b" if c[j] > top else "v"); break
            if l[j] < lo: lo, at = l[j], j
            if at > 0 and j - at >= hold: jj = j; break
        if kind == "b": last = ("b", n - 1 - q, top, None); k = q; continue
        if kind == "v": k = q; continue
        if jj is None: last = ("t", n - 1 - k, top, None); break
        bot = float(lo)
        for x in range(jj + 1, n):
            if c[x] < bot: q, kind = x, "x"; break
            if c[x] > top: q, kind = x, "b"; break
        if q is None: last = ("i", n - 1 - k, top, bot); break
        last = ("b", n - 1 - q, top, bot) if kind == "b" else None
        k = q
    return last

def darvas(h, l, per=252, near=5):
    """Port of the chart's Darvas-box indicator: list of (top_index, complete_index, top, bottom)."""
    n = len(h); out = []; k = 3
    rmax = pd.Series(h).rolling(per, min_periods=1).max().values
    while k < n - 3:
        if h[k] < rmax[k] * (1 - near / 100) or any(h[j] > h[k] for j in range(k + 1, k + 4)):
            k += 1; continue
        lo, at = 1e18, -1
        for j in range(k + 1, n):
            if l[j] < lo: lo, at = l[j], j
            if at > 0 and j - at >= 3:
                out.append((k, j, float(h[k]), float(lo))); k = j; break
        k += 1
    return out

def pivot_highs(h, c, N=5, within=10, cancel=25, per=252, span=320):
    """Port of the chart's Pivots indicator: a high near the 52-week high with N lower highs on each side.
    Returns [(index, price, break_index_or_None)] for pivots not cancelled by a 25% fall."""
    n = len(c); rm = pd.Series(h).rolling(per, min_periods=1).max().values; out = []
    for k in range(max(N, n - span), n - N):
        hk = h[k]
        if hk < rm[k] * (1 - within / 100): continue
        if max(h[k - N:k].max(), h[k + 1:k + N + 1].max()) > hk: continue
        brk, canc = None, False
        for j in range(k + N + 1, n):
            if c[j] > hk: brk = j; break
            if c[j] < hk * (1 - cancel / 100): canc = True; break
        if not canc: out.append((k, float(hk), brk))
    return out

def cup_handle(h, l, minDepth=.12, maxDepth=.40, minCup=35, maxCup=260, rimTol=.10, minHandle=4, maxHandle=26, maxHD=.12, minLeft=10):
    """Cup with handle (matches the reference): a 12-40% deep cup lasting 7+ weeks whose right side recovered to within 10% of the left
    high, then a 3-25 session handle (counted after the pivot) no deeper than 12% that stays in the upper half of the cup.
    The pivot is the handle high, which is also the highest point of the right side. The left high is searched among bars at least
    7 weeks before the handle; of the valid handles the one with the highest pivot is kept."""
    n = len(h); end = n - 1; best = None
    for hlen in range(minHandle, maxHandle + 1):
        hs = end - hlen + 1
        if hs < minCup: continue
        hh = float(h[hs:].max())
        if h[hs] < hh or hh <= 0: continue
        hl = float(l[hs:].min()); hd = (hh - hl) / hh
        if hd > maxHD: continue
        lo = max(0, hs - maxCup); seg = h[lo:hs - minCup + 1]
        if len(seg) < 1: continue
        la = lo + int(np.argmax(seg)); LH = float(h[la])
        if LH <= 0: continue
        bi = la + int(np.argmin(l[la:hs])); bot = float(l[bi]); depth = (LH - bot) / LH
        if depth < minDepth or depth > maxDepth: continue
        if hh < LH * (1 - rimTol): continue
        if hl < bot + (LH - bot) * .5: continue
        if bi - la < minLeft: continue
        if hh < float(h[bi:hs].max()): continue
        if best is None or hh > best["rightHigh"]:
            best = dict(depth=depth * 100, hD=hd * 100, cupLen=hs - la, rightHigh=hh, hlen=hlen)
    return best

def pattern_fields(o, h, l, c, v):
    """Pattern / setup fields for one stock from daily OHLCV (arrays, latest last)."""
    n = len(c); f = {}
    price = c[-1]
    ma50, ma150, ma200 = sma(c, 50), sma(c, 150), sma(c, 200)
    f["a50"] = int(price > ma50) if ma50 == ma50 else 0
    f["a200"] = int(price > ma200) if ma200 == ma200 else 0
    f["p50"] = round((price / ma50 - 1) * 100, 1) if ma50 == ma50 else None
    f["p200"] = round((price / ma200 - 1) * 100, 1) if ma200 == ma200 else None
    for _n in (21, 50):                                                     # % above the exponential averages (used by "Groups above an average")
        if n >= _n * 3:
            _a = 2.0 / (_n + 1); _e = float(c[-_n * 3])
            for _x in c[-_n * 3 + 1:]: _e = _a * float(_x) + (1 - _a) * _e
            f["e%d" % _n] = round((price / _e - 1) * 100, 1)
    # volume
    avg50 = np.mean(v[-51:-1]) if n > 51 else np.nan
    f["v"] = round(float(v[-1] / avg50), 2) if avg50 and avg50 == avg50 else None
    f["v10"] = round(float(np.mean(v[-10:]) / avg50), 2) if avg50 and avg50 == avg50 else None
    # breakout / pocket pivot
    f["b50"] = int(n > 51 and price > np.max(h[-51:-1]))
    dn = [v[i] for i in range(n - 11, n - 1) if c[i] < c[i - 1]]
    f["pp"] = int(n > 60 and c[-1] > c[-2] and price > ma50 and v[-1] > (max(dn) if dn else 0))
    # ranges / compression
    f["rng10"] = round(float((np.max(h[-45:]) - np.min(l[-45:])) / np.max(h[-45:]) * 100), 1)
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    f["atr1050"] = round(float(np.mean(tr[-10:]) / np.mean(tr[-50:]) * 100), 1) if len(tr) >= 50 else None
    f["inside5"] = int(sum(1 for i in range(n - 5, n) if h[i] <= h[i - 1] and l[i] >= l[i - 1]))
    f["low52"] = round(float(np.min(l[-252:])), 2)
    f["high1m"] = round(float(np.max(h[-21:])), 2); f["high3m"] = round(float(np.max(h[-63:])), 2)
    # pivot = resistance from the Pivots indicator: highest not-yet-cleared pivot high (last 3), or the one cleared today / most recently
    if n > 70:
        pvs = pivot_highs(h, c)
        today = [p for p in pvs if p[2] == n - 1]
        un = [p for p in pvs if p[2] is None][-3:]
        old = [p for p in pvs if p[2] is not None and n - 1 - p[2] <= 60]
        if un:
            k, pr, bj = max(un, key=lambda q: q[1]); f["pv"] = round(pr, 2); f["pivotHeldSessions"] = int(n - 1 - k); f["breakoutToday"] = 0
        elif today:
            k, pr, bj = today[-1]; f["pv"] = round(pr, 2); f["pivotHeldSessions"] = int(n - 1 - k); f["breakoutToday"] = 1
        elif old:
            k, pr, bj = max(old, key=lambda q: q[2]); f["pv"] = round(pr, 2); f["pivotHeldSessions"] = int(n - 1 - bj); f["breakoutToday"] = 0
    # VCP: three shrinking 4-week pullbacks, last <= 8%, volume drying up, above 50 DMA, near the high
    if n > 130:
        d1, d2, d3 = (depth_in(h[a:b], l[a:b]) for a, b in ((-60, -40), (-40, -20), (-20, None)))
        f["pbs"] = [round(d1, 1), round(d2, 1), round(d3, 1)]
        f["vcp"] = int(d1 > d2 > d3 and d3 <= 8 and (f["v10"] or 9) < 1 and f["a50"] and price >= 0.85 * np.max(h[-252:]))
    # Tight weeks: consecutive weekly closes within 1.5%
    return f

def _piv(H, L, C):
    P = (H + L + C) / 3.0; BC = (H + L) / 2.0; TC = 2 * P - BC
    if TC < BC: TC, BC = BC, TC
    return dict(P=P, BC=BC, TC=TC, R1=2 * P - L, S1=2 * P - H, R2=P + (H - L), S2=P - (H - L),
                R3=H + 2 * (P - L), S3=L - 2 * (H - P), W=abs(TC - BC) / P * 100 if P else 0.0)

def _lv(q):   # S3,S2,S1,BC,P,TC,R1,R2,R3
    return [round(float(q[k]), 2) for k in ("S3", "S2", "S1", "BC", "P", "TC", "R1", "R2", "R3")]

def _dirn(a, b):   # CPR direction vs the previous period: A ascending, D descending, M mixed, O overlapping
    up, dn = a["TC"] > b["TC"] and a["BC"] > b["BC"], a["TC"] < b["TC"] and a["BC"] < b["BC"]
    if up: return "A"
    if dn: return "D"
    if (a["TC"] > b["TC"] and a["BC"] < b["BC"]) or (a["TC"] < b["TC"] and a["BC"] > b["BC"]): return "M"
    return "O"

def _ema(x, n):
    a = 2.0 / (n + 1); e = float(x[0])
    for v in x[1:]: e = a * float(v) + (1 - a) * e
    return e

def _period_complete(last, rule, today=None):
    """True only when the calendar week / month that contains the bar `last` is really over: the bar is on the period's last weekday,
    or today (IST) is already past the period end. A running month is never treated as complete (28th-31st included)."""
    last = pd.Timestamp(last).normalize()
    today = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None).normalize()
    if rule == "W-FRI":
        end = last + pd.Timedelta(days=4 - last.weekday()) if last.weekday() <= 4 else last - pd.Timedelta(days=last.weekday() - 4)
        return bool(last >= end or today > end)
    end = pd.Timestamp(year=last.year, month=12, day=31) if rule == "YE" else last + pd.offsets.MonthEnd(0)
    lastday = end
    while lastday.weekday() > 4: lastday -= pd.Timedelta(days=1)                      # last weekday of the month
    return bool(last >= lastday or today > end)

def cpr_fields(df, today=None):
    """CPR + traditional pivots (daily / weekly / monthly) for the NEXT session, EMA stack, RSI(14), daily DRVOL and the CPR-tab scanners.
    Daily/weekly/monthly levels always come from the last COMPLETED period. Scanner rules follow the Chartink 'KAYBEE PIVOT' dashboard."""
    f = {}
    try:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["High", "Low", "Close"]); n = len(d)
        if n < 30: return f
        O, H, L, C = (d[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        V = np.nan_to_num(d["Volume"].values.astype(float))
        c = C[-1]
        dl = np.diff(C); up = np.where(dl > 0, dl, 0.0); dn = np.where(dl < 0, -dl, 0.0)
        au, ad = up[:14].mean(), dn[:14].mean()
        for i in range(14, len(dl)): au = (au * 13 + up[i]) / 14; ad = (ad * 13 + dn[i]) / 14
        f["rsi"] = round(float(100 - 100 / (1 + au / ad)) if ad > 0 else 100.0, 1)
        tail = C[-180:]
        e9, e20, e50 = _ema(tail, 9), _ema(tail, 20), _ema(tail, 50)
        f["es"] = int(e9 > e20 > e50)                                                   # EMA 9 > 20 > 50
        av20 = float(np.mean(V[-21:-1])) if n > 21 else 0.0
        f["drv"] = round(float(V[-1] / av20 * 100), 0) if av20 > 0 else None            # daily DRVOL(20) %
        t, y = _piv(H[-1], L[-1], C[-1]), _piv(H[-2], L[-2], C[-2])
        f["cw"] = round(float(t["W"]), 2)
        hist = [_piv(H[i], L[i], C[i])["W"] for i in range(max(0, n - 60), n)]
        f["cpn"] = int(f["cw"] <= float(np.percentile(hist, 20)))
        f["cpu"] = int(t["BC"] > y["TC"]); f["cpd"] = int(t["TC"] < y["BC"])
        f["cpr"] = [round(float(t[k]), 2) for k in ("S2", "S1", "BC", "P", "TC", "R1", "R2")]
        f["cprNar"] = int((t["R1"] - t["S1"]) < (y["R1"] - y["S1"]) and t["P"] > y["P"])
        f["pdh"] = round(float(H[-1]), 2)                                              # previous (last completed) day high
        f["dc"] = _lv(t); f["dcw"] = round(float(t["W"]), 2); f["dcd"] = _dirn(t, y)
        f["dnr"] = int((t["R1"] - t["S1"]) < (y["R1"] - y["S1"]))                       # daily range smaller than the day before
        wn_rng = mn_rng = None
        f["cDN"] = int(t["R1"] < y["R1"] and t["S1"] > y["S1"])                         # daily narrow: R1 lower and S1 higher than the day before (Chartink rule)
        for tag, rule in (("w", "W-FRI"), ("m", "ME"), ("y", "YE")):
            g = d.resample(rule).agg({"High": "max", "Low": "min", "Close": "last"}).dropna()
            if len(g) < 4: continue
            last = d.index[-1]
            if not _period_complete(last, rule, today): g = g.iloc[:-1]                  # the period still running is never used
            if len(g) < 3: continue
            if tag == "y" and d.index[0] > pd.Timestamp(year=int(g.index[-2].year), month=1, day=20): continue   # yearly CPR needs the full previous two years of candles
            a, b = _piv(*g.iloc[-1][["High", "Low", "Close"]]), _piv(*g.iloc[-2][["High", "Low", "Close"]])
            f[tag + "r1"] = round(float(a["R1"]), 2); f[tag + "s1"] = round(float(a["S1"]), 2); f[tag + "p"] = round(float(a["P"]), 2)
            f[tag + "nar"] = int((a["R1"] - a["S1"]) < (b["R1"] - b["S1"]) and a["P"] > b["P"])
            f[tag + "brk"] = int(c > a["R1"])
            f[tag + "c"] = _lv(a); f[tag + "cw"] = round(float(a["W"]), 2); f[tag + "cd"] = _dirn(a, b)
            rng = int((a["R1"] - a["S1"]) < (b["R1"] - b["S1"]))
            f[tag + "nr"] = rng                                                          # period range smaller than the previous period (wnr / mnr)
            f[tag + "h"] = round(float(g.iloc[-1]["High"]), 2)                           # previous COMPLETED week / month high (wh / mh) used as targets
            if tag == "w": wn_rng = rng; f["cWN"] = int(a["R1"] < b["R1"] and a["S1"] > b["S1"])   # weekly narrow range = inside the previous week (R1 lower, S1 higher), like the monthly rule
            elif tag == "m":
                f["cMN"] = int(a["R1"] < b["R1"] and a["S1"] > b["S1"]); mn_rng = rng
            else:
                f["cYN"] = int(a["R1"] < b["R1"] and a["S1"] > b["S1"])                  # yearly narrow range = inside the previous year
        if wn_rng is not None and mn_rng is not None: f["cAN"] = int(wn_rng and mn_rng)
        f["cWR"] = int(f.get("cWN") == 1 and f.get("wbrk") == 1)
        f["cMR"] = int(f.get("cMN") == 1 and f.get("mbrk") == 1)
        f["cWMR"] = int(f["cWR"] and f["cMR"])
        f["cYR"] = int(f.get("cYN") == 1 and f.get("ybrk") == 1)                        # yearly narrow, then close above the yearly R1
        f["cALL"] = int(all(f.get(k) == 1 for k in ("cDN", "cWN", "cMN", "cYN")))       # narrow on Daily, Weekly, Monthly and Yearly together
        # Pattern breakout: bullish candle that closes above the last 3 closes and opens, volume above its 20-day average, close above the 20 SMA
        if n > 25:
            sma20 = float(np.mean(C[-20:])); vsma20 = float(np.mean(V[-20:])); vsma5 = float(np.mean(V[-5:]))
            f["bpat"] = int(O[-1] < C[-1] and all(C[-1] > C[-i] and C[-1] > O[-i] for i in (2, 3, 4)) and V[-1] > vsma20 and sma20 < C[-1])
            prev20 = float(np.mean(C[-21:-1]))
            f["b20"] = int(C[-1] > sma20 and C[-2] <= prev20 and V[-1] >= 300000 and V[-1] > vsma5)
        if n > 210:
            m200 = [float(np.mean(C[i - 199:i + 1])) for i in range(n - 6, n)]
            f["a200v"] = int(C[-1] > m200[-1] and any(C[n - 6 + j] <= m200[j] for j in range(5)) and (f["drv"] or 0) >= 150)
    except Exception as e:
        pass
    return f

def _levels_by_bar(d, rule):
    """CPR levels in force on every daily bar = pivots of the PREVIOUS completed week/month (never the period the bar belongs to -> no lookahead)."""
    key = d.index.to_period("W-FRI") if rule == "W" else d.index.to_period("M")
    g = d.groupby(key).agg(H=("High", "max"), L=("Low", "min"), C=("Close", "last"))
    P = (g.H + g.L + g.C) / 3.0; bc = (g.H + g.L) / 2.0; tc = 2 * P - bc
    lv = pd.DataFrame({"P": P, "TC": np.maximum(tc, bc), "BC": np.minimum(tc, bc), "R1": 2 * P - g.L, "R2": P + (g.H - g.L)}).shift(1)
    return lv.reindex(key).set_axis(d.index)

def entry_fields(df, f):
    """Four long-only CPR entries on real daily candles for Weekly (swing) and Monthly (positional) CPR.
    Evaluated on the latest COMPLETED daily bar t. Filters (all four): EMA9>EMA20>EMA50, RSI(14)>60, daily DRVOL(20)>=150%.
    Returns f["en"] = [[tf, code, status, entry, stop, T1, T2], ...]; status C = confirmed (buy above the confirmation candle high), F = forming."""
    out = []
    try:
        if f.get("es") != 1 or (f.get("rsi") or 0) <= 60 or (f.get("drv") or 0) < 150: return {"en": out}
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
        n = len(d)
        if n < 80 or d["Volume"].iloc[-1] <= 0: return {"en": out}
        O, H, L, C = (d[k].values.astype(float) for k in ("Open", "High", "Low", "Close")); t = n - 1
        e9, e20, e50 = (d.Close.ewm(span=k, adjust=False).mean().values for k in (9, 20, 50))
        TOL = 0.005
        f["op"] = round(float(O[t]), 2); f["gp"] = round(float((O[t] / C[t - 1] - 1) * 100), 2)
        def bull(i): return C[i] > O[i] and C[i] > C[i - 1] and C[i] >= L[i] + 0.5 * (H[i] - L[i])     # bullish candle closing in its upper half
        for tf, rule in (("w", "W"), ("m", "M")):
            lv = _levels_by_bar(d, rule)
            if lv.iloc[t].isna().any() or lv.iloc[t - 20:t + 1].isna().any().any(): continue            # need completed periods behind every bar used
            P, TC, R1, R2 = (lv[k].values for k in ("P", "TC", "R1", "R2"))
            found = {}
            def add(code, st, stop):
                entry = float(H[t]); stop = float(stop)
                if not (stop < entry < R2[t]): return                                                       # no risk or no target left
                found[code] = [tf, code, st, round(entry, 2), round(stop, 2), round(float(R1[t]), 2), round(float(R2[t]), 2)]
            # 1) P Breakout: previous close at/below P, latest close above P
            if C[t - 1] <= P[t] and C[t] > P[t]: add("PB", "C", L[t])
            # 2) P Retest: an earlier close above P, then a test of P in the last 3 sessions that held (closes stay >= P), then a bullish candle
            for code, LV in (("PR", P), ("TB", TC)):
                for k in (t - 1, t - 2, t - 3, t):                                                          # most recent test first (tightest honest stop)
                    if L[k] > LV[k] * (1 + TOL) or C[k] < LV[k]: continue                                   # no touch, or support failed
                    if any(C[j] < LV[j] for j in range(k, t + 1)): continue
                    if not any(C[j] > LV[j] for j in range(max(0, k - 15), k)): continue                    # must have traded above before the test
                    if code == "TB" and not C[k - 1] > TC[k - 1]: continue                                  # TC bounce: came from above the CPR
                    if k < t and bull(t): add(code, "C", min(L[k:t + 1])); break
                    if k == t: add(code, "F", min(L[k:t + 1])); break
            # 4) EMA Pullback: price above P, EMA stack, touched EMA 9/20 within the last 5 sessions, held EMA 20 / 50, bullish candle
            if C[t] > P[t] and C[t] >= e20[t]:
                for k in (t - 1, t - 2, t - 3, t - 4, t - 5, t):
                    if L[k] <= max(e9[k], e20[k]) * (1 + TOL) and all(C[j] > e50[j] for j in range(k, t + 1)):
                        if k < t and bull(t): add("EP", "C", min(L[k:t + 1])); break
                        if k == t: add("EP", "F", min(L[k:t + 1])); break
            out += list(found.values())
    except Exception:
        out = []
    return {"en": out} if out else {}


def weekly_tight(close_series):
    wc = close_series.resample("W-FRI").last().dropna().values
    t = 0
    for i in range(len(wc) - 1, 0, -1):
        if abs(wc[i] / wc[i - 1] - 1) <= 0.015: t += 1
        else: break
    return t

def flag_cup_my(h, l, c):
    f = {}; n = len(c); price = c[-1]
    # Flag (reference rule): a run-up of 25%+ (pole, measured from the lowest low of the 40 sessions before) into a new 52-week high made 4-20 sessions ago,
    # then a pullback of 12% or less; the high must not have been exceeded since.
    f["fl"] = 0
    if n > 80:
        hi52 = float(np.max(h[-252:])); bestf = None
        for age in range(4, 21):
            pk = n - 1 - age
            if pk < 40 or h[pk] < h[pk:].max() or h[pk] < hi52 * 0.995: continue
            pole = (h[pk] / float(np.min(l[max(0, pk - 40):pk + 1])) - 1) * 100; fd = (h[pk] - float(np.min(l[pk:]))) / h[pk] * 100
            if pole >= 25 and fd <= 12 and (bestf is None or pole > bestf[0]): bestf = (pole, fd)
        if bestf: f["fl"] = 1; f["pole"], f["fD"] = round(float(bestf[0]), 1), round(float(bestf[1]), 1)
    # Cup with handle (port of the chart's indicator, calibrated to the reference); the pivot is the handle high (cpv)
    if n > 200:
        cu = cup_handle(h[-400:], l[-400:])
        if cu:
            f["cup"] = 1; f["cupD"] = round(cu["depth"], 1); f["hD"] = round(cu["hD"], 1); f["cpv"] = round(cu["rightHigh"], 2)
    # Multi-year breakout: closes above the highest high of the last ~3 years (older than 60 sessions)
    if n > 400:
        look = min(n - 1, 756); seg = h[-look:-60]; ci = n - look + int(np.argmax(seg)); C = float(h[ci])
        above = np.where(c[-60:] > C)[0]
        if len(above) and price <= 1.15 * C and price >= C:
            bi_ = n - 60 + int(above[0])
            f["my"] = 1; f["cy"] = round((bi_ - ci) / 252, 1); f["pc"] = round((price / C - 1) * 100, 1)
    # Darvas boxes (like the reference): every box is followed until a close above its top (breakout) or below its bottom (cancelled);
    # the newest box that is still alive is the one shown. "In box Nd" counts the sessions since the box top was set.
    ds = darvas_state(h, l, c)
    if ds:
        kind, age, top, bot = ds
        f["dvt"] = round(top, 2)
        if bot is not None: f["dvl"] = round(bot, 2); f["dvh"] = round((top - bot) / top * 100, 1)
        if kind == "b":
            if age <= 4: f["dvb"] = 1; f["dvd"] = age
        elif kind == "i":
            f["dvn"] = 1; f["dva"] = age
            if price >= top * 0.95: f["dvi"] = 1
        else:
            f["dvp"] = 1; f["dva"] = age
    return f

# ---------------------------------------------------------------- per-stock features
# Yahoo publishes a new session for only a few stocks first: use the newest date that most stocks have, and cut the rest
_lasts = pd.Series([df.index[-1] for df in frames.values()]).sort_values()
_cut = _lasts.iloc[int(len(_lasts) * 0.4)]                    # 60% of the stocks have data up to this date or later
if _cut < _lasts.iloc[-1]:
    print("session", _lasts.iloc[-1].date(), "is only partly published; using", _cut.date())
    frames = {s: df[df.index <= _cut] for s, df in frames.items()}
latest = max(df.index[-1] for df in frames.values())
try:
    _pa = json.load(open(f"{OUT}/themes.json")).get("asOf")
    if _pa and _pa < _now_ist.strftime("%Y-%m-%d") and latest.strftime("%Y-%m-%d") < _pa:
        sys.exit(f"Yahoo returned data only up to {latest:%Y-%m-%d}, older than the published {_pa} - keeping the published data, nothing written")
except (FileNotFoundError, ValueError):
    pass
fe, closes = {}, {}
for s, df in frames.items():
    if len(df) < 150 or (latest - df.index[-1]).days > 7: continue        # need ~7 months of history (newer listings are scored on the periods they have), still trading
    o, h, l, c, v = (df[k].values.astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    v = np.nan_to_num(v)
    price, prev = c[-1], c[-2]
    ma150, ma200 = sma(c, 150), sma(c, 200)
    ma200_prev = float(np.mean(c[-221:-21])) if len(c) >= 221 else np.nan
    stage = 2 if (price > ma150 > ma200 and ma200 > ma200_prev) else 4 if (price < ma150 and price < ma200) else 1 if price < ma200 else 3
    # days since the stock entered Stage 2 (99 when not in Stage 2)
    ns2 = 99
    if stage == 2:
        ns2 = 0
        for back in range(1, 60):
            e = len(c) - back
            if e < 221: break
            m150, m200, m200p = float(np.mean(c[e - 150:e])), float(np.mean(c[e - 200:e])), float(np.mean(c[e - 220:e - 20]))
            if c[e - 1] > m150 > m200 and m200 > m200p: ns2 = back
            else: break
    f = dict(symbol=s, name=(info[s].name if info[s].name else s), sector=info[s].sector or "",
             price=round(float(price), 2), change=round(float((price / prev - 1) * 100), 2), stage=stage, ns2=ns2,
             fromHigh=round(float((price / np.max(h[-252:]) - 1) * 100), 1),
             fromAth=round(float((price / np.max(h) - 1) * 100), 1),
             nh=int(len(h) > 252 and h[-1] >= np.max(h[-253:-1])),            # hit a new 52-week high today (intraday high)
             turnover=round(float(np.mean(c[-20:] * v[-20:]) / 1e7), 1))
    if BAND.get(s): f["pb"] = BAND[s]
    f.update(pattern_fields(o, h, l, c, v)); f.update(flag_cup_my(h, l, c))
    f["tw"] = weekly_tight(df.Close)
    f.update(cpr_fields(df)); f.update(entry_fields(df, f))
    # trend-template pieces that do not depend on RS
    lo52, hi52 = float(np.min(l[-252:])), float(np.max(h[-252:])); ma50 = sma(c, 50)
    _ttc = [price > ma150 and price > ma200, ma150 > ma200, ma200 > ma200_prev, ma50 > ma150 and ma50 > ma200,
            price > ma50, price >= 1.3 * lo52, price >= 0.75 * hi52]
    f["ttc"] = [int(bool(x)) for x in _ttc]          # the 7 trend-template checks (the 8th is RS >= 70)
    f["_tt"] = sum(f["ttc"])
    f["_sma50"] = ma50
    fe[s] = f; closes[s] = df.Close
C = pd.DataFrame(closes).sort_index().ffill()
print("scored stocks:", len(fe))

# ---------------------------------------------------------------- RS rating (weighted return, ranked 1-99)
def ret(n): return C / C.shift(n) - 1
R21, R63, R126, R189, R252 = ret(21), ret(63), ret(126), ret(189), ret(252)
# 40% 3M + 20% 6M + 20% 9M + 20% 12M; a stock listed less than a year ago uses the periods it has (weights rescaled to add up to 1)
_RW = ((R63, 0.4), (R126, 0.2), (R189, 0.2), (R252, 0.2))
RAW = (sum(r.fillna(0) * w for r, w in _RW) / sum(r.notna() * w for r, w in _RW)).where(R63.notna())
# like the reference: ranked against the Nifty Total Market stocks that actually trade (20-day average traded value >= Rs 6 cr; fitted
# against 95 stock ratings and 35 sector/theme medians copied from the reference - the very thin stocks are left out of the ranking)
RS_MIN_TURNOVER = 0                                                 # rank against every Nifty Total Market stock (fits the reference's RS best)
_liq_tm = [s for s in C.columns if s in TM and s in fe and fe[s]["turnover"] >= RS_MIN_TURNOVER]
uni = _liq_tm if len(_liq_tm) >= 300 else ([s for s in C.columns if s in TM] if len(TM & set(C.columns)) >= 300 else list(C.columns))
print("RS ranked against", len(uni), "stocks")

def pct_of(series_now, series_ref):
    ref = np.sort(series_ref.dropna().values)
    out = {}
    for s, x in series_now.items():
        out[s] = np.nan if (x != x or len(ref) == 0) else (np.searchsorted(ref, x, side="left") + np.searchsorted(ref, x, side="right")) / 2 / len(ref)
    return pd.Series(out)
rs_of = lambda p: np.clip(np.round(1 + 98 * p), 1, 99)

def rs_at(offset):
    row = RAW.iloc[-1 - offset]
    return rs_of(pct_of(row, row[uni]))
RS_NOW, RS_1M = rs_at(0), rs_at(21)
RS_HIST = [rs_at(k) for k in range(0, 12)]                          # last 12 sessions, for "RS >= 90 for n days"
P = {k: pct_of(r.iloc[-1], r.iloc[-1][uni]) for k, r in (("r1", R21), ("r3", R63), ("r6", R126), ("r12", R252))}
idx_close = bench["Close"].reindex(C.index).ffill() if bench is not None else None
idx_r63 = float(idx_close.iloc[-1] / idx_close.iloc[-64] - 1) * 100 if idx_close is not None and idx_close.notna().sum() > 70 else 0.0
liq = lambda f: f["turnover"] >= 10

def safe(x): return 0.0 if x != x else x
rows = []
for s, f in fe.items():
    rs = RS_NOW.get(s, np.nan)
    if rs != rs: continue
    r = C[s]
    rs1m = RS_1M.get(s, np.nan); rs1m = rs if rs1m != rs1m else rs1m
    f["rs"] = int(rs); f["rsd"] = int(rs - rs1m)
    _rw = RS_HIST[5].get(s, np.nan); f["rsw"] = 0 if _rw != _rw else int(rs - _rw)       # RS change over the last week (5 sessions)
    f["ret3"] = safe(round(float(R63[s].iloc[-1]) * 100, 1)); f["ret6"] = safe(round(float(R126[s].iloc[-1]) * 100, 1))
    f["ret1"] = safe(round(float(R252[s].iloc[-1]) * 100, 1)); f["r1"] = safe(round(float(R21[s].iloc[-1]) * 100, 1))
    f["ret1w"] = safe(round(float(C[s].iloc[-1] / C[s].iloc[-6] - 1) * 100, 1))
    f["bi"] = round(f["ret3"] - idx_r63, 1)
    for k, name in (("r1", "rs1"), ("r3", "rs3"), ("r6", "rs6"), ("r12", "rs12")): f[name] = (lambda v: int(rs) if v != v else int(v))(rs_of(P[k][s]))
    _pv = [P[k][s] for k in P if P[k][s] == P[k][s]]
    f["mom"] = round(float(np.mean(_pv)) * 98 + 1, 1) if _pv else float(rs)           # 25% each of 1M/3M/6M/12M percentiles
    f["tr"] = int(f.pop("_tt") + (1 if rs >= 70 else 0)); f["trend"] = f["tr"]
    f["mt"] = int(min(f["rs1"], f["rs3"], f["rs6"]) >= 80)
    streak = 0
    for k in range(12):
        if RS_HIST[k].get(s, 0) >= 90: streak += 1
        else: break
    if 1 <= streak <= 10: f["rn"] = streak                                             # RS has been >= 90 for n sessions
    if idx_close is not None:
        ratio = (r / idx_close).dropna()
        if len(ratio) > 252: f["rsH"] = int(ratio.iloc[-1] >= ratio.iloc[-252:].max() * 0.999)
    f.pop("_sma50", None)
    if TM: f["tm"] = int(s in TM)
    if INDEX_OK:
        f["indices"] = [n for n, m in IDX.items() if s in m]; f["indexMembershipVerified"] = True
    if FNO: f["fno"] = s in FNO
    rows.append(f)

# ---------------------------------------------------------------- optional: NSE delivery data
try:
    dates = list(C.index[-21:])
    frames_d = []
    for dte in dates:
        url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dte.strftime('%d%m%Y')}.csv"
        try:
            x = get_csv(url); x.columns = [c.strip() for c in x.columns]
            x = x[x["SERIES"].str.strip().isin(["EQ", "BE"])][["SYMBOL", "DELIV_QTY", "DELIV_PER"]].copy()
            x["SYMBOL"] = x["SYMBOL"].str.strip(); x["DELIV_QTY"] = pd.to_numeric(x["DELIV_QTY"], errors="coerce")
            x["DELIV_PER"] = pd.to_numeric(x["DELIV_PER"], errors="coerce"); x["d"] = dte; frames_d.append(x)
        except Exception: pass
    if len(frames_d) >= 15:
        D = pd.concat(frames_d); print("delivery data days:", len(frames_d))
        for s, g in D.groupby("SYMBOL"):
            g = g.sort_values("d")
            if len(g) < 15 or g["d"].iloc[-1] != dates[-1]: continue
            tgt = next((r for r in rows if r["symbol"] == s), None)
            if tgt is None: continue
            q, p = g["DELIV_QTY"].values, g["DELIV_PER"].values
            avg20 = np.nanmean(q[-21:-1])
            if avg20 and avg20 == avg20: tgt["deliveryQty"] = float(q[-1]); tgt["deliveryQtyAvg20"] = float(avg20)
            tgt["deliveryPct"] = round(float(p[-1]), 1); tgt["deliveryPct5D"] = round(float(np.nanmean(p[-5:])), 1)
            tgt["deliveryPct20D"] = round(float(np.nanmean(p[-20:])), 1)
    else:
        print("delivery data not available (NSE blocked or too few days) - delivery screens will stay empty")
except Exception as e:
    print("delivery step skipped:", e)

# ---------------------------------------------------------------- themes (Swing Trade tab)
custom = {}
if os.path.exists("themes.csv"):
    t = pd.read_csv("themes.csv", comment="#").fillna("")
    for r in t.to_dict("records"): custom.setdefault(str(r["theme"]).strip(), []).append(str(r["symbol"]).strip().upper())
by = {r["symbol"]: r for r in rows}
valid_custom = {nm: mem for nm, mem in custom.items() if sum(1 for s in set(mem) if s in by and liq(by[s])) >= 5}
groups = {}
if len(valid_custom) >= 3:    # your own themes.csv lists drive the Swing Trade tab (like the reference's hand-made themes)
    groups = dict(valid_custom)
else:                         # too few usable custom themes: fall back to NSE sectors, plus whatever themes you listed
    for r in rows:
        if r["sector"]: groups.setdefault(r["sector"], []).append(r["symbol"])
    for name, mem in custom.items(): groups[name] = mem
themes = []
for name, mem in groups.items():
    ms = [by[s] for s in dict.fromkeys(mem) if s in by and liq(by[s])]
    if len(ms) < 5: continue
    medrs = float(np.median([m["rs"] for m in ms]))
    breadth = 100 * np.mean([m["p50"] is not None and m["p50"] > 0 for m in ms])
    allm = [by[s] for s in dict.fromkeys(mem) if s in by]          # persistence is measured over every stock in the theme (like the reference), not only the liquid ones
    persist = 100 * np.mean([m["rs3"] >= 70 and m["rs6"] >= 70 for m in allm])
    strong = sum(m["rs"] >= 85 for m in ms)
    vs = float(np.median([m["bi"] for m in ms]))
    themes.append(dict(name=name, score=int(round(0.4 * medrs + 0.3 * breadth + 0.3 * persist)), rs=round(medrs),
                       breadth=int(round(breadth)), persist=int(round(persist)),
                       vs=("+" if vs >= 0 else "−") + f"{abs(vs):.0f}%", count=f"{strong} of {len(ms)} RS ≥ 85",
                       members=[m["symbol"] for m in ms]))
themes.sort(key=lambda x: -x["score"])
# full member lists (every scored stock in the theme, liquid or not) for the Workspace sector/theme filter
lists = {nm: [s for s in dict.fromkeys(mem) if s in by] for nm, mem in groups.items()}
for nm, mem in custom.items(): lists.setdefault(nm, [s for s in dict.fromkeys(mem) if s in by])

# ---------------------------------------------------------------- Sector Rotation (sectors and themes)
# score = 60% median RS + 20% liquid members above 50 DMA + 20% above 200 DMA, pulled toward 50 for small groups (n / (n + 5.5));
# quadrant = median 3-month return vs the index (outperforming or not) and the median RS change over the last month (rising or falling)
def _idx_ret(n):
    try: return float(idx_close.iloc[-1] / idx_close.iloc[-1 - n] - 1) * 100
    except Exception: return 0.0
IDX_R21, IDX_R126 = _idx_ret(21), _idx_ret(126)
def group_metrics(name, mem, k0=5.5, kmin=5, tm_only=False):
    allm = [by[s] for s in dict.fromkeys(mem) if s in by]
    ms = [m for m in allm if liq(m) and (not tm_only or m.get("tm", 1) == 1)]       # sectors: only liquid Nifty Total Market members, like the reference
    if len(ms) < kmin: return None
    n = len(ms); med = float(np.median([m["rs"] for m in ms]))
    a50 = 100 * np.mean([m.get("a50") == 1 for m in ms]); a200 = 100 * np.mean([m.get("a200") == 1 for m in ms])
    score = 50 + (0.6 * med + 0.2 * a50 + 0.2 * a200 - 50) * n / (n + k0)
    ch = float(np.median([m.get("rsd", 0) for m in ms])); x3 = float(np.median([m["bi"] for m in ms]))
    quad = ("Leading" if ch >= 0 else "Weakening") if x3 >= 0 else ("Improving" if ch >= 0 else "Lagging")
    top = sorted(ms, key=lambda m: -m["rs"])[:3]
    return dict(name=name, score=int(round(score)), rs=round(med, 1), rsd=round(ch, 1), quad=quad, n=n, nall=len(allm),
                x1=round(float(np.median([m["r1"] for m in ms])) - IDX_R21, 1), x3=round(x3, 1),
                x6=round(float(np.median([m["ret6"] for m in ms])) - IDX_R126, 1),
                adv=sum(m["change"] > 0 for m in ms), dec=sum(m["change"] < 0 for m in ms),
                aadv=sum(m["change"] > 0 for m in allm), adec=sum(m["change"] < 0 for m in allm),
                top=[[m["symbol"], m["rs"]] for m in top], small=n < 10, members=[m["symbol"] for m in ms])
def rank_all(items, key):
    items = [i for i in items if i]; items.sort(key=lambda i: (-i["score"], -i["rs"]))
    for r, i in enumerate(items, 1): i["rank"] = r
    hp = f"{OUT}/sector_history.json"
    try: hist = json.load(open(hp))
    except Exception: hist = {}
    today = dt.datetime.strptime(C.index[-1].strftime("%Y-%m-%d"), "%Y-%m-%d")
    old = [d for d in hist if (today - dt.datetime.strptime(d, "%Y-%m-%d")).days >= 3]
    prev = hist.get(max((d for d in old if (today - dt.datetime.strptime(d, "%Y-%m-%d")).days <= 40), default=None) or (min(old) if old else ""), {})
    _today = C.index[-1].strftime("%Y-%m-%d"); _early = [d for d in hist if d < _today]; _pd = max(_early) if _early else None
    for i in items:
        pr = prev.get(key + ":" + i["name"]); i["prev"] = (pr - i["rank"]) if pr else None
        i["pq"] = hist.get(_pd, {}).get("Q" + key + ":" + i["name"]) if _pd else None
    hist[C.index[-1].strftime("%Y-%m-%d")] = {**hist.get(C.index[-1].strftime("%Y-%m-%d"), {}), **{**{key + ":" + i["name"]: i["rank"] for i in items}, **{"Q" + key + ":" + i["name"]: i["quad"] for i in items}}}
    for d in sorted(hist)[:-90]: hist.pop(d, None)
    json.dump(hist, open(hp, "w"), separators=(",", ":"))
    return items
sec_groups = {}
for r in rows:
    if r.get("sector"): sec_groups.setdefault(r["sector"], []).append(r["symbol"])
# NSE sector and thematic indices (Nifty Bank, Nifty IPO, Nifty Capital Markets ...): the reference lists these next to the hand-made sectors/themes.
# Each index is read from NSE's constituent file; any file that cannot be fetched is skipped (see the "idx loaded" line in the log).
import re as _re
from concurrent.futures import ThreadPoolExecutor as _TPE
_IDX_SEC = ["Telecommunications", "Pharma", "Media", "Midsmall Healthcare", "Healthcare", "Midsmall IT & Telecom", "Capital Goods", "Metal",
            "Commercial & Transport", "Midsmall Financial Services", "Retail", "Consumer Services", "Private Bank", "Chemicals", "PSU Bank", "Bank",
            "Consumer Durables", "IT", "Auto", "FMCG", "Realty", "Oil & Gas", "Financial Services", "Services Sector", "Cement & Construction"]
_IDX_THM = ["IPO", "India Defence", "Capital Markets", "Sugar & Ethanol", "AI Catalysts", "India Internet", "Small Finance Banks & NBFC", "India Manufacturing",
            "Energy", "India Digital", "India Infrastructure & Logistics", "MNC", "Mobility", "EV & New Age Automotives", "India New Age Consumer",
            "Transportation & Logistics", "Non-Cyclical Consumer", "India Tourism", "Housing", "Infrastructure", "PSE", "India Consumption", "Commodities"]
_IDX_FILE = {"Financial Services": ["financelist", "financialserviceslist"], "Private Bank": ["pvtbanklist", "privatebanklist"], "Oil & Gas": ["oilgaslist"],
             "Telecommunications": ["telecomlist", "telecommunicationlist"], "Services Sector": ["servicelist", "serviceslist", "servicessectorlist"]}
def _idx_load(item):
    kind, nm = item
    base = _re.sub(r"[^a-z0-9]", "", nm.lower())
    slugs = [base, base.replace("india", ""), _re.sub(r"[^a-z0-9]", "", nm.lower().replace("&", "and"))]
    cands = []
    for sl in dict.fromkeys(slugs):
        for tail in ("list", "_list"):
            cands.append(f"ind_nifty{sl}{tail}"); cands.append(f"ind_nifty_{sl}{tail}")
    for extra in _IDX_FILE.get(nm, []): cands.insert(0, "ind_nifty" + extra)
    for fn in dict.fromkeys(cands):
        try:
            c = pd.read_csv(io.BytesIO(urllib.request.urlopen(urllib.request.Request(f"https://nsearchives.nseindia.com/content/indices/{fn}.csv", headers=H), timeout=15).read()))
            c.columns = [str(x).strip() for x in c.columns]
            col = next((x for x in c.columns if x.lower() == "symbol"), None)
            if col is None: continue
            syms = [str(x).strip().upper() for x in c[col].dropna() if str(x).strip()]
            if len(syms) >= 5: return kind, "Nifty " + nm, syms
        except Exception:
            continue
    return None
idx_sec, idx_thm = {}, {}
try:
    with _TPE(8) as _ex:
        for res in _ex.map(_idx_load, [("S", n) for n in _IDX_SEC] + [("T", n) for n in _IDX_THM]):
            if res: (idx_sec if res[0] == "S" else idx_thm)[res[1]] = res[2]
except Exception as e:
    print("NSE index step skipped:", e)
print("idx loaded:", len(idx_sec), "of", len(_IDX_SEC), "sector indices,", len(idx_thm), "of", len(_IDX_THM), "thematic indices")
print("idx sector files:", sorted(idx_sec)); print("idx thematic files:", sorted(idx_thm))
# groups copied from the reference site's Workspace (index_groups.csv: symbol,group,kind). An index that NSE's own file already supplied is left as NSE gave it.
if os.path.exists("index_groups.csv"):
    try:
        _ig = pd.read_csv("index_groups.csv", comment="#").fillna("")
        for _g, _d in _ig.groupby("group", sort=False):
            _nm = str(_g).strip(); _nm = _nm if _nm.lower().startswith("nifty") else "Nifty " + _nm
            _kind = str(_d["kind"].iloc[0]).strip().upper()
            if _nm in idx_sec or _nm in idx_thm: continue
            (idx_sec if _kind == "S" else idx_thm)[_nm] = [str(x).strip().upper() for x in _d["symbol"] if str(x).strip()]
        print("index_groups.csv applied:", _ig["group"].nunique(), "groups")
    except Exception as e:
        print("index_groups.csv skipped:", e)
if "Nifty Services Sector" in idx_sec:        # the reference lists Services Sector among the themes
    idx_thm["Nifty Services Sector"] = idx_sec.pop("Nifty Services Sector")
print("idx totals now:", len(idx_sec), "sector groups,", len(idx_thm), "thematic groups")
sec_all = {**sec_groups, **{k: v for k, v in idx_sec.items() if k not in sec_groups}}
thm_all = {**groups, **{k: v for k, v in idx_thm.items() if k not in groups}}
sector_rot = rank_all([group_metrics(k, v, tm_only=True, kmin=3) for k, v in sec_all.items()], "S")
theme_rot = rank_all([group_metrics(k, v) for k, v in thm_all.items()], "T")
for _i in sector_rot:
    if _i["name"] in idx_sec: _i["idx"] = 1
for _i in theme_rot:
    if _i["name"] in idx_thm: _i["idx"] = 1
ind_groups = {}
if os.path.exists("industries.csv"):      # NSE industry of each stock (copied from the reference), used for the Industry level of Sector Rotation
    try:
        for r_ in pd.read_csv("industries.csv", comment="#").fillna("").to_dict("records"):
            if str(r_["symbol"]).strip() and str(r_["industry"]).strip(): ind_groups.setdefault(str(r_["industry"]).strip(), []).append(str(r_["symbol"]).strip().upper())
    except Exception as e: print("industries.csv skipped:", e)
ind_rot = rank_all([group_metrics(k, v, kmin=3, tm_only=True) for k, v in ind_groups.items()], "I") if ind_groups else []
print("rotation:", len(sector_rot), "sectors,", len(theme_rot), "themes,", len(ind_rot), "industries")

# "Where money is moving": rank of every sector/theme 1 day, 1 week, 3 weeks and 6 weeks ago, rebuilt from the price history
# (same score formula, same liquid members as today, RS and 50/200-day averages as they were on that day)
MOVE_K = {"1D": 1, "1W": 5, "3W": 15, "6W": 30}
_SMA50, _SMA200 = C.rolling(50).mean(), C.rolling(200).mean()
def _rank_then(grp, k, kmin=5, tm_only=False):
    rs_k = rs_at(k); a50 = (C.iloc[-1 - k] > _SMA50.iloc[-1 - k]); a200 = (C.iloc[-1 - k] > _SMA200.iloc[-1 - k]); res = []
    for name, mem in grp.items():
        ms = [s for s in dict.fromkeys(mem) if s in by and liq(by[s]) and (not tm_only or by[s].get("tm", 1) == 1) and s in rs_k.index and rs_k[s] == rs_k[s]]
        if len(ms) < kmin: continue
        n = len(ms); med = float(np.median([rs_k[s] for s in ms]))
        p50 = 100 * np.mean([bool(a50.get(s, False)) for s in ms]); p200 = 100 * np.mean([bool(a200.get(s, False)) for s in ms])
        res.append((name, int(round(50 + (0.6 * med + 0.2 * p50 + 0.2 * p200 - 50) * n / (n + 5.5))), round(med, 1)))
    res.sort(key=lambda x: (-x[1], -x[2]))
    return {nm: r for r, (nm, _, _) in enumerate(res, 1)}
MOVE_SINCE = {}
try:
    for lab, k in MOVE_K.items():
        MOVE_SINCE[lab] = C.index[-1 - k].strftime("%Y-%m-%d")
        for items, grp in ((sector_rot, sec_all), (theme_rot, thm_all), (ind_rot, ind_groups)):
            rk = _rank_then(grp, k, 3 if (grp is ind_groups or grp is sec_all) else 5, tm_only=(grp is sec_all or grp is ind_groups))
            for i in items: i.setdefault("pr", {})[lab] = rk.get(i["name"])
    for items in (sector_rot, theme_rot, ind_rot):
        for i in items:
            p3 = (i.get("pr") or {}).get("3W")
            if p3: i["prev"] = p3 - i["rank"]          # the arrow on each card = rank change over 3 weeks, like the reference
    print("money-moving ranks built for", list(MOVE_SINCE))
except Exception as e:
    print("money-moving step skipped:", e); MOVE_SINCE = {}

# ---------------------------------------------------------------- what changed since the previous trading day
asof_now = C.index[-1].strftime("%Y-%m-%d")
def _bc(r):                      # base-cycle state, same rule as the Stock Screener's Base cycle screen
    pv = r.get("pv")
    if not pv or (r.get("pivotHeldSessions") or 0) < 5: return None
    below50 = r.get("a50") == 0 or (r.get("p50") is not None and r["p50"] < 0)
    if r["price"] <= pv * 0.92 or below50: return "failed"
    if r["price"] > pv: return "broke" if r.get("breakoutToday") else "holding"
    return "basing"
def _st(r):
    tc = r.get("ttc")
    return dict(rs=int(r["rs"]), st=r.get("stage"), bc=_bc(r), t8=bool(isinstance(tc, list) and len(tc) == 7 and all(tc) and r["rs"] >= 70),
                su=bool(r.get("vcp") == 1 or r.get("fl") == 1))
cur_state = {r["symbol"]: _st(r) for r in rows}
_lp, _pp = f"{OUT}/last_state.json", f"{OUT}/prev_state.json"
try: _last = json.load(open(_lp))
except Exception: _last = None
if _last and _last.get("asOf") != asof_now:        # a new trading day: yesterday's snapshot becomes the comparison
    prevs = _last; json.dump(_last, open(_pp, "w"), separators=(",", ":")); json.dump({"asOf": asof_now, "state": cur_state}, open(_lp, "w"), separators=(",", ":"))
else:                                                # same day re-run (or first run): keep comparing with the stored previous day
    try: prevs = json.load(open(_pp))
    except Exception: prevs = None
    if _last is None: json.dump({"asOf": asof_now, "state": cur_state}, open(_lp, "w"), separators=(",", ":"))
PS = prevs["state"] if prevs else {}
if not PS:                                           # first run: at least RS 90+ moves can be told from yesterday's RS
    try: PS = {s: {"rs": int(v)} for s, v in RS_HIST[1].items() if v == v}
    except Exception: PS = {}
_TMOK = any(r.get("tm") == 1 for r in rows)                      # like the reference: liquid Nifty Total Market stocks only
_L = sorted([r for r in rows if liq(r) and (r.get("tm") == 1 or not _TMOK)], key=lambda r: -r["rs"])
def _pick(f): return [r["symbol"] for r in _L if f(r)]
_p = lambda r, k: PS.get(r["symbol"], {}).get(k)
chg_groups = [
    ["New 52-week highs", _pick(lambda r: r.get("nh") == 1)],
    ["Joined RS 90+", _pick(lambda r: r["rs"] >= 90 and _p(r, "rs") is not None and _p(r, "rs") < 90)],
    ["Dropped below RS 90", _pick(lambda r: r["rs"] < 90 and _p(r, "rs") is not None and _p(r, "rs") >= 90)],
    ["Entered Stage 2", _pick(lambda r: r.get("stage") == 2 and "st" in PS.get(r["symbol"], {}) and _p(r, "st") != 2)],
    ["Broke into Stage 4", _pick(lambda r: r.get("stage") == 4 and "st" in PS.get(r["symbol"], {}) and _p(r, "st") != 4)],
    ["Now pass all 8 trend checks", _pick(lambda r: _st(r)["t8"] and "st" in PS.get(r["symbol"], {}) and not _p(r, "t8"))],
    ["Volume breakouts today", _pick(lambda r: r.get("b50") == 1 and (r.get("v") or 0) >= 2)],
    ["Pocket pivots today", _pick(lambda r: r.get("pp") == 1)],
    ["New setups (VCP, flag)", _pick(lambda r: _st(r)["su"] and "st" in PS.get(r["symbol"], {}) and not _p(r, "su"))]]
_hasbc = lambda r: "bc" in PS.get(r["symbol"], {})
bc_chg = {"broke": _pick(lambda r: _hasbc(r) and _p(r, "bc") == "basing" and _bc(r) in ("broke", "holding")),
          "failed": _pick(lambda r: _hasbc(r) and _p(r, "bc") in ("basing", "broke", "holding") and _bc(r) == "failed"),
          "bases": _pick(lambda r: _hasbc(r) and _p(r, "bc") is None and _bc(r) == "basing")}
chg_quads = [f'{i["name"]}: {i["pq"]} → {i["quad"]}' for i in sector_rot if i.get("pq") and i["pq"] != i["quad"]]
json.dump({"asOf": asof_now, "prevAsOf": prevs.get("asOf") if prevs else None, "quads": chg_quads, "groups": chg_groups, "bc": bc_chg},
          open(f"{OUT}/changes.json", "w"), separators=(",", ":"))
print("changes vs", prevs.get("asOf") if prevs else "nothing (first run)", "|", sum(len(g[1]) for g in chg_groups) + len(chg_quads), "items")

# ---------------------------------------------------------------- shares outstanding (market cap), cached for 30 days
import time as _t
from concurrent.futures import ThreadPoolExecutor
_SHP = f"{OUT}/shares.json"
try: _SH = json.load(open(_SHP))
except Exception: _SH = {}
_now = _t.time()
def _get_sh(s):
    try:
        v = yf.Ticker(s + ".NS").fast_info.get("shares")
        return s, (float(v) if v and v == v and v > 0 else None)
    except Exception:
        return s, None
_todo = [s for s in frames if s not in _SH or _now - _SH[s][1] > 30 * 86400]
_t0 = _t.time(); _got = 0
for _i in range(0, len(_todo), 120):
    if _t.time() - _t0 > 900: break                      # 15 minute budget; the rest continues on the next run
    with ThreadPoolExecutor(12) as ex:
        for s, v in ex.map(_get_sh, _todo[_i:_i + 120]):
            if v: _SH[s] = [v, _now]; _got += 1
json.dump(_SH, open(_SHP, "w"), separators=(",", ":"))
SHR = {s: v[0] for s, v in _SH.items()}
print("shares known:", len(SHR), "| fetched now:", _got, "| pending:", len(_todo) - _got)
for _r in rows:
    _s = SHR.get(_r["symbol"])
    if _s: _r["mcap"] = round(_s * _r["price"] / 1e7)

# ---------------------------------------------------------------- upcoming results dates ("Results coming up")
def _res_date(x):
    for f_ in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d/%m/%Y"):
        try: return dt.datetime.strptime(str(x).strip()[:11 if "%b" in f_ and "-" in f_ else 10], f_).date()
        except Exception: pass
    return None
def parse_board_meetings(data, today):
    """NSE board-meeting list -> {symbol: earliest date >= today} for meetings about financial results."""
    items = data.get("data", []) if isinstance(data, dict) else (data or [])
    out = {}
    for e in items:
        if not isinstance(e, dict): continue
        sym = (e.get("bm_symbol") or e.get("symbol") or "").strip()
        d = _res_date(e.get("bm_date") or e.get("date") or "")
        txt = " ".join(str(e.get(k) or "") for k in ("bm_purpose", "purpose", "bm_desc", "desc", "sub")).lower()
        if sym and d and d >= today and "result" in txt and (sym not in out or d < out[sym]): out[sym] = d
    return out
_RESP = f"{OUT}/results.json"
try: _RC = json.load(open(_RESP))
except Exception: _RC = {}
_today_d = C.index[-1].date(); RESD = {}
try:
    import http.cookiejar
    _op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    _hd = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json,*/*", "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"}
    try: _op.open(urllib.request.Request("https://www.nseindia.com/", headers=_hd), timeout=30).read()
    except Exception: pass
    _u = "https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date=%s&to_date=%s" % (_today_d.strftime("%d-%m-%Y"), (_today_d + dt.timedelta(days=200)).strftime("%d-%m-%Y"))
    RESD = parse_board_meetings(json.loads(_op.open(urllib.request.Request(_u, headers=_hd), timeout=90).read()), _today_d)
    print("results dates from NSE board meetings:", len(RESD))
except Exception as _e:
    print("NSE board meetings not available:", str(_e)[:80])
# Yahoo calendar as a backup for strong, liquid stocks that NSE did not list (cached for 7 days)
def _yf_res(s):
    try:
        cal = yf.Ticker(s + ".NS").calendar
        ds = (cal or {}).get("Earnings Date") or []
        ds = [d for d in ds if hasattr(d, "year") and d >= _today_d]
        return s, (min(ds) if ds else None)
    except Exception:
        return s, None
_want = [r["symbol"] for r in rows if r["symbol"] not in RESD and r.get("rs", 0) >= 60 and r.get("turnover", 0) >= 10]
_yf_todo = []
for _s in _want:
    _c = _RC.get(_s)
    _d = _res_date(_c[0]) if _c and _c[0] else None
    if _c and _t.time() - _c[1] < 7 * 86400 and (_d is None or _d >= _today_d):
        if _d: RESD[_s] = _d
    else: _yf_todo.append(_s)
_t0 = _t.time(); _yf_got = 0
for _i in range(0, len(_yf_todo), 60):
    if _t.time() - _t0 > 420: break                                    # 7 minute budget
    with ThreadPoolExecutor(8) as ex:
        for s, d in ex.map(_yf_res, _yf_todo[_i:_i + 60]):
            _RC[s] = [d.isoformat() if d else "", _t.time()]
            if d: RESD[s] = d; _yf_got += 1
json.dump(_RC, open(_RESP, "w"), separators=(",", ":"))
for _r in rows:
    _d = RESD.get(_r["symbol"])
    if _d: _r["rdate"] = _d.isoformat(); _r["nr"] = (_d - _today_d).days
print("results dates known:", len(RESD), "| from Yahoo now:", _yf_got, "| checked:", len(_yf_todo))

# ---------------------------------------------------------------- gap-ups and recent IPOs (screener screens)
gaps = []
for s_, df in frames.items():
    tail = df.tail(130)
    o_, c_ = tail.Open.values, tail.Close.values
    _tv = float(np.mean(c_[-20:] * tail.Volume.fillna(0).values[-20:]) / 1e7)
    if s_ in SHR and SHR[s_] * float(c_[-1]) / 1e7 < 300: continue      # screen is for market cap 300 cr and above
    if s_ not in SHR and _tv < 5: continue
    for i in range(1, len(tail)):
        if c_[i - 1] > 0 and o_[i] / c_[i - 1] - 1 >= 0.02 and (len(df) - 130 + i) > 5:
            gaps.append(dict(symbol=s_, date=tail.index[i].strftime("%Y-%m-%d"), open=round(float(o_[i]), 2),
                             prevClose=round(float(c_[i - 1]), 2), close=round(float(c_[i]), 2),
                             **({"marketCapCr": round(SHR[s_] * float(c_[i - 1]) / 1e7)} if s_ in SHR else {})))
ipos = []
cut = latest - pd.Timedelta(days=366)
for s_, df in frames.items():
    ldt = LISTED.get(s_)
    first = df.index[0]
    if ldt is None or ldt != ldt: ldt = first if first > pd.Timestamp("2019-02-01") else None
    if ldt is None or ldt < cut or len(df) < 3: continue
    post = df[df.index >= ldt] if (df.index >= ldt).any() else df
    c_ = post.Close.values; hi_ = post.High.values
    lc, last_ = float(c_[0]), float(c_[-1])
    ma50_ = float(np.mean(c_[-50:])) if len(c_) >= 20 else last_
    rng_ = (hi_[-50:].max() - post.Low.values[-50:].min()) / hi_[-50:].max() * 100
    fromhi = (last_ / hi_.max() - 1) * 100
    val_ = float(np.mean(post.Close.values[-50:] * post.Volume.fillna(0).values[-50:]) / 1e7)
    ipos.append(dict(symbol=s_, name=info[s_].name if s_ in info else s_, ld=ldt.strftime("%Y-%m-%d"), lc=round(lc, 2), last=round(last_, 2),
                     since=round((last_ / lc - 1) * 100, 1), fromHi=round(fromhi, 1),
                     m1=round((last_ / c_[-22] - 1) * 100, 1) if len(c_) > 22 else None, val=round(val_, 1),
                     base=bool(rng_ <= 15 and fromhi >= -15 and last_ > ma50_), inScan=bool(val_ >= 10 and fromhi >= -25)))
print("gap records:", len(gaps), "| recent IPOs:", len(ipos))

# ---------------------------------------------------------------- monthly top 100 (calendar-month returns)
try:
    _me = C.resample("ME").last()
    _lastd = C.groupby(C.index.to_period("M")).apply(lambda x: x.index[-1])
    _mret = _me.pct_change(fill_method=None) * 100; _pm = _me.shift(1)
    _V = pd.DataFrame({s: frames[s].Volume for s in C.columns if s in frames}).reindex(C.index)
    _TV = ((C[_V.columns] * _V).rolling(20, min_periods=10).mean() / 1e7).resample("ME").last().shift(1)   # traded value at the start of each month
    _elig = [s for s in C.columns if s in fe and s in _V.columns]
    monthly = {"asOf": C.index[-1].strftime("%Y-%m-%d"), "months": [], "data": {}}
    for ts in _mret.index[1:]:
        key = ts.strftime("%Y-%m"); col = _mret.loc[ts, _elig].dropna()
        if len(col) < 50: continue
        col = col[[s for s in col.index if key not in SPLIT_M.get(s, ()) and _TV.loc[ts, s] == _TV.loc[ts, s] and _TV.loc[ts, s] >= 0.5]]
        top = col.sort_values(ascending=False).head(250)
        monthly["months"].append(key)
        monthly["data"][key] = [[s, round(float(v), 1), round(float(_me.loc[ts, s]), 2), round(float(_TV.loc[ts, s]), 1) if _TV.loc[ts, s] == _TV.loc[ts, s] else 0,
                                 (round(SHR[s] * float(_pm.loc[ts, s]) / 1e7) if s in SHR and _pm.loc[ts, s] == _pm.loc[ts, s] else None)] for s, v in top.items()]
    json.dump(monthly, open(f"{OUT}/monthly.json", "w"), separators=(",", ":"), allow_nan=False)
    print("monthly months:", len(monthly["months"]))
except Exception as _e:
    print("monthly skipped:", _e)

# ---------------------------------------------------------------- write
rows.sort(key=lambda r: (-r["rs"], -r["ret3"]))
for i, r in enumerate(rows, 1): r["rank"] = i
def clean(v):
    if isinstance(v, (np.integer,)): return int(v)
    if isinstance(v, (np.floating, float)): return None if not math.isfinite(v) else round(float(v), 2)
    if isinstance(v, list): return [clean(x) for x in v]
    return v
rows = [{k: clean(v) for k, v in r.items() if clean(v) is not None} for r in rows]
asof = C.index[-1].strftime("%Y-%m-%d")
json.dump(rows, open(f"{OUT}/summary.json", "w"), separators=(",", ":"), allow_nan=False)
json.dump(gaps, open(f"{OUT}/gaps.json", "w"), separators=(",", ":"), allow_nan=False)
json.dump(ipos, open(f"{OUT}/ipos.json", "w"), separators=(",", ":"), allow_nan=False)
json.dump({"asOf": asof, "themes": themes, "lists": lists}, open(f"{OUT}/themes.json", "w"), separators=(",", ":"), allow_nan=False)
json.dump({"asOf": asof, "since": MOVE_SINCE, "sectors": sector_rot, "themes": theme_rot, "industries": ind_rot}, open(f"{OUT}/sectors.json", "w"), separators=(",", ":"), allow_nan=False)
json.dump({"asOf": asof, "updated": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(), "generated": dt.datetime.utcnow().isoformat() + "Z",
           "quotes": {r["symbol"]: {"p": r["price"], "c": r["change"]} for r in rows}},
          open(f"{OUT}/quotes.json", "w"), separators=(",", ":"), allow_nan=False)
json.dump({"dates": [t.strftime("%Y-%m-%d") for t in C.index],
           "stocks": {s: {"sec": info[s].sector if s in info else "",
                          "c": [None if pd.isna(v) else round(float(v), 2) for v in C[s]]} for s in uni if s in by},
           "bench": ({"n500": [None if pd.isna(v) else round(float(v), 2) for v in bench.Close.reindex(C.index).ffill()]} if bench is not None else {})},      # ranking universe only: keeps the file under Cloudflare's 25 MB per-file limit
          open(f"{OUT}/history.json", "w"), separators=(",", ":"), allow_nan=False)
# last ~1 year of daily closes for every scored stock: powers the Pattern Finder
rc = C.iloc[-260:]
json.dump({"dates": [d.strftime("%Y-%m-%d") for d in rc.index],
           "stocks": {s: [None if pd.isna(v) else round(float(v), 2) for v in rc[s]] for s in rc.columns if s in by}},
          open(f"{OUT}/recent.json", "w"), separators=(",", ":"), allow_nan=False)
print("done:", len(rows), "stocks,", len(themes), "themes, as of", asof)
# which stocks sit in each Popular Scan today and in the previous run's day (for "New" tags and "Dropped since ..." lines)
def _scan_sets(rs_):
    def ok(x): return x.get("turnover") is None or x.get("turnover", 0) >= 10
    rules = {
        "darvas": lambda x: x.get("dvb") == 1 and x.get("rs", 0) >= 70,
        "inside": lambda x: x.get("dvi") == 1 and x.get("rs", 0) >= 80 and (x.get("dvh") is None or x.get("dvh") <= 15),
        "near": lambda x: x.get("pv") is not None and x["pv"] >= x["price"] and (x["pv"] - x["price"]) / x["pv"] * 100 <= 5 and x.get("rng10") is not None and x["rng10"] <= 15 and bool(x.get("a50")) and x.get("tr", 0) >= 6 and x.get("rs", 0) >= 80,
        "vcp": lambda x: x.get("vcp") == 1 and x.get("rs", 0) >= 70,
        "cup": lambda x: x.get("cup") == 1 and x.get("rs", 0) >= 60,
        "pocket": lambda x: x.get("pp") == 1 and x.get("rs", 0) >= 70,
        "flags": lambda x: x.get("fl") == 1 and x.get("rs", 0) >= 60,
        "cprn": lambda x: x.get("cpn") == 1 and (x.get("e21") or -1) > 0 and 55 <= (x.get("rsi") or 0) <= 75,
        "wr1": lambda x: x.get("wnar") == 1 and x.get("wbrk") == 1 and (x.get("rsi") or 0) >= 55 and (x.get("e21") or -1) > 0,
        "mr1": lambda x: x.get("mnar") == 1 and x.get("mbrk") == 1 and (x.get("rsi") or 0) >= 55}
    srt = sorted(rs_, key=lambda x: (-x.get("rs", 0), -(x.get("mom") or 0)))
    return {k: [x["symbol"] for x in srt if ok(x) and fn(x)] for k, fn in rules.items()}
try:
    _sp = f"{OUT}/scans.json"; _cs = _scan_sets(rows)
    try: _old = json.load(open(_sp))
    except Exception: _old = None
    if _old and _old.get("asOf") != asof: _prevd, _prevs = _old.get("asOf"), _old.get("cur")
    elif _old: _prevd, _prevs = _old.get("prevAsOf"), _old.get("prev")
    else: _prevd, _prevs = None, None
    json.dump({"asOf": asof, "prevAsOf": _prevd, "cur": _cs, "prev": _prevs}, open(_sp, "w"), separators=(",", ":"))
except Exception as _e: print("scans.json skipped:", _e)
# freshness / health file shown on the website
_trend = None
try:   # like the reference: Uptrend = benchmark above its 50 and 200-day averages, Downtrend = below both, otherwise Mixed
    _bc = idx_close.dropna(); _px = float(_bc.iloc[-1]); _m50 = float(_bc.iloc[-50:].mean()); _m200 = float(_bc.iloc[-200:].mean())
    _trend = "Uptrend" if (_px > _m50 and _px > _m200) else ("Downtrend" if (_px < _m50 and _px < _m200) else "Mixed")
except Exception: pass
# Nifty 50 market-regime inputs for the CPR entry scanners. Nifty 50 first; if it is missing, invalid or stale the Nifty 500 benchmark is used and labelled
# as a fallback; if that fails too, nothing is published and the site shows "Market Data Unavailable - Avoid New Buy".
def _regime(nd, src, asof):
    if nd is None: return None
    nd = nd.dropna(subset=["Close", "High", "Low"])
    if len(nd) < 230: return None
    nd = nd[nd.index <= pd.Timestamp(asof)]                                    # never use a bar newer than the stock data
    if len(nd) < 230: return None
    if (pd.Timestamp(asof) - nd.index[-1]).days > 3:
        print("market regime:", src, "is stale (", nd.index[-1].date(), ")"); return None
    c, h, l = (nd[k].values.astype(float) for k in ("Close", "High", "Low"))
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    pdm = np.where((h[1:] - h[:-1] > l[:-1] - l[1:]) & (h[1:] - h[:-1] > 0), h[1:] - h[:-1], 0.0)
    mdm = np.where((l[:-1] - l[1:] > h[1:] - h[:-1]) & (l[:-1] - l[1:] > 0), l[:-1] - l[1:], 0.0)
    def w(x):
        o = np.zeros(len(x)); o[13] = x[:14].sum()
        for i in range(14, len(x)): o[i] = o[i - 1] - o[i - 1] / 14 + x[i]
        return o[13:]
    a, p_, m_ = w(tr), w(pdm), w(mdm)
    dx = 100 * abs(p_ - m_) / np.where((p_ + m_) == 0, 1, (p_ + m_))
    adx = float(np.mean(dx[:14]))
    for v in dx[14:]: adx = (adx * 13 + v) / 14
    vals = [float(c[-1]), float(c[-50:].mean()), float(c[-200:].mean()), float(c[-70:-20].mean()), float(adx)]
    if not all(np.isfinite(vals)) or vals[0] <= 0: print("market regime:", src, "has invalid values"); return None
    return {"src": src, "asOf": nd.index[-1].strftime("%Y-%m-%d"), "close": round(vals[0], 2), "sma50": round(vals[1], 2),
            "sma200": round(vals[2], 2), "sma50prev": round(vals[3], 2), "adx": round(vals[4], 1)}
_mkt = None
try:
    _nd = None
    try:
        _nd = yf.download("^NSEI", start="2023-01-01", auto_adjust=False, progress=False)
        if isinstance(_nd.columns, pd.MultiIndex): _nd.columns = _nd.columns.get_level_values(0)
    except Exception as _e: _nd = None
    _mkt = _regime(_nd, "Nifty 50", asof)
    if _mkt is None and bench is not None: _mkt = _regime(bench, "Nifty 500 fallback (Nifty 50 data missing, invalid or stale)", asof)
except Exception as _e:
    print("market regime skipped:", _e)
print("market regime:", _mkt["src"] if _mkt else "NOT AVAILABLE -> site will show Avoid New Buy")
_last = max(df.index[-1] for df in frames.values())
json.dump({"asOf": asof, "lastCandle": _last.strftime("%Y-%m-%d"), "generated": dt.datetime.utcnow().isoformat() + "Z",
           "stocksDownloaded": len(frames), "stocksRequested": len(syms), "stocksScored": len(rows),
           "benchmarkOk": bench is not None, "rsUniverse": len(uni),
           "trend": _trend, "mkt": _mkt, "liquid": sum(1 for r in rows if r.get("turnover", 0) >= 10), "scanned": pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d %H:%M")},
          open(f"{OUT}/meta.json", "w"), separators=(",", ":"))

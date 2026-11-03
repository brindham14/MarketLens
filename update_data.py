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

def cup_handle(h, l, minDepth=.12, maxDepth=.40, minCup=35, maxCup=260, rimTol=.10, minHandle=3, maxHandle=25, maxHD=.12):
    """Port of the chart's CupHandle indicator for a handle that ends on the latest bar."""
    n = len(h); end = n - 1; best = None
    for hlen in range(minHandle, maxHandle + 1):
        hs = end - hlen + 1
        if hs < 1: continue
        hh, hl = float(h[hs:end + 1].max()), float(l[hs:end + 1].min())
        if hh <= 0 or (hh - hl) / hh > maxHD: continue
        hd = (hh - hl) / hh
        for cupLen in range(minCup, min(maxCup, hs) + 1, 2):
            cs, ce = hs - cupLen, hs - 1
            if cs < 0: continue
            rw = max(3, int(cupLen * .22))
            leftAt = cs + int(np.argmax(h[cs:min(ce, cs + rw) + 1])); leftHigh = float(h[leftAt])
            if leftAt + 8 >= ce or ce - 3 < leftAt + 3: continue
            bottomAt = leftAt + 3 + int(np.argmin(l[leftAt + 3:ce - 2])); bottom = float(l[bottomAt])
            if leftHigh <= 0: continue
            depth = (leftHigh - bottom) / leftHigh
            if depth < minDepth or depth > maxDepth: continue
            bp = (bottomAt - leftAt) / max(1, ce - leftAt)
            if bp < .30 or bp > .70: continue
            a2 = max(bottomAt + 3, ce - rw)
            if a2 > ce: continue
            rightHigh = float(h[a2:ce + 1].max())
            if abs(rightHigh - leftHigh) / leftHigh > rimTol: continue
            mid = bottom + (leftHigh - bottom) * .5
            if hl < mid or hl < rightHigh * (1 - maxHD * 1.25): continue
            if hh > max(leftHigh, rightHigh) * (1 + rimTol * .5): continue
            if best is None or cupLen > best["cupLen"]:
                best = dict(depth=depth * 100, hD=hd * 100, cupLen=cupLen, rightHigh=rightHigh)
    return best

def pattern_fields(o, h, l, c, v):
    """Pattern / setup fields for one stock from daily OHLCV (arrays, latest last)."""
    n = len(c); f = {}
    price = c[-1]
    ma50, ma150, ma200 = sma(c, 50), sma(c, 150), sma(c, 200)
    f["a50"] = int(price > ma50) if ma50 == ma50 else 0
    f["a200"] = int(price > ma200) if ma200 == ma200 else 0
    f["p50"] = round((price / ma50 - 1) * 100, 1) if ma50 == ma50 else None
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

def weekly_tight(close_series):
    wc = close_series.resample("W-FRI").last().dropna().values
    t = 0
    for i in range(len(wc) - 1, 0, -1):
        if abs(wc[i] / wc[i - 1] - 1) <= 0.015: t += 1
        else: break
    return t

def flag_cup_my(h, l, c):
    f = {}; n = len(c); price = c[-1]
    # Flag: >=25% run-up into a recent high, then a tight (<=12%) pullback
    if n > 80:
        pk = n - 25 + int(np.argmax(h[-25:])); since = n - 1 - pk
        base_low = float(np.min(l[max(0, pk - 40):pk + 1]))
        pole = (h[pk] / base_low - 1) * 100; fd = (h[pk] - np.min(l[pk:])) / h[pk] * 100
        f["pole"], f["fD"] = round(float(pole), 1), round(float(fd), 1)
        f["fl"] = int(pole >= 25 and fd <= 12 and 3 <= since <= 25 and price >= 0.85 * h[pk])
    # Cup with handle (port of the chart's indicator): 12-40% cup of 7+ weeks, shallow handle in its upper half, still below the right rim
    if n > 200 and price > sma(c, 50):
        cu = cup_handle(h[-400:], l[-400:])
        if cu and -6 <= (price / cu["rightHigh"] - 1) * 100 <= 0:
            f["cup"] = 1; f["cupD"] = round(cu["depth"], 1); f["hD"] = round(cu["hD"], 1); f["pv"] = round(cu["rightHigh"], 2)
            f["pivotHeldSessions"] = max(5, int(cu["cupLen"] // 10)); f["breakoutToday"] = 0
    # Multi-year breakout: closes above the highest high of the last ~3 years (older than 60 sessions)
    if n > 400:
        look = min(n - 1, 756); seg = h[-look:-60]; ci = n - look + int(np.argmax(seg)); C = float(h[ci])
        above = np.where(c[-60:] > C)[0]
        if len(above) and price <= 1.15 * C and price >= C:
            bi_ = n - 60 + int(above[0])
            f["my"] = 1; f["cy"] = round((bi_ - ci) / 252, 1); f["pc"] = round((price / C - 1) * 100, 1)
    # Darvas boxes
    boxes = darvas(h, l)
    if boxes:
        k, j, top, bot = boxes[-1]
        if n - 1 - k <= 150:
            after = c[j + 1:]; st = None
            for q, x in enumerate(after):
                if x < bot: st = "x"; break          # close below the bottom cancels the box
                if x > top: st = ("b", n - 1 - (j + 1 + q)); break
            if st is None: st = ("i", n - 1 - j)
            if st != "x":
                f["dvt"] = round(top, 2); f["dvl"] = round(bot, 2)
                f["dvh"] = round((top - bot) / top * 100, 1)
                if st[0] == "b":
                    if st[1] <= 2: f["dvb"] = 1; f["dvd"] = st[1]
                else:
                    f["dvn"] = 1; f["dva"] = st[1]
                    if price >= top * 0.95: f["dvi"] = 1
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
    if _pa and latest.strftime("%Y-%m-%d") < _pa:
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
             nh=int(len(h) > 252 and h[-1] >= np.max(h[-253:-1])),            # hit a new 52-week high today (intraday high)
             turnover=round(float(np.mean(c[-20:] * v[-20:]) / 1e7), 1))
    if BAND.get(s): f["pb"] = BAND[s]
    f.update(pattern_fields(o, h, l, c, v)); f.update(flag_cup_my(h, l, c))
    f["tw"] = weekly_tight(df.Close)
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
RS_MIN_TURNOVER = 6
_liq_tm = [s for s in C.columns if s in TM and s in fe and fe[s]["turnover"] >= RS_MIN_TURNOVER]
uni = _liq_tm if len(_liq_tm) >= 300 else ([s for s in C.columns if s in TM] if len(TM & set(C.columns)) >= 300 else list(C.columns))
print("RS ranked against", len(uni), "stocks")

def pct_of(series_now, series_ref):
    ref = np.sort(series_ref.dropna().values)
    out = {}
    for s, x in series_now.items():
        out[s] = np.nan if (x != x or len(ref) == 0) else np.searchsorted(ref, x, side="right") / len(ref)
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
sector_rot = rank_all([group_metrics(k, v, tm_only=True) for k, v in sec_groups.items()], "S")
theme_rot = rank_all([group_metrics(k, v) for k, v in groups.items()], "T")
ind_groups = {}
if os.path.exists("industries.csv"):      # NSE industry of each stock (copied from the reference), used for the Industry level of Sector Rotation
    try:
        for r_ in pd.read_csv("industries.csv", comment="#").fillna("").to_dict("records"):
            if str(r_["symbol"]).strip() and str(r_["industry"]).strip(): ind_groups.setdefault(str(r_["industry"]).strip(), []).append(str(r_["symbol"]).strip().upper())
    except Exception as e: print("industries.csv skipped:", e)
ind_rot = rank_all([group_metrics(k, v, kmin=3) for k, v in ind_groups.items()], "I") if ind_groups else []
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
        for items, grp in ((sector_rot, sec_groups), (theme_rot, groups), (ind_rot, ind_groups)):
            rk = _rank_then(grp, k, 3 if grp is ind_groups else 5, tm_only=grp is sec_groups)
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
# freshness / health file shown on the website
_last = max(df.index[-1] for df in frames.values())
json.dump({"asOf": asof, "lastCandle": _last.strftime("%Y-%m-%d"), "generated": dt.datetime.utcnow().isoformat() + "Z",
           "stocksDownloaded": len(frames), "stocksRequested": len(syms), "stocksScored": len(rows),
           "benchmarkOk": bench is not None, "rsUniverse": len(uni)},
          open(f"{OUT}/meta.json", "w"), separators=(",", ":"))

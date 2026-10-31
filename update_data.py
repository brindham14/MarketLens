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

# ---------------------------------------------------------------- prices
def fetch(batch, tick=lambda s: s + ".NS"):
    return yf.download([tick(s) for s in batch], start="2019-01-01", group_by="ticker",
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
            after = c[j + 1:]
            first_above = np.where(after > top)[0]
            if len(first_above) and (j + 1 + first_above[0]) >= n - 5 and price > top: f["dvb"] = 1
            elif not len(first_above) and bot <= price <= top and price >= top * 0.95: f["dvi"] = 1
    return f

# ---------------------------------------------------------------- per-stock features
latest = max(df.index[-1] for df in frames.values())
fe, closes = {}, {}
for s, df in frames.items():
    if len(df) < 260 or (latest - df.index[-1]).days > 7: continue        # need ~1 year of history, still trading
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
             turnover=round(float(np.mean(c[-20:] * v[-20:]) / 1e7), 1))
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
RAW = 0.4 * R63 + 0.2 * R126 + 0.2 * R189 + 0.2 * R252            # 40% 3M + 20% 6M + 20% 9M + 20% 12M
uni = [s for s in C.columns if s in TM] if len(TM & set(C.columns)) >= 300 else list(C.columns)
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

# ---------------------------------------------------------------- gap-ups and recent IPOs (screener screens)
gaps = []
for s_, df in frames.items():
    tail = df.tail(130)
    o_, c_ = tail.Open.values, tail.Close.values
    for i in range(1, len(tail)):
        if c_[i - 1] > 0 and o_[i] / c_[i - 1] - 1 >= 0.03 and (len(df) - 130 + i) > 5:
            gaps.append(dict(symbol=s_, date=tail.index[i].strftime("%Y-%m-%d"), open=round(float(o_[i]), 2),
                             prevClose=round(float(c_[i - 1]), 2), close=round(float(c_[i]), 2)))
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

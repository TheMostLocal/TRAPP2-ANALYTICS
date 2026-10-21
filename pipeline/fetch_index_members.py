#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/fetch_index_members.py
#
#  z136 - who is IN each index we report earnings for, with sector and weight.
#    SPY (S&P 500) and DIA (Dow 30): State Street's daily holdings file (ticker, weight, GICS sector).
#    QQQ (Nasdaq-100): Wikipedia's components table (ticker, GICS sector; weights from market cap
#    later). The S&P 500 falls back to Wikipedia's list if State Street's file fails.
#  -> data/earnings/universe.json { updatedAt, indices: { SPY: { name, source, members: [
#       { ticker, name, sector, weight } ] }, QQQ: {...}, DIA: {...} } }
#  Weekly (or when missing). A fetch that fails keeps the previous members - never an empty index.
# ============================================================
import io
import sys
import urllib.request

from earnings_common import OUT, now_iso, read, sector_of, write, yahoo_symbol, num

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
SSGA = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{}.xlsx"
WIKI = {"SPY": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "QQQ": "https://en.wikipedia.org/wiki/Nasdaq-100",
        "DIA": "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"}
NAMES = {"SPY": "S&P 500", "QQQ": "Nasdaq-100", "DIA": "Dow Jones 30"}


def log(*a):
    print("[members]", *a, flush=True)


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


def from_ssga(etf):
    import pandas as pd
    raw = pd.read_excel(io.BytesIO(get(SSGA.format(etf.lower()))), header=None)
    hdr = next(i for i in range(min(15, len(raw))) if "Ticker" in [str(x).strip() for x in raw.iloc[i].tolist()])
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(x).strip() for x in raw.iloc[hdr].tolist()]
    out = []
    for _, r in df.iterrows():
        t = yahoo_symbol(r.get("Ticker"))
        w = num(r.get("Weight"))
        if not t or t in ("-", "NAN", "CASH_USD") or w is None or w <= 0:
            continue
        out.append({"ticker": t, "name": str(r.get("Name") or "").strip(), "sector": sector_of(r.get("Sector")), "weight": round(w / 100, 8)})
    return out


def from_wiki(etf):
    import pandas as pd
    tables = pd.read_html(io.BytesIO(get(WIKI[etf])))
    for tb in tables:
        cols = {str(c).strip().lower(): c for c in tb.columns}
        tc = cols.get("symbol") or cols.get("ticker")
        if tc is None or len(tb) < (25 if etf == "DIA" else 90):
            continue
        sc = cols.get("gics sector") or cols.get("sector") or cols.get("industry")
        nc = cols.get("security") or cols.get("company") or cols.get("name")
        out = [{"ticker": yahoo_symbol(r[tc]), "name": str(r[nc]).strip() if nc is not None else "",
                "sector": sector_of(r[sc]) if sc is not None else None, "weight": None} for _, r in tb.iterrows()]
        return [m for m in out if m["ticker"]]
    return []


def main():
    old = read(OUT / "universe.json", {}) or {}
    idx = dict(old.get("indices") or {})
    for etf in ("SPY", "DIA", "QQQ"):
        members, source = [], None
        if etf in ("SPY", "DIA"):
            try:
                members, source = from_ssga(etf), "State Street daily holdings"
            except Exception as e:
                log(f"{etf}: State Street failed ({e}) - trying Wikipedia")
        if len(members) < (25 if etf == "DIA" else 90):
            try:
                members, source = from_wiki(etf), "Wikipedia components list"
            except Exception as e:
                log(f"{etf}: Wikipedia failed ({e})")
        # fill a missing sector from the S&P list (Dow / Nasdaq names are mostly S&P members)
        if members:
            idx[etf] = {"name": NAMES[etf], "source": source, "updatedAt": now_iso(), "members": members}
            log(f"{etf}: {len(members)} members from {source}")
        else:
            log(f"{etf}: no members this run - keeping the previous list ({len((idx.get(etf) or {}).get('members') or [])})")
    sp = {m["ticker"]: m.get("sector") for m in (idx.get("SPY") or {}).get("members") or []}
    for etf in ("DIA", "QQQ"):
        for m in (idx.get(etf) or {}).get("members") or []:
            if not m.get("sector"):
                m["sector"] = sp.get(m["ticker"])
    write(OUT / "universe.json", {"updatedAt": now_iso(), "indices": idx})
    return 0


if __name__ == "__main__":
    sys.exit(main())

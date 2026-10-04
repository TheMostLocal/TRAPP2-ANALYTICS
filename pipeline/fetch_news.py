#!/usr/bin/env python3
"""
fetch_news.py — backend news puller for TRAPP2-ANALYTICS.

Pulls recent news per ticker via yfinance (free, keyless) for every ticker in
the master.json files and writes one compact corpus:

    data/news/latest.json
      { generatedAt, count, items: [ {url, headline, summary, ticker,
                                      source, datetime, _fetchedByTicker,
                                      _tickerConfident} ] }

TICKER ATTRIBUTION (the important part):
yfinance's per-ticker news endpoint returns a mix of (a) news genuinely about
the ticker and (b) general market/macro news that Yahoo merely surfaces on that
ticker's page. The old version tagged EVERYTHING with the ticker it was fetched
under, so popular names (esp. NVDA, first popular symbol scanned) collected a
pile of Fed/crypto/SpaceX articles mis-labeled NVDA.

Now an article keeps its fetched ticker ONLY when we're confident it's about it:
  1. yfinance lists the ticker in the article's related/stock tickers, OR
  2. the symbol appears as a standalone token in the headline/summary, OR
  3. a distinctive company-name token (e.g. "Nvidia", "Tesla") appears.
Otherwise the ticker is left BLANK ("") and the frontend routes the article to
the review queue for a human to assign — no more silent NVDA defaulting.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

REPOS = [
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-1/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-2/main/data/master.json",
    "https://raw.githubusercontent.com/GoodGlobeLLC/TRAPP2-3/main/data/master.json",
]
OUT = Path(__file__).resolve().parent.parent / "data" / "news" / "latest.json"
PER_TICKER = 4
GLOBAL_CAP = 2500
SLEEP = 0.12

# Common corporate-suffix / filler tokens that aren't distinctive enough to
# attribute an article to a company on their own.
_STOP = {
    "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC",
    "HOLDINGS", "HOLDING", "GROUP", "CLASS", "COMMON", "STOCK", "SHARES",
    "ETF", "FUND", "TRUST", "ISHARES", "SPDR", "VANGUARD", "INDEX", "THE",
    "AND", "OF", "FOR", "MSCI", "ULTRA", "PROSHARES", "TECHNOLOGIES",
    "TECHNOLOGY", "INTERNATIONAL", "AMERICAN", "GLOBAL", "SYSTEMS",
    "SERVICES", "PARTNERS", "ADR", "NV", "SA", "AG", "REIT",
}


def _name_tokens(name):
    """Distinctive uppercase tokens from a company name for loose matching."""
    if not name:
        return []
    toks = re.findall(r"[A-Za-z][A-Za-z&\.]{2,}", name.upper())
    out = []
    for t in toks:
        t = t.replace(".", "").replace("&", "")
        if len(t) >= 4 and t not in _STOP:
            out.append(t)
    return out[:3]  # first few distinctive words are enough


def load_universe():
    """Return (ordered_tickers, {ticker: [name_tokens]})."""
    tickers, seen, names = [], set(), {}
    for url in REPOS:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ValuatioAnalytics"}), timeout=30) as r:
                d = json.load(r)
            rows = d if isinstance(d, list) else (d.get("rows") or list(d.values()))
            if isinstance(rows, dict):
                rows = list(rows.values())
        except Exception as e:
            print(f"  ✗ {url.split('/')[4]}: {e}", file=sys.stderr)
            continue
        for row in rows:
            t = (row.get("ticker") or "").upper()
            if not t or t in seen:
                continue
            seen.add(t)
            tickers.append(t)
            names[t] = _name_tokens(row.get("name") or row.get("Name") or "")
    return tickers, names


def _related_tickers(c, n):
    """Best-effort extraction of tickers yfinance associates with an article."""
    out = set()
    for src in (c, n):
        if not isinstance(src, dict):
            continue
        # Newer shapes nest under finance/stockTickers; older used relatedTickers.
        rel = src.get("relatedTickers")
        if isinstance(rel, list):
            out.update(str(x).upper() for x in rel if x)
        fin = src.get("finance") or {}
        st = fin.get("stockTickers") if isinstance(fin, dict) else None
        if isinstance(st, list):
            for x in st:
                sym = (x.get("symbol") if isinstance(x, dict) else x)
                if sym:
                    out.add(str(sym).upper())
    return out


def _is_about(ticker, name_toks, text, related):
    """True if we're confident the article is about `ticker`."""
    if related:
        return ticker in related          # explicit association wins
    U = text.upper()
    # Standalone symbol mention (word-boundary so "AI" doesn't match "SAID").
    if re.search(r"(?<![A-Z])" + re.escape(ticker) + r"(?![A-Z])", U):
        return True
    # Distinctive company-name token mention.
    for tok in (name_toks or []):
        if re.search(r"(?<![A-Z])" + re.escape(tok) + r"(?![A-Z])", U):
            return True
    return False


def main():
    tickers, names = load_universe()
    print(f"Pulling news for {len(tickers)} tickers …")

    items, have_urls = [], set()
    confident_n = 0
    for i, t in enumerate(tickers, 1):
        try:
            raw = yf.Ticker(t).news or []
        except Exception:
            raw = []
        for n in raw[:PER_TICKER]:
            c = n.get("content") or n
            url = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or n.get("link")
            title = c.get("title") or n.get("title")
            if not url or not title or url in have_urls:
                continue
            have_urls.add(url)
            summary = (c.get("summary") or c.get("description") or "")[:400]
            related = _related_tickers(c, n)
            confident = _is_about(t, names.get(t), f"{title} {summary}", related)
            if confident:
                confident_n += 1
            ts = c.get("pubDate") or n.get("providerPublishTime")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
            items.append({
                "url": url, "headline": title, "summary": summary,
                # Confident → keep the ticker. Not confident → BLANK so the
                # frontend sends it to review instead of mis-attributing it.
                "ticker": t if confident else "",
                "source": ((c.get("provider") or {}).get("displayName")
                           or n.get("publisher") or "Yahoo"),
                "datetime": ts,
                "_fetchedByTicker": True,
                "_tickerConfident": confident,
                # Keep the fetch-origin so the frontend can SUGGEST it in review
                # without treating it as confirmed.
                "_suggestedTicker": t,
            })
        if i % 50 == 0:
            print(f"  … {i}/{len(tickers)} · {len(items)} articles ({confident_n} confident)")
        time.sleep(SLEEP)
        if len(items) >= GLOBAL_CAP:
            print("  global cap reached")
            break

    items.sort(key=lambda a: a.get("datetime") or "", reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items), "items": items,
    }, separators=(",", ":")))
    blank = len(items) - confident_n
    print(f"✓ news/latest.json: {len(items)} articles · {confident_n} confidently tagged · {blank} → review (blank ticker)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

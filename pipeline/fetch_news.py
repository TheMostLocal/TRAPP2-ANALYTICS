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

FALLBACK (z87): if yfinance comes back (nearly) empty - Yahoo throttling or
blocking the Actions runner, or an API change - the run falls back to keyless
RSS: Google News search feeds for (a) the topics the crypto/futures research
layer reads (bitcoin, crude oil, grains, metals, rates ...) and (b) the largest
RSS_TICKERS names in the universe. Same attribution rules; topic items carry
`topic` and a blank ticker. Every item records `via` (yfinance | google-news-rss).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import yfinance as yf

# Repo owner — resolved at runtime so the pipeline follows the repos to any
# GitHub account. Actions sets GITHUB_REPOSITORY_OWNER automatically;
# VALUATIO_OWNER (repo variable/env) overrides; TheMostLocal is the fallback.
_GH_OWNER = (__import__("os").environ.get("VALUATIO_OWNER")
             or __import__("os").environ.get("GITHUB_REPOSITORY_OWNER")
             or "TheMostLocal").strip()

REPOS = [
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2/main/data/master.json",
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-1/main/data/master.json",
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-2/main/data/master.json",
    f"https://raw.githubusercontent.com/{_GH_OWNER}/TRAPP2-3/main/data/master.json",
]
OUT = Path(__file__).resolve().parent.parent / "data" / "news" / "latest.json"
XTRAPP_URL = f"https://raw.githubusercontent.com/{_GH_OWNER}/XTRAPP/main/data/xtrapp_data.json"
PER_TICKER = 4
GLOBAL_CAP = 2500
SLEEP = 0.12
# RSS fallback (see module docstring)
RSS_FALLBACK = (os.environ.get("NEWS_RSS_FALLBACK") or "1") not in ("0", "false", "no")
RSS_MIN_ITEMS = 100          # fewer yfinance items than this = treat the source as failed
RSS_TICKERS = int(os.environ.get("NEWS_RSS_TICKERS") or 150)
RSS_PER_FEED = 6
RSS_SLEEP = 0.4
GN_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
RSS_TOPICS = {   # alt_research groups -> search queries (its keyword matching does the rest)
    "crypto": ["bitcoin", "ethereum crypto"],
    "energy": ["crude oil OPEC", "natural gas prices"],
    "grains": ["corn wheat soybeans USDA"],
    "softs": ["coffee cocoa sugar prices"],
    "precious": ["gold price"],
    "base": ["copper price"],
    "rates": ["treasury yields Fed"],
    "livestock": ["cattle hog prices"],
    "markets": ["stock market today"],
}

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
    """Return (ordered_tickers, {ticker: [name_tokens]}); market caps are kept in
    MCAPS so the RSS fallback can pick the largest names."""
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
            try:
                MCAPS[t] = float(row.get("marketcap") or 0)
            except (TypeError, ValueError):
                MCAPS[t] = 0.0
            ASSET[t] = str(row.get("asset_class") or "")
            CCY[t] = str(row.get("currency") or "USD").upper()
    return tickers, names


MCAPS, ASSET, CCY = {}, {}, {}


def _strip_html(x):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", x or ""))).strip()


def fetch_rss(url, timeout=20):
    """-> [{url, headline, summary, source, datetime}] from an RSS 2.0 feed."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ValuatioAnalytics news fallback)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
        # Google News titles end with " - Publisher"
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        ts = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(ts).astimezone(timezone.utc).isoformat(timespec="seconds") if ts else None
        except Exception:
            ts = None
        out.append({"url": link, "headline": title, "summary": _strip_html(it.findtext("description"))[:400],
                    "source": source or "Google News", "datetime": ts})
    return out


def rss_fallback(tickers, names, have_urls):
    """Keyless RSS when yfinance failed. -> (items, stats)"""
    items, stats = [], {"topicFeeds": 0, "tickerFeeds": 0, "feedErrors": 0, "firstError": None}

    def pull(url):
        try:
            got = fetch_rss(url)
        except Exception as e:
            stats["feedErrors"] += 1
            stats["firstError"] = stats["firstError"] or f"{type(e).__name__}: {str(e)[:120]}"
            got = []
        time.sleep(RSS_SLEEP)
        return got

    for topic, queries in RSS_TOPICS.items():
        for q in queries:
            stats["topicFeeds"] += 1
            for a in pull(GN_URL.format(q=urllib.parse.quote(q)))[:RSS_PER_FEED]:
                if a["url"] in have_urls:
                    continue
                have_urls.add(a["url"])
                items.append(dict(a, ticker="", topic=topic, via="google-news-rss",
                                  _fetchedByTicker=False, _tickerConfident=False, _suggestedTicker=""))
    # Largest US-listed equities (market caps are in LOCAL currency, so foreign
    # listings in yen/won would otherwise crowd the list; the feed is US-English).
    ranked = sorted((t for t in tickers if ASSET.get(t, "").lower() in ("equity", "")
                     and CCY.get(t, "USD") == "USD"),
                    key=lambda t: -MCAPS.get(t, 0))[:RSS_TICKERS]
    for t in ranked:
        stats["tickerFeeds"] += 1
        tok = (names.get(t) or [None])[0]
        q = f'"{t}" stock' + (f' OR "{tok.title()}"' if tok else "")
        for a in pull(GN_URL.format(q=urllib.parse.quote(q)))[:PER_TICKER]:
            if a["url"] in have_urls:
                continue
            have_urls.add(a["url"])
            confident = _is_about(t, names.get(t), f"{a['headline']} {a['summary']}", set())
            items.append(dict(a, ticker=t if confident else "", via="google-news-rss",
                              _fetchedByTicker=True, _tickerConfident=confident, _suggestedTicker=t))
    return items, stats


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


# Symbols that are also everyday words: only count them in explicit forms
# ("$IT", "(IT)", "NYSE: IT"), never as a bare word ("it", "IT department").
_WORD_SYMBOLS = {
    "A", "AI", "ALL", "ANY", "ARE", "BE", "BIG", "CAN", "CAR", "CASH", "CAT", "EAT", "FOR", "FUN", "GO", "HAS",
    "HIGH", "IT", "KEY", "LIFE", "LOW", "MAN", "NEW", "NOW", "ON", "ONE", "OPEN", "OUT", "PEAK", "PLAY", "REAL",
    "RUN", "SAVE", "SEE", "SO", "TEAM", "TOP", "TRUE", "TWO", "UP", "WELL", "WIN", "YOU",
}
# "price target", "target price", "raises target" ... are analyst language, not Target Corp.
_FIN_PHRASE = re.compile(
    r"\b(?:price|stock|share|analyst|earnings|revenue|sales|profit|margin|inflation|growth|return|upside|downside|"
    r"valuation|raise[sd]?|cut[s]?|lower(?:s|ed)?|boost(?:s|ed)?)\s+targets?\b|\btargets?\s+(?:price|prices|of|range|"
    r"for|at|to|on|above|below)\b|\btarget(?:ed|ing)\b", re.I)


def _symbol_hit(ticker, text):
    """Ticker mentioned explicitly ($X, (X), exchange prefix) or, for ordinary
    symbols, as an UPPERCASE standalone word in the original text (z88: the old
    test upper-cased the text first, so "it" counted as Gartner's IT)."""
    t = re.escape(ticker)
    if re.search(r"(?:\$|\(|(?:NYSE|NASDAQ|NYSEARCA|AMEX|OTC)\s*:\s*)" + t + r"(?![A-Za-z0-9])", text):
        return True
    if ticker in _WORD_SYMBOLS or len(ticker) < 3:
        return False
    return re.search(r"(?<![A-Za-z0-9$])" + t + r"(?![A-Za-z0-9])", text) is not None


def _name_hit(tok, text):
    """Company-name token, whole word, Capitalized or ALL-CAPS as a name would be
    written (z88: was case-insensitive, so "price target" tagged Target Corp)."""
    for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", text, re.I):
        w = m.group(0)
        if not w[0].isupper():
            continue                      # "apple pie", "a target"
        if tok == "TARGET" and _FIN_PHRASE.search(text[max(0, m.start() - 30): m.end() + 30]):
            continue
        return True
    return False


def _is_about(ticker, name_toks, text, related):
    """True if we're confident the article is about `ticker`."""
    if related:
        return ticker in related          # explicit association wins
    if _symbol_hit(ticker, text):
        return True
    return any(_name_hit(tok, text) for tok in (name_toks or []))

def apply_xtrapp_fixes(items):
    """XTRAPP is the system override (z90): every human article fix made in the
    app's Review / news editor is applied to the pipeline corpus, so the
    backend (bot research, analytics) sees the same truth the app shows.
      bad      -> article dropped
      fixed / confirmed -> ticker + tickers from the fix
      noticker -> general news (blank ticker, no tickers)
      headline / summary / sentiment / impact overrides -> applied
    Keyed like the app: url, else id, else "TICKER:headline".
    -> (items, stats)"""
    stats = {"loaded": 0, "dropped": 0, "retagged": 0, "untagged": 0, "edited": 0, "error": None}
    try:
        req = urllib.request.Request(XTRAPP_URL, headers={"User-Agent": "ValuatioAnalytics/xtrapp"})
        with urllib.request.urlopen(req, timeout=30) as r:
            x = json.loads(r.read().decode("utf-8"))
        fixes = x.get("articleFixes") or {}
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {str(e)[:100]}"
        return items, stats
    stats["loaded"] = len(fixes)
    if not fixes:
        return items, stats
    out = []
    for a in items:
        key = a.get("url") or a.get("id") or f"{a.get('ticker', '')}:{a.get('headline', '')}"
        f = fixes.get(key)
        if not isinstance(f, dict):
            out.append(a)
            continue
        v = f.get("verdict")
        if v == "bad":
            stats["dropped"] += 1
            continue
        if v in ("fixed", "confirmed") and isinstance(f.get("tickers"), list) and f["tickers"]:
            a["ticker"] = str(f["tickers"][0]).upper()
            a["tickers"] = [str(t).upper() for t in f["tickers"]]
            a["_tickerConfident"] = True
            stats["retagged"] += 1
        elif v == "noticker":
            a["ticker"], a["tickers"], a["_noTicker"], a["_tickerConfident"] = "", [], True, False
            stats["untagged"] += 1
        edited = False
        for src, dst in (("fixHeadline", "headline"), ("fixSummary", "summary"), ("sentiment", "sentiment"), ("impact", "impact")):
            if f.get(src):
                a[dst] = f[src]
                edited = True
        if edited:
            stats["edited"] += 1
        a["humanFixed"] = True
        out.append(a)
    return out, stats


def main():
    tickers, names = load_universe()
    print(f"Pulling news for {len(tickers)} tickers …")

    items, have_urls = [], set()
    confident_n = 0
    errors, first_err, empty = 0, None, 0
    for i, t in enumerate(tickers, 1):
        try:
            raw = yf.Ticker(t).news or []
        except Exception as e:
            raw = []
            errors += 1
            first_err = first_err or f"{t}: {type(e).__name__}: {str(e)[:160]}"
        if not raw:
            empty += 1
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
                "via": "yfinance",
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

    print(f"  fetch stats: {len(tickers)} tickers · {errors} raised · {empty} returned no news")
    if RSS_FALLBACK and len(items) < RSS_MIN_ITEMS:
        print(f"  yfinance gave {len(items)} items (< {RSS_MIN_ITEMS}) - falling back to keyless RSS"
              + (f"; first yfinance error: {first_err}" if first_err else ""))
        extra, rs = rss_fallback(tickers, names, have_urls)
        items += extra
        confident_n += sum(1 for a in extra if a.get("_tickerConfident"))
        print(f"  rss fallback: {len(extra)} items from {rs['topicFeeds']} topic + {rs['tickerFeeds']} ticker feeds"
              f" · {rs['feedErrors']} feed errors" + (f" (first: {rs['firstError']})" if rs['firstError'] else ""))
    if not items:
        # Never overwrite a good corpus with an empty one. A zero-article run is
        # a source failure (Yahoo throttling/blocking the runner, or a yfinance
        # API change), not "no news" - keep yesterday's file and say so.
        print(f"::error::news: 0 articles from {len(tickers)} tickers ({errors} raised"
              + (f"; first: {first_err}" if first_err else "") + ") - latest.json left unchanged")
        return 1

    items, xs = apply_xtrapp_fixes(items)
    print(f"  XTRAPP article fixes: {xs['loaded']} on file · {xs['retagged']} retagged · {xs['untagged']} general news"
          f" · {xs['dropped']} dropped as bad · {xs['edited']} text edits" + (f" · read failed: {xs['error']}" if xs['error'] else ""))
    items.sort(key=lambda a: a.get("datetime") or "", reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items), "items": items,
    }, separators=(",", ":")))
    blank = len(items) - confident_n
    via = {}
    for a in items:
        via[a.get("via", "?")] = via.get(a.get("via", "?"), 0) + 1
    print(f"✓ news/latest.json: {len(items)} articles · {confident_n} confidently tagged · {blank} → review (blank ticker)"
          f" · by source {via}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

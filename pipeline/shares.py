# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/shares.py
#  z202 - MARKET CAP BY THE FORMULA: price x shares outstanding, with the share count as the company FILED it.
#  Owner 11-21: "We need all data to be 100% accurate ... instead of relying on yahoo finance to give us market cap we
#  can simply use the price and outstanding shares to find this and compare with Yahoo and take the most accurate
#  info ... it's measurable and computable and I feel we should rely on the formulas instead of trying to pull more
#  from a website that may hit a limit."
#  Until now the app's "Price x Shares" check multiplied the price by YAHOO's own share count - it could only ever
#  agree with Yahoo's market cap (AAPL: 342.56 x 14,532,182,000 = Yahoo's 4,978,144,288,451 to the dollar). An
#  independent count is what makes it a check:
#    * SEC: dei:EntityCommonStockSharesOutstanding - the count on the cover of every 10-K / 10-Q (as of a stated
#      date, weeks after the period end) - from the XBRL FRAMES API: ONE request returns every filer's latest count
#      for a calendar quarter (the last three quarters are read: 3 requests for ~6,000 companies, no per-company
#      calls, no rate limits to hit). Frames carry non-dimensional facts only, so a company that files its count per
#      share class (Alphabet, Berkshire) has no SEC total here - Yahoo stays for it, flagged.
#    * Yahoo: the books' master.json (marketcap / price = the count Yahoo's market cap implies; its "shares" field).
#    * A third count to break ties: the latest diluted weighted-average shares in this repo's data/sec/<T>.json
#      (us-gaap, 10-Q / 10-K; diluted >= basic by a few % - options, RSUs).
#  THE PICK (per ticker; the formula's market cap = price x the picked count). A count is replaced only when something
#  independent says the replacement is right:
#    within 2 %                  -> SEC, "verified" (two independent counts agree)
#    2-10 %                      -> SEC when the diluted count sides with it (and not with Yahoo), "sec"; else Yahoo,
#                                   "near" (flagged)
#    > 10 %                      -> Yahoo, "check" (a split since the cover date - both SEC counts are pre-split then -,
#                                   an ADR's ratio, a class count, a tagging error) - listed in review[] for a human
#    SEC only (<= 200 days)      -> SEC, "sec-only"     · Yahoo only -> Yahoo, "yahoo-only"
#  Foreign listings (not USD) and funds without a filing keep Yahoo. -> data/sec/shares.json (the app and the bot read it)
#  Needs SEC_USER_AGENT (the SEC run's secret). Never stops a pipeline: no SEC, no change (exits 0).
# ============================================================
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BUILD = "z202"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "sec" / "shares.json"
RAW = "https://raw.githubusercontent.com/TheMostLocal"
BOOKS = ("TRAPP2", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4", "TRAPP2-1")
UA = (os.environ.get("SEC_USER_AGENT") or "").strip()
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{}.json"
AGREE, NEAR = 0.02, 0.10            # within 2 % = the same count; beyond 10 % = a structural difference
STALE_DAYS = 200                    # an SEC cover count older than this is not used alone
DIL_TOL = 0.06                      # diluted vs a count: within 6 % = "sides with it"
REVIEW = 60


def log(*a):
    print("[shares]", *a, flush=True)


def now_utc():
    v = os.environ.get("SHARES_NOW")
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.now(timezone.utc)


def _num(v):
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def get_json(url, sec=False, tries=3):
    """GET JSON (None on any failure). SEC requests carry the SEC_USER_AGENT and stay well under 10 a second."""
    if os.environ.get("SHARES_OFFLINE"):
        return None
    hdr = {"User-Agent": UA if sec else "valuatio-shares", "Accept-Encoding": "identity"}
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                         # a quarter with no frame yet
            time.sleep(1.5 * (k + 1))
        except Exception:
            time.sleep(1.5 * (k + 1))
    return None


def frame_codes(today):
    """The last three calendar quarters' instantaneous frames, newest first: 2026-11-21 -> CY2026Q4I, Q3I, Q2I."""
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(3):
        out.append(f"CY{y}Q{q}I")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def sec_counts(frames):
    """[frame docs] -> {cik: {"val", "end", "accn"}} - each filer's latest cover count across the frames."""
    best = {}
    for doc in frames:
        for f in ((doc or {}).get("data") or []):
            try:
                cik, val, end = int(f.get("cik")), _num(f.get("val")), str(f.get("end") or "")[:10]
            except (TypeError, ValueError):
                continue
            if not val or val <= 0 or len(end) != 10:
                continue
            if cik not in best or end > best[cik]["end"]:
                best[cik] = {"val": val, "end": end, "accn": f.get("accn")}
    return best


def book_rows(fetch=get_json):
    """{ticker: {price, mcap, shares, fund, usd, book}} from the books' master.json (the first book listing a ticker wins)."""
    out = {}
    for b in BOOKS:
        d = fetch(f"{RAW}/{b}/main/data/master.json")
        rows = d if isinstance(d, list) else ((d or {}).get("rows") or []) if isinstance(d, dict) else []
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").upper()
            if not t or t in out:
                continue
            cur = str(r.get("currency") or "USD").upper()
            out[t] = {"price": _num(r.get("price")), "mcap": _num(r.get("marketcap") or r.get("marketCap")),
                      "shares": _num(r.get("shares")), "book": b, "usd": cur in ("USD", ""),
                      "fund": str(r.get("isetf") or "").lower() == "true" or str(r.get("isfund") or "").lower() == "true"}
    return out


def diluted_latest(doc):
    """The latest diluted weighted-average share count (quarterly or annual, by period end) -> (val, end) or (None, None)."""
    ds = ((doc or {}).get("facts") or {}).get("dilutedShares") or {}
    pts = [p for p in (ds.get("quarterly") or []) + (ds.get("annual") or []) if _num(p.get("val")) and p.get("end")]
    if not pts:
        return None, None
    p = max(pts, key=lambda x: str(x["end"]))
    return _num(p["val"]), str(p["end"])[:10]


def _days(a, b):
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (TypeError, ValueError):
        return None


def pick(sec, sec_end, yh, dil, dil_end, today):
    """-> (shares, source "sec" | "yahoo" | None, status, why). The rules in the header. A count is only replaced
    when something independent says the replacement is right: agreement, or the diluted count siding with the SEC.
    A large gap always keeps Yahoo - after a split both SEC counts (cover, diluted) are pre-split and would agree
    with each other while both being wrong."""
    age = _days(sec_end, today.isoformat()) if sec else None
    dil_ok = bool(dil and dil_end) and (_days(dil_end, today.isoformat()) or 999) <= STALE_DAYS + 90
    near = lambda a, b: bool(a and b) and abs(a / b - 1) <= DIL_TOL
    if sec and yh:
        dx = sec / yh - 1
        if abs(dx) <= AGREE:
            return sec, "sec", "verified", f"SEC {sec:,.0f} (cover {sec_end}) and Yahoo agree ({dx:+.1%})"
        if abs(dx) <= NEAR:
            if dil_ok and near(dil, sec) and not near(dil, yh) and age is not None and age <= STALE_DAYS:
                return sec, "sec", "sec", f"SEC cover {sec_end} ({age} days) {dx:+.1%} off Yahoo; the diluted count ({dil_end}) sides with the SEC"
            return yh, "yahoo", "near", f"SEC cover {sec_end} {dx:+.1%} off Yahoo and nothing independent decides - Yahoo kept, flagged"
        return yh, "yahoo", "check", (f"SEC cover {sec:,.0f} vs Yahoo {yh:,.0f} ({dx:+.0%}) - a split since {sec_end}, an ADR ratio, "
                                      f"a class count or a tagging error; Yahoo kept until reviewed")
    if sec:
        if age is not None and age <= STALE_DAYS:
            return sec, "sec", "sec-only", f"SEC cover {sec_end} ({age} days); no Yahoo count"
        return None, None, "none", f"SEC cover {sec_end} too old ({age} days) and no Yahoo count"
    if yh:
        return yh, "yahoo", "yahoo-only", "no SEC cover count (multi-class, a fund, a foreign filer or not in the frames)"
    return None, None, "none", "no count"


def build(rows, cik_of, counts, sec_docs, today):
    """-> the shares.json document."""
    out, review = {}, []
    tally = {}
    for t, r in sorted(rows.items()):
        if not r.get("usd"):
            continue                                         # a foreign listing: its own market, its own currency
        px = r.get("price")
        yh = (r["mcap"] / px) if (r.get("mcap") and px) else r.get("shares")
        c = counts.get(cik_of.get(t)) if cik_of.get(t) is not None else None
        sec, sec_end = (c["val"], c["end"]) if c else (None, None)
        dil, dil_end = diluted_latest(sec_docs.get(t)) if sec_docs.get(t) else (None, None)
        sh, src, st, why = pick(sec, sec_end, yh, dil, dil_end, today)
        tally[st] = tally.get(st, 0) + 1
        if not sh:
            continue
        rec = {"s": round(sh), "p": src, "st": st}
        if sec:
            rec["se"] = [round(sec), sec_end]
        if yh:
            rec["y"] = round(yh)
        if sec and yh:
            rec["dx"] = round((sec / yh - 1) * 100, 2)
        if dil:
            rec["dl"] = [round(dil), dil_end]
        if px:
            rec["px"] = px
            rec["mc"] = round(px * sh)
        if r.get("mcap"):
            rec["ym"] = round(r["mcap"])
        if st in ("check", "near") or (rec.get("dx") is not None and abs(rec["dx"]) > AGREE * 100):
            review.append({"t": t, "why": why, "dx": rec.get("dx"), "sec": rec.get("se"), "yahoo": rec.get("y"), "dil": rec.get("dl"), "pick": src})
        out[t] = rec
    review.sort(key=lambda x: -abs(x.get("dx") or 0))
    n_sec = sum(1 for v in out.values() if v["p"] == "sec")
    return {"build": BUILD, "asOf": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "date": today.isoformat(),
            "rule": ("market cap = price x the picked share count; SEC cover count (dei:EntityCommonStockSharesOutstanding, XBRL frames) "
                     "vs the count Yahoo's market cap implies: within 2 % -> SEC (verified); 2-10 % -> SEC only when the diluted count "
                     "(us-gaap, latest 10-Q / 10-K) sides with it, else Yahoo (near, flagged); > 10 % -> Yahoo (check, flagged - a split, "
                     "an ADR ratio, a class count); SEC only if <= 200 days old; foreign listings keep Yahoo"),
            "keys": {"s": "shares used", "p": "source of s", "st": "status", "se": "[SEC cover count, as of]", "y": "Yahoo's count (its market cap / price)",
                     "dx": "SEC vs Yahoo, %", "dl": "[diluted weighted shares, period end]", "px": "book price", "mc": "price x s", "ym": "Yahoo's market cap"},
            "counts": dict(tally, tickers=len(out), fromSec=n_sec, fromYahoo=len(out) - n_sec),
            "tickers": out, "review": review[:REVIEW]}


def main():
    today = now_utc().date()
    if not UA or "@" not in UA:
        log("SEC_USER_AGENT not set - skipped (the formula needs the SEC's own share counts)")
        return 0
    cmap = get_json(TICKERS_URL, sec=True) or {}
    cik_of = {}
    for v in (cmap.values() if isinstance(cmap, dict) else []):
        try:
            cik_of.setdefault(str(v["ticker"]).upper().replace(".", "-"), int(v["cik_str"]))
        except (KeyError, TypeError, ValueError):
            continue
    frames, seen = [], {}
    for code in frame_codes(today):
        doc = get_json(FRAME_URL.format(code), sec=True)
        seen[code] = len((doc or {}).get("data") or [])
        if doc:
            frames.append(doc)
        time.sleep(0.25)
    counts = sec_counts(frames)
    rows = book_rows()
    sec_docs = {}
    for t in rows:
        p = DATA / "sec" / f"{t}.json"
        if p.exists():
            try:
                sec_docs[t] = json.loads(p.read_text())
            except Exception:
                pass
    if not counts or not rows:
        log(f"no SEC frames ({seen}) or no book rows ({len(rows)}) - kept the last file")
        return 0
    doc = build(rows, cik_of, counts, sec_docs, today)
    doc["frames"] = seen
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, separators=(",", ":")))
    c = doc["counts"]
    log(f"{c['tickers']} tickers · from SEC {c['fromSec']} (verified {c.get('verified', 0)}, corroborated {c.get('sec', 0)}, "
        f"SEC only {c.get('sec-only', 0)}) · Yahoo kept {c['fromYahoo']} (check > 10 % {c.get('check', 0)}, near 2-10 % {c.get('near', 0)}, "
        f"no SEC count {c.get('yahoo-only', 0)}) · frames {seen} · review: "
        + ", ".join(f"{r['t']} {r['dx']:+.0f}%" for r in doc["review"][:12] if r.get("dx") is not None))
    return 0


if __name__ == "__main__":
    sys.exit(main())

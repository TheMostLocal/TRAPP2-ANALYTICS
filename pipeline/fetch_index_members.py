# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/earnings_common.py
#  z136 - shared helpers for the earnings pipeline (universe, per-ticker earnings, season).
# ============================================================
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "earnings"
TICK = OUT / "tickers"

# GICS sector → the eleven names and three groups used on the page (Fundstrat's grouping)
SECTOR_NAME = {
    "information technology": "Technology", "technology": "Technology",
    "consumer discretionary": "Consumer Discretionary", "consumer cyclical": "Consumer Discretionary",
    "industrials": "Industrials", "materials": "Basic Materials", "basic materials": "Basic Materials",
    "communication services": "Communication Services", "telecommunication services": "Communication Services",
    "financials": "Financials", "financial services": "Financials", "real estate": "Real Estate",
    "energy": "Energy", "health care": "Healthcare", "healthcare": "Healthcare",
    "consumer staples": "Consumer Staples", "consumer defensive": "Consumer Staples", "utilities": "Utilities",
}
GROUPS = [("Cyclicals", ["Technology", "Consumer Discretionary", "Industrials", "Basic Materials", "Communication Services"]),
          ("Near-Cyclicals", ["Financials", "Real Estate", "Energy"]),
          ("Defensives", ["Healthcare", "Consumer Staples", "Utilities"])]
SECTOR_ETF = {"Technology": "XLK", "Consumer Discretionary": "XLY", "Industrials": "XLI", "Basic Materials": "XLB",
              "Communication Services": "XLC", "Financials": "XLF", "Real Estate": "XLRE", "Energy": "XLE",
              "Healthcare": "XLV", "Consumer Staples": "XLP", "Utilities": "XLU"}


def sector_of(raw):
    return SECTOR_NAME.get(str(raw or "").strip().lower())


def yahoo_symbol(t):
    """BRK.B -> BRK-B (Yahoo's form); strips spaces / stray characters."""
    t = str(t or "").strip().upper().replace(" ", "")
    return t.replace(".", "-") if t and not t.endswith((".L", ".PA", ".T")) else t


def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def clean(o):
    """No NaN / inf ever reaches JSON."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean(doc), separators=(",", ":")))
    os.replace(tmp, path)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def season_quarter(today=None):
    """The reporting season = the most recently completed calendar quarter (on 2026-10-20: 3Q26,
    ending 2026-09-30). Reports dated from the day after the quarter end through +100 days count."""
    d = today or date.today()
    q = (d.month - 1) // 3                        # current quarter index 0..3
    y = d.year
    if q == 0:
        y, q = y - 1, 4
    end_month = q * 3
    end = (date(y, end_month % 12 + 1, 1) if end_month < 12 else date(y + 1, 1, 1)) - timedelta(days=1)
    return {"label": f"{q}Q{str(y)[2:]}", "end": end.isoformat(), "from": (end + timedelta(days=1)).isoformat(),
            "to": (end + timedelta(days=100)).isoformat()}

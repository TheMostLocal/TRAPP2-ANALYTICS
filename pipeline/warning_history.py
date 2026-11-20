#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/warning_history.py
#
#  z197 - CRISES THE SYSTEM KNOWS (owner, 11-19: "I need the system to understand the dot com bubble and everything that
#  went into that … where these first warnings occurred … other crashes, recessions and scares … anything that shows clear
#  stress signals … History rhymes and bail-outs are bad").
#
#  Each episode: when it peaked and bottomed, what broke, what warned first, what policy did (the bail-out), and what the
#  rules of the day did NOT cover - the gap the next crisis used. The narrative is history; the numbers are not typed in:
#    readings(rows, ep)  - the gauges and pillars as they read (publication-lagged) 12 / 6 / 3 months before the peak
#    first_warnings(...)  - which indicators crossed into their top fifth first, and how many months ahead
#    rhymes(now, eps)     - which past run-ups today's pillar readings most resemble, and on what
#  "History doesn't repeat, it rhymes": regulation closes the last gap; the next one sits inside the new rules.
#
#  z198 - the panel reaches back to 1955, so 1962, 1966, 1969-70, 1973-74, 1980-82 and 1987 are read from data too
#  (each episode names the pillars its readings rest on), and track_records() asks the scientist's question of every
#  first warning: how often has a crossing like this one been followed by a 20 % fall or a recession - and how often not?
# ============================================================
import bisect
import math
from datetime import date, timedelta

# peak / trough: equity peak and trough months (or the stress onset for a non-equity episode); rec: NBER recession months
EPISODES = [
    dict(id="1962", name="1962 'Kennedy slide'", kind="valuation / policy shock", peak="1961-12", trough="1962-06", rec=None,
         broke="The S&P 500 fell 28 % in six months without a recession: richly valued growth stocks met the April 1962 steel-price confrontation and a May 28 one-day break.",
         warnings=["S&P 500 at ~22x earnings after the 1950s re-rating", "speculation in new issues and 'electronics' stocks (the 1961 IPO boom)"],
         policy="The Fed cut the margin requirement from 70 % to 50 % (July 1962); no rescues.", gap="New-issue promotion and thin disclosure for small IPOs - the SEC's Special Study of the Securities Markets (1963) followed.",
         lesson="An IPO boom on stretched multiples can fall 25 %+ with no recession at all - valuation was the fuel, a shock was the match.", data=True),
    dict(id="1966", name="1966 credit crunch", kind="rates / disintermediation", peak="1966-02", trough="1966-10", rec=None,
         broke="The Fed tightened into a capped deposit market: money left banks and savings & loans for market rates, mortgage credit seized, housing starts fell by half; S&P 500 -22 %.",
         warnings=["market rates above the Regulation Q deposit ceilings (money leaving banks and thrifts)", "Fed tightening into an overheating, war-time economy", "falling housing starts"],
         policy="The Fed asked banks to slow business lending, then eased; Congress extended deposit-rate ceilings to thrifts (1966).",
         gap="Rate ceilings left thrifts funding 30-year mortgages with short deposits - the maturity mismatch that broke the S&L industry in the 1980s.",
         lesson="Capping the price of funding doesn't stop the run - it moves it; the fix sowed the 1980s S&L crisis.", data=True),
    dict(id="1970", name="1969-70 bear market and the Penn Central failure", kind="credit / funding / speculation", peak="1968-11", trough="1970-05", rec=("1969-12", "1970-11"),
         broke="The 'go-go' fund and conglomerate boom unwound (S&P 500 -36 %); Penn Central's bankruptcy (June 1970) defaulted on $82 M of commercial paper and the CP market froze for weaker issuers.",
         warnings=["go-go funds and conglomerates priced on accounting growth", "inflation and Fed tightening (fed funds ~9 % in 1969)", "inverted curve (1969)", "a railroad funding itself with short-term paper"],
         policy="The Fed opened the discount window for banks lending to firms shut out of CP and suspended deposit-rate ceilings on large CDs - the first backstop of a non-bank funding market.",
         gap="Non-bank short-term funding had no lender of last resort - until the Fed became one, an implicit promise later markets priced in (2008's CP facility).",
         lesson="When a big issuer fails, the marginal buyer of short-term paper vanishes overnight; the rescue became a precedent.", data=True),
    dict(id="1973", name="1973-74 oil shock and stagflation", kind="oil / inflation", peak="1973-01", trough="1974-10", rec=("1973-11", "1975-03"),
         broke="OPEC embargo quadrupled oil; inflation above 10 %; the Nifty Fifty de-rated; S&P 500 -48 %.",
         warnings=["oil supply shock", "inflation running ahead of policy", "Nifty Fifty valuations (one-decision stocks at 50x+)"],
         policy="Price controls (failed), then the Fed tightened into the downturn.", gap="Inflation expectations were not anchored - policy credibility, not a rule, was missing.",
         lesson="Valuation on 'can't-miss' growth stocks does not survive an inflation and rate shock.", data=True),
    dict(id="1980", name="1980-82 Volcker tightening", kind="rates / inflation", peak="1980-11", trough="1982-08", rec=("1981-07", "1982-11"),
         broke="Fed funds near 20 %; double-dip recession; LDC (Latin-American) debt crisis followed in 1982.",
         warnings=["fed funds +10 pts", "inverted curve", "bank lending to developing countries (petrodollar recycling)"],
         policy="Volcker held rates high; banks' LDC losses were forborne for years (regulatory forbearance).", gap="Bank capital on sovereign loans - Basel I came in 1988.",
         lesson="Disinflation by recession; forbearance hid bank losses for a decade.", data=True),
    dict(id="1987", name="1987 crash", kind="rates / market structure", peak="1987-08", trough="1987-12", rec=None,
         broke="The Dow fell 22.6 % on 10-19-87; portfolio insurance sold into a falling market.",
         warnings=["10-year yield up ~3 pts in 1987", "falling dollar, trade deficit", "a 40 % rally into August"],
         policy="Fed liquidity statement on 10-20; no firm bail-outs.", gap="Market structure (portfolio insurance, no circuit breakers) - circuit breakers came in 1988.",
         lesson="A rate shock into stretched prices can crash a market without a recession.", data=True),
    dict(id="1990", name="1990-91 recession: S&L, real estate, oil", kind="credit / real estate / oil", peak="1990-07", trough="1990-10", rec=("1990-07", "1991-03"),
         broke="Savings & loans failed on bad real-estate loans; oil doubled after Iraq invaded Kuwait; bank credit crunch.",
         warnings=["S&L and commercial real-estate losses (1986-89)", "Fed tightening 1988-89", "inverted curve 1989", "oil +100 % (Aug-Oct 1990)"],
         policy="FIRREA / Resolution Trust Corp (1989): ~$124 bn of taxpayer losses - the S&L bail-out.", gap="Deposit insurance without risk-based premiums or capital - moral hazard.",
         lesson="Insured deposits funding real-estate speculation: the taxpayer was the backstop.", data=True),
    dict(id="1994", name="1994 bond massacre", kind="rates shock", peak="1994-02", trough="1994-11", rec=None,
         broke="The Fed doubled rates (3 % -> 6 %) in a year; bond losses; Orange County bankrupt; Mexico's peso crisis.",
         warnings=["first hike after a long hold", "levered bond and mortgage-derivative positions", "Mexico's dollar-linked short-term debt (tesobonos)"],
         policy="US Exchange Stabilization Fund loans to Mexico (~$20 bn) - a sovereign bail-out.", gap="Leverage in derivatives (inverse floaters) outside bank capital rules.",
         lesson="A rate shock finds the leverage that assumed rates would stay low.", data=True),
    dict(id="1998", name="1998 Russia / LTCM", kind="leverage / funding", peak="1998-07", trough="1998-10", rec=None,
         broke="Russia defaulted; LTCM's ~25x leveraged convergence trades blew up; credit and liquidity froze.",
         warnings=["Asian crisis from July 1997", "credit spreads widening from late 1997", "hedge-fund leverage invisible to regulators"],
         policy="The Fed organised a private $3.6 bn recapitalisation of LTCM and cut rates three times - the 'Greenspan put'.", gap="Hedge-fund leverage and OTC derivatives were unregulated.",
         lesson="Rescuing leverage teaches leverage; the 1999 mania followed the 1998 rescue.", data=True),
    dict(id="2000", name="2000-02 dot-com bubble", kind="valuation / speculation", peak="2000-03", trough="2002-10", rec=("2001-03", "2001-11"),
         broke="Nasdaq -78 %; telecom over-build and vendor financing (Lucent, Nortel lent to customers to buy their gear) failed; accounting frauds (Enron 2001, WorldCom 2002).",
         warnings=["stock market value / GDP at a record (~180 %)", "IPO frenzy - hundreds of unprofitable IPOs a year, first-day pops ~70 % in 1999",
                   "Fed +1.75 pts (June 1999 - May 2000) into the mania", "inverted curve (2000)", "credit spreads and high-yield defaults rising from 1998",
                   "telecom capex and debt; vendor financing", "narrowing breadth - most stocks fell in 1999 while the index rose", "insider selling at lock-up expiries"],
         policy="Fed cut 6.5 % -> 1.75 %; no bail-out of dot-coms; Sarbanes-Oxley (2002) and the Global Research Settlement (2003) on conflicted analysts.",
         gap="Underwriter-analyst conflicts (buy ratings on banking clients), off-balance-sheet vehicles (Enron SPEs), IPO laddering / spinning.",
         lesson="Price far ahead of profits, funded by the sellers themselves (vendor financing) and cheered by conflicted research - rhymes with any circular build-out.", data=True),
    dict(id="2008", name="2007-09 financial crisis", kind="credit / housing / funding", peak="2007-10", trough="2009-03", rec=("2007-12", "2009-06"),
         broke="Subprime mortgages defaulted; AAA tranches failed; asset-backed commercial paper froze (Aug 2007); Lehman failed; S&P -57 %.",
         warnings=["subprime delinquencies rising from 2006 (published monthly)", "house prices peaked 2006", "ABCP and interbank funding seized Aug 2007",
                   "banks tightening standards 2007", "marginal buyers (German regional banks) stopped buying CDOs in summer 2007", "claims rising 2008"],
         policy="TARP ($700 bn), Fed emergency facilities, Fannie / Freddie conservatorship, AIG rescue - bail-outs of the system.",
         gap="Shadow banking (SIVs, ABCP, repo), ratings models assuming 7-10 % losses, CDS without capital (AIG).",
         lesson="The loss data led by a year; the funding markets were the trigger; bail-outs socialised the losses.", data=True),
    dict(id="2010", name="2010 euro sovereign scare / flash crash", kind="sovereign / funding", peak="2010-04", trough="2010-07", rec=None,
         broke="Greece lost market access; bank funding in euros stressed; the 5-6-2010 flash crash.",
         warnings=["Greek deficit revisions", "euro bank funding spreads", "VIX spike"],
         policy="EU / IMF bail-out of Greece (€110 bn).", gap="No euro-area fiscal backstop; the sovereign-bank doom loop.",
         lesson="Sovereigns can be the marginal borrower that fails.", data=True),
    dict(id="2011", name="2011 euro crisis / US downgrade", kind="sovereign / funding", peak="2011-04", trough="2011-10", rec=None,
         broke="Italy / Spain spreads blew out; S&P downgraded the US (8-5-2011); S&P 500 -19 %.",
         warnings=["peripheral bond spreads", "euro bank CDS", "debt-ceiling standoff"],
         policy="ECB LTROs, then 'whatever it takes' (July 2012).", gap="A central bank as the only buyer of last resort.",
         lesson="Funding stress spreads across borders through banks.", data=True),
    dict(id="2015", name="2015-16 oil collapse / China / high-yield energy", kind="commodity / credit", peak="2015-05", trough="2016-02", rec=None,
         broke="Oil -75 %; high-yield energy defaults; China's devaluation (Aug 2015); an industrial recession.",
         warnings=["oil -50 % y/y", "high-yield energy spreads", "dollar surge", "manufacturing new orders falling"],
         policy="Fed delayed hikes; China capital controls.", gap="Shale capex funded by high-yield debt at $100 oil.",
         lesson="Debt-funded capex on a commodity price fails when the price does.", data=True),
    dict(id="2018", name="2018 Q4 tightening scare", kind="rates / liquidity", peak="2018-09", trough="2018-12", rec=None,
         broke="Fed hikes plus balance-sheet run-off; S&P 500 -20 %; credit spreads widened.",
         warnings=["fed funds +1 pt and quantitative tightening", "flattening curve", "high-yield outflows"],
         policy="Fed pivot (Jan 2019).", gap="Markets priced on a central bank 'put'.",
         lesson="Liquidity withdrawal hits the most expensive assets first.", data=True),
    dict(id="2020", name="2020 COVID crash", kind="exogenous shock", peak="2020-02", trough="2020-03", rec=("2020-02", "2020-04"),
         broke="Pandemic shutdown; the dash for cash broke the Treasury market; S&P 500 -34 % in 23 sessions.",
         warnings=["none in credit or loss data - an exogenous shock", "repo stress (Sept 2019)", "corporate leverage at a record"],
         policy="Fed bought corporate bonds and backstopped everything; CARES Act - the broadest bail-out on record.", gap="Open-end bond funds and Treasury market liquidity.",
         lesson="Not every crash is forecastable; leverage decides how bad it gets.", data=True),
    dict(id="2022", name="2022 inflation and rate shock", kind="rates / inflation / valuation", peak="2022-01", trough="2022-10", rec=None,
         broke="CPI 9 %; the Fed +4.25 pts in a year; Nasdaq -35 %, bonds -17 %; crypto and SPAC collapse.",
         warnings=["M2 +25 % (2020-21)", "inflation ahead of the Fed", "valuation near 2000 levels", "SPAC and IPO frenzy in 2021 (a dot-com rhyme)"],
         policy="No bail-outs; the Fed's fastest hiking cycle since 1981.", gap="Duration risk on bank balance sheets (held-to-maturity accounting) - it surfaced in 2023.",
         lesson="Money-printing and a speculative frenzy, then a rate shock - 2000's structure in a new costume.", data=True),
    dict(id="2023", name="2023 regional bank run (SVB, Signature, First Republic)", kind="banking / rates", peak="2023-02", trough="2023-05", rec=None,
         broke="Unrealised losses on banks' bond portfolios plus concentrated uninsured deposits; a digital bank run.",
         warnings=["2022 rate shock vs. long-duration securities", "uninsured-deposit concentration", "deposits falling (H.8) through 2022", "venture funding drought"],
         policy="Systemic-risk exception (uninsured depositors made whole) and the Bank Term Funding Program - a depositor bail-out.",
         gap="Held-to-maturity accounting hid rate losses; 2018 rules eased oversight of mid-size banks.",
         lesson="The 2022 shock found the gap the 2010 rules left; the bail-out covered it again.", data=True),
]


def _ym(s):
    y, m = int(s[:4]), int(s[5:7])
    return y, m


def _shift(y, m, k):
    t = y * 12 + (m - 1) + k
    return t // 12, t % 12 + 1


def _key(t):
    return f"{t.year:04d}-{t.month:02d}"


def readings(rows, ep, offsets=(12, 6, 3, 0)):
    """The gauges and pillars as they read (publication-lagged) k months before the episode's peak."""
    by = {_key(r["t"]): r for r in rows}
    y, m = _ym(ep["peak"])
    out = {}
    for k in offsets:
        yy, mm = _shift(y, m, -k)
        r = by.get(f"{yy:04d}-{mm:02d}")
        if not r:
            continue
        out[f"t-{k}"] = {"risk": _r(r.get("compositeP")), "fund": _r(r.get("fundamental")), "mkt": _r(r.get("market")),
                         "frag": _r(r.get("fragility")), "growth": _r(r.get("growth")),
                         "pillars": {p: _r(v) for p, v in (r.get("pillars") or {}).items() if v is not None},
                         "regime": r.get("regime"), "blind": bool(r.get("blind"))}
    return out


def _r(v):
    return None if v is None else round(v, 1)


def first_warnings(rows, ep, labels, window=24, level=80, top=6):
    """Indicators that moved into their top fifth of stress first (and stayed there 2 of the next 3 months), within
    `window` months before the peak - the system's own answer to 'where did the first warnings occur'."""
    by = {_key(r["t"]): i for i, r in enumerate(rows)}
    y, m = _ym(ep["peak"])
    end = by.get(ep["peak"])
    if end is None:
        return []
    ys, ms = _shift(y, m, -window)
    start = by.get(f"{ys:04d}-{ms:02d}", 0)
    hits = []
    seen, below = set(), set()
    for i in range(start, end + 1):
        sc = rows[i].get("scores") or {}
        for k, v in sc.items():
            if v is None or labels.get(k, ("", ""))[1] == "growth":
                continue
            if v < 70:
                below.add(k)           # a warning is a CROSSING - an indicator already hot from the last episode isn't one
                continue
            if k in seen or v < level or k not in below:
                continue
            nxt = [((rows[j].get("scores") or {}).get(k) or 0) >= 70 for j in range(i + 1, min(i + 4, len(rows)))]
            # ... and still warning in the last 6 months before the peak (a spike from the previous episode that faded isn't this one's)
            late = any(((rows[j].get("scores") or {}).get(k) or 0) >= 70 for j in range(max(i, end - 6), end + 1))
            if len(nxt) >= 2 and sum(nxt) >= 2 and late:
                seen.add(k)
                hits.append({"key": k, "label": labels.get(k, (k,))[0], "pillar": labels.get(k, ("", ""))[1],
                             "month": _key(rows[i]["t"]), "lead": end - i, "score": round(v, 1)})
    hits.sort(key=lambda h: (-h["lead"], -h["score"]))
    return hits[:top]


def track_records(rows, market, labels, level=80, horizon=730, dd=-0.20):
    """z198 - each indicator's record as a WARNING over the whole panel. A warning = the same crossing first_warnings()
    looks for (from below 70 into the top fifth, 2 of the next 3 months still >= 70; the next one only after it has
    cooled below 70 again). A hit = a 20 % fall in the S&P 500 or a recession start within `horizon` days. Compared
    with the base rate - the share of all months followed by such an event - so a noisy series can't pass as a prophet.
    -> {"byKey": {key: {n, hits, rate, lift, lead (median months to the event)}}, "base", "rule", "horizon"}"""
    ts = [r["t"] for r in rows]

    def event_after(t):
        """months from t to the first event within the horizon, or None"""
        w = market.window(t, horizon) if market and market.d else None
        best = None
        if w and w[0] > 0:
            p0 = w[0]
            i0 = bisect.bisect_right(market.d, t)
            for j, v in enumerate(w[1]):
                if v / p0 - 1 <= dd:
                    best = (market.d[i0 + j] - t).days / 30.44
                    break
        for s0 in getattr(market, "rec", []) or []:
            if t < s0 <= t + timedelta(days=horizon):
                m = (s0 - t).days / 30.44
                best = m if best is None else min(best, m)
                break
        return best

    def known(t):
        return bool(market and market.d and market.d[-1] >= t + timedelta(days=horizon))

    base_n = base_hits = 0
    ev_cache = {}
    for t in ts:
        if not known(t):
            continue
        e = event_after(t)
        ev_cache[t] = e
        base_n += 1
        base_hits += e is not None
    base = base_hits / base_n if base_n else None
    by = {}
    keys = {k for r in rows for k in (r.get("scores") or {})}
    for k in keys:
        if labels.get(k, ("", ""))[1] == "growth":
            continue
        armed, n, hits, leads = False, 0, 0, []
        for i, r in enumerate(rows):
            v = (r.get("scores") or {}).get(k)
            if v is None:
                continue
            if v < 70:
                armed = True
                continue
            if not armed or v < level:
                continue
            nxt = [((rows[j].get("scores") or {}).get(k) or 0) >= 70 for j in range(i + 1, min(i + 4, len(rows)))]
            if len(nxt) < 2 or sum(nxt) < 2:
                continue
            armed = False
            t = r["t"]
            if t not in ev_cache:
                continue                      # horizon not over yet: an open warning, not graded
            n += 1
            e = ev_cache[t]
            if e is not None:
                hits += 1
                leads.append(e)
        if n:
            leads.sort()
            by[k] = {"n": n, "hits": hits, "rate": round(hits / n, 3),
                     "lift": round((hits / n) / base, 2) if base else None,
                     "lead": round(leads[len(leads) // 2], 1) if leads else None}
    return {"byKey": by, "base": None if base is None else round(base, 3), "horizon": horizon,
            "rule": (f"a warning = a crossing from below 70 into the top fifth (>= {level}) that holds 2 of the next 3 months; "
                     f"hit = a {abs(dd) * 100:.0f} % fall or a recession start within {round(horizon / 365)} years")}


def fingerprint(rows, ep, k=6):
    """The pillar scores k months before the peak (the shape of the run-up)."""
    r = readings(rows, ep, offsets=(k,)).get(f"t-{k}")
    return (r or {}).get("pillars") or {}


def similarity(a, b):
    """Cosine similarity of two pillar vectors centred at 50 (above / below normal), over the pillars both have;
    -> (similarity, shared features)."""
    keys = [k for k in a if k in b and a[k] is not None and b[k] is not None]
    if len(keys) < 3:
        return None, []
    va = [a[k] - 50 for k in keys]
    vb = [b[k] - 50 for k in keys]
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(x * x for x in vb))
    if na < 1e-9 or nb < 1e-9:
        return None, []
    sim = sum(x * y for x, y in zip(va, vb)) / (na * nb)
    shared = [k for k in keys if (a[k] >= 65 and b[k] >= 65) or (a[k] <= 35 and b[k] <= 35)]
    return round(sim, 3), shared


def analyze(rows, now_pillars, labels, pillar_names):
    """Every episode's run-up readings, first warnings and fingerprint; and today's closest rhymes."""
    eps = []
    for ep in EPISODES:
        e = {k: ep[k] for k in ("id", "name", "kind", "peak", "trough", "broke", "warnings", "policy", "gap", "lesson")}
        e["recession"] = list(ep["rec"]) if ep.get("rec") else None
        if ep.get("data"):
            e["readings"] = readings(rows, ep)
            e["firstWarnings"] = first_warnings(rows, ep, labels)
            e["fingerprint"] = fingerprint(rows, ep)
        eps.append(e)
    rh = []
    for e in eps:
        fp = e.get("fingerprint") or {}
        sim, shared = similarity(now_pillars or {}, fp)
        if sim is None:
            continue
        rh.append({"id": e["id"], "name": e["name"], "similarity": sim,
                   "shared": [pillar_names.get(k, k) for k in shared],
                   "lesson": e["lesson"]})
    rh.sort(key=lambda x: -x["similarity"])
    return eps, rh[:5]

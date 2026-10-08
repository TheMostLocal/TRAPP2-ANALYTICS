"""Synthetic tests for pipeline/patterns_engine.py - run: python tests/test_patterns.py"""
import sys, random, math
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pipeline'))
import patterns_engine as pe

def path(points, noise=0.002, seed=1):
    """points: [(bars, price), ...] piecewise-linear with small multiplicative noise"""
    rnd = random.Random(seed); out = [points[0][1]]
    for (n, p) in points[1:]:
        a = out[-1]
        for k in range(1, n + 1):
            out.append((a + (p - a) * k / n) * (1 + rnd.gauss(0, noise)))
    return out

ok = fail = 0
def check(name, cond):
    global ok, fail
    print(('PASS ' if cond else 'FAIL ') + name); ok += bool(cond); fail += not cond

HS = [(0,100),(40,130),(15,115),(20,145),(20,116),(15,131)]
def find(c, key):
    return [d for d in pe.detect_patterns(c, templates=(key,))]

c = path(HS + [(30,112),(40,80)])
d = find(c, 'hs_top')
check(f'H&S top identified ({len(d)})', len(d) >= 1)
h = d[-1] if d else {}
check(f"  confirmed then target hit -> success (status={h.get('status')}, ret={h.get('returnPct')}%)", h.get('status') == 'success')
check('  pivots ordered LS<T1<H<T2<RS and head highest', h and [p['type'] for p in h['pivots']] == list('HLHLH') and h['pivots'][2]['price'] == max(p['price'] for p in h['pivots']))
check('  target = neckline - height (measured move)', h and h.get('target') and h['target'] < min(p['price'] for p in h['pivots']))

c2 = path(HS + [(25,125),(25,160)], seed=2)
d2 = find(c2, 'hs_top'); s2 = d2[-1]['status'] if d2 else None
check(f'H&S top then breakout above head -> invalidated (misidentified) ({s2})', s2 == 'invalidated')

c3 = path(HS + [(15,124),(10,128),(10,123),(10,127),(10,124),(10,126)], seed=3)
d3 = find(c3, 'hs_top'); s3 = d3[-1]['status'] if d3 else None
check(f'H&S top never breaks neckline within 40 bars -> expired ({s3})', s3 == 'expired')

c4 = path(HS + [(20,110),(15,108),(30,170)], seed=4)
d4 = find(c4, 'hs_top'); s4 = d4[-1]['status'] if d4 else None
check(f'H&S top confirmed, then rallies through the head -> failure ({s4})', s4 == 'failure')

inv = [(0,200),(40,160),(15,175),(20,140),(20,174),(15,158),(30,180),(40,215)]
c5 = path(inv, seed=5); d5 = find(c5, 'hs_bottom'); s5 = d5[-1]['status'] if d5 else None
check(f'Inverse H&S -> success ({s5})', s5 == 'success')

dt = [(0,100),(40,140),(20,120),(20,141),(25,115),(40,95)]
c6 = path(dt, seed=6); d6 = find(c6, 'double_top'); s6 = d6[-1]['status'] if d6 else None
check(f'Double top -> success ({s6})', s6 == 'success')

# no lookahead: truncating at the identification bar still finds it; one bar earlier doesn't
i = h['identIdx']
check('no lookahead: found on the identification bar', any(x['identIdx'] == i for x in find(c[:i+1], 'hs_top')))
check('no lookahead: NOT found one bar earlier', not any(x['identIdx'] == i for x in find(c[:i], 'hs_top')))
check('no lookahead: open when no bars after identification', [x for x in find(c[:i+1],'hs_top') if x['identIdx']==i][0]['status'] == 'open')

# false-positive sanity on random walks (20 x 2000 bars)
tot = 0; bars = 0
for sd in range(20):
    r = random.Random(100 + sd); x = [100.0]
    for _ in range(2000): x.append(x[-1] * (1 + r.gauss(0.0003, 0.015)))
    tot += len(pe.detect_patterns(x, templates=('hs_top','hs_bottom'))); bars += 2000
print(f'  random walks: {tot} H&S identifications in {bars} bars (~{tot/bars*252:.2f}/yr)')
check('random walk H&S rate is rare (< 1 per year)', tot / bars * 252 < 1.0)

# Minervini: steady uptrend with strong RS -> fresh 10/10 then success vs flat SPY
up = path([(0,50),(300,60),(250,120),(100,150)], noise=0.004, seed=7)
ev = pe.minervini_series(up, rs_pct=[90]*len(up))
sig = pe.minervini_signals(up, ev, spy_by_idx=lambda k: 100.0)
check(f'Minervini fresh 10/10 found ({len(sig)})', len(sig) >= 1 and sig[0]['score'] == 10)
check('Minervini signal judged vs SPY (beat -> success)', sig and sig[0]['status'] == 'success' and sig[0]['excessPct'] > 0)
ev_low = pe.minervini_series(up, rs_pct=[50]*len(up))
check('RP <= 70 caps the score at 9/10', max(e['score'] for e in ev_low if e) == 9)

st = pe.summarize([dict(template='hs_top', status=s, returnPct=r, fwd20Pct=r) for s, r in [('success',10),('failure',-5),('invalidated',-3),('expired',None),('success',8)]], baseline_fwd20=1.0)
check(f"summary: success 2/3 judged, ident 2/5, Wilson CI present ({st['successRate']}, {st['identificationSuccessRate']}, {st['successCI95']})",
      st['successRate'] == 66.7 and st['identificationSuccessRate'] == 40.0 and st['successCI95'][0] is not None)
print(f'\n{ok} passed, {fail} failed')

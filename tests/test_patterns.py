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

# ------------------------------------------------------------- v2 patterns ----
def vol_series(n, base=1000.0, segs=()):
    v = [base] * n
    for a, b, f in segs:
        for k in range(a, min(b, n)):
            v[k] = base * f
    return v

def last_of(c, key, v=None):
    d = [x for x in pe.detect_patterns(c, templates=(key,), v=v)]
    return d[-1] if d else None

# VCP: stage-2 run-up, then 3 contractions 20% -> 11% -> 6%, volume drying up, breakout
vcp_pts = [(0,50),(250,100),(25,80),(25,99),(20,88),(20,98),(15,92),(15,99),(30,120)]
c = path(vcp_pts, noise=0.0015, seed=11)
n0 = 250
v = vol_series(len(c), segs=((n0, n0+50, 1.6), (n0+50, n0+90, 1.0), (n0+90, n0+120, 0.6)))
d = last_of(c, 'vcp', v)
check(f"VCP identified with 3 contractions -> {d and d['status']} ({d and len(d['pivots'])} pivots)", d and d['status'] == 'success' and len(d['pivots']) == 6)
check('VCP without volume data is never identified (volume rule fails)', last_of(c, 'vcp', None) is None)

# Cup & handle
ch = [(0,60),(80,100),(30,80),(30,75),(30,80),(30,99),(10,93),(15,100),(30,130)]
c = path(ch, noise=0.0015, seed=12); d = last_of(c, 'cup_handle')
check(f"Cup & handle -> {d and d['status']} (target {d and d.get('target')})", d and d['status'] == 'success' and d['target'] > 115)

# Bull flag / bear flag
bf = [(0,55),(15,50),(10,60),(8,56),(10,61),(20,75)]   # flag pullback must exceed the zigzag threshold (~5.5% here)
c = path(bf, noise=0.001, seed=13); d = last_of(c, 'bull_flag')
check(f"Bull flag -> {d and d['status']}", d and d['status'] == 'success')
brf = [(0,45),(15,50),(10,40),(8,43),(10,39),(20,28)]
c = path(brf, noise=0.001, seed=14); d = last_of(c, 'bear_flag')
check(f"Bear flag -> {d and d['status']}", d and d['status'] == 'success')

# Ascending / descending triangle
at = [(0,70),(60,100),(15,88),(15,100.5),(12,94),(12,101.5),(30,120)]
c = path(at, noise=0.001, seed=15); d = last_of(c, 'asc_triangle')
check(f"Ascending triangle -> {d and d['status']}", d and d['status'] == 'success')
dtri = [(0,130),(60,100),(15,112),(15,99.5),(12,106),(12,98.5),(30,80)]
c = path(dtri, noise=0.001, seed=16); d = last_of(c, 'desc_triangle')
check(f"Descending triangle -> {d and d['status']}", d and d['status'] == 'success')

# 52-week breakout on volume (and the same chart WITHOUT the volume surge)
bo = [(0,60),(150,100),(40,85),(90,96),(5,102),(40,125)]
c = path(bo, noise=0.001, seed=17)
i_bo = next(i for i in range(253, len(c)) if c[i] > max(c[i-252:i]))
v = vol_series(len(c), segs=((i_bo, i_bo+1, 3.0),))
d = [x for x in pe.detect_patterns(c, templates=('breakout_52w',), v=v)]
check(f"52-week breakout on 3x volume identified + confirmed same bar -> {d and d[0]['status']}", d and d[0]['identIdx'] == i_bo and d[0]['confirmIdx'] == i_bo and d[0]['status'] == 'success')
check('same breakout on normal volume is NOT identified', not pe.detect_patterns(c, templates=('breakout_52w',), v=vol_series(len(c))))

# manual-review candidates + rule names line up with every template
c = path(vcp_pts, noise=0.0015, seed=11); v = vol_series(len(c))
piv = pe.zigzag(c)
cand = pe.latest_candidate(c, piv, 'vcp', v=v)
check(f"VCP candidate lists every rule even when invalid ({cand and cand['passes']}/{cand and cand['of']})", cand and cand['of'] == 8 and not cand['valid'])
names = pe.rule_names()
ok_names = all(len(names[k]) == len((pe.latest_candidate(c, piv, k, v=v) or {'criteria': names[k]})['criteria']) for k in pe.PATTERN_KEYS)
check(f"rule_names covers all {len(names)} templates and matches candidate rule counts", set(names) == set(pe.TEMPLATES) and ok_names)

# no lookahead for the v2 templates too
c = path(ch, noise=0.0015, seed=12); d = last_of(c, 'cup_handle'); i = d['identIdx']
check('cup & handle: no lookahead (found at ident bar, not one bar earlier)',
      any(x['identIdx'] == i for x in pe.detect_patterns(c[:i+1], templates=('cup_handle',))) and
      not any(x['identIdx'] == i for x in pe.detect_patterns(c[:i], templates=('cup_handle',))))
c = path(bo, noise=0.001, seed=17); v = vol_series(len(c), segs=((i_bo, i_bo+1, 3.0),))
check('52w breakout: no lookahead (needs only bars up to the breakout close)',
      any(x['identIdx'] == i_bo for x in pe.detect_patterns(c[:i_bo+1], templates=('breakout_52w',), v=v[:i_bo+1])))
print(f'\n{ok} passed, {fail} failed')

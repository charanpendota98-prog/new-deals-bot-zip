"""Public-channel safety policy: Python bot and WhatsApp bridge must agree.

Run: python3 test_safety_filter.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'bestgaa'))

import safety_filter as sf  # noqa: E402

CASES = json.loads((ROOT / 'tests/safety_cases.json').read_text())['cases']
POLICY = json.loads((ROOT / 'bestgaa/safety_rules.json').read_text())
failures = []


def check(name, condition, detail=''):
    if condition:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}  {detail}')
        failures.append(name)


print(f'Safety policy v{POLICY["version"]}: {len(POLICY["categories"])} categories, '
      f'{len(CASES)} cases')

# ---- 1. Python verdicts -----------------------------------------------------
print('\n[1] Python verdicts')
for case in CASES:
    verdict = sf.scan(case['text'], case.get('links'))
    got = verdict['category'] if verdict['blocked'] else 'clean'
    check(f'py: {case["text"][:52]!r} -> {case["expect"]}',
          got == case['expect'],
          f'got {got} / severity={verdict["severity"]} matched={verdict["matched"]!r} '
          f'({case.get("note", "")})')
    if verdict['blocked']:
        check(f'py: block carries a reason for {case["text"][:32]!r}',
              bool(verdict['reason'].strip()) and verdict['severity'] == 'block',
              repr(verdict))

# ---- 2. JavaScript verdicts are byte-identical ------------------------------
print('\n[2] Python == JavaScript (same JSON, same corpus)')
script = """
import fs from 'node:fs';
import { scan } from './tg-wa-bridge/safety_filter.js';
const cases = JSON.parse(fs.readFileSync('tests/safety_cases.json', 'utf8')).cases;
console.log(JSON.stringify(cases.map(c => scan(c.text, c.links || undefined))));
"""
try:
    js = json.loads(subprocess.check_output(
        ['node', '--input-type=module', '-e', script], cwd=ROOT, text=True))
except (FileNotFoundError, subprocess.CalledProcessError) as exc:  # pragma: no cover
    js = None
    print(f'  SKIP  node unavailable ({exc})')
if js is not None:
    mismatches = 0
    for case, counterpart in zip(CASES, js, strict=True):
        mine = sf.scan(case['text'], case.get('links'))
        if mine != counterpart:
            mismatches += 1
            print(f'  FAIL  parity {case["text"][:48]!r}\n        py={mine}\n        js={counterpart}')
    check(f'parity on {len(CASES)} cases (decision, category, match, reason)', mismatches == 0,
          f'{mismatches} mismatched')

# ---- 3. Evasion cannot slip through ----------------------------------------
print('\n[3] evasion is normalised, never posted')
evasions = ['s.e.x', 's e x', 's-e-x', 'S.E.X', 's3x', '5ex', '$ex', 'S E X',
            'ｓｅｘ', 's_e_x', 'p.o.r.n', 'p0rn', 'b00bs', 'b**bs'.replace('*', 'o'),
            'm.a.d.a.r.c.h.o.d']
for text in evasions:
    verdict = sf.scan(f'new {text} clip')
    check(f'blocked: {text!r}', verdict['blocked'], repr(verdict))

# ---- 4. innocent deals are never refused ----------------------------------
print('\n[4] real products are not refused')
innocent = ['unisex hoodie', 'XXXL kurta', '2XL track pants', '3XL shirt', 'Essex',
            'Sussex', 'cocktail shaker', 'cockroach gel', 'Dickens novel', 'analog watch',
            'analogue clock', 'data analysis course', 'cumin seeds', 'cummin powder',
            'cumulative discount', 'massage gun', 'massage chair', 'bra strap', 'bra 2 pack',
            'panties 3 pack', 'lingerie set', 'sanitary pads', 'menstrual cup',
            'Prestige cooker 5L', 'Boat earbuds', 'HP laptop 15s', '₹99 offer', '99.9% germs',
            'Brand new arrival', 'Nike shoes size 9', 'Classmate notebook 6 pack']
for text in innocent:
    verdict = sf.scan(text)
    check(f'clean: {text!r}', not verdict['blocked'], repr(verdict))

# ---- 5. operator switches --------------------------------------------------
print('\n[5] operator switches')
os.environ['SAFETY_ALLOW_INTIMATE_PRODUCTS'] = 'true'
sf.reset_cache()
check('SAFETY_ALLOW_INTIMATE_PRODUCTS=true allows condoms',
      not sf.scan('Durex condoms pack of 10')['blocked'])
os.environ.pop('SAFETY_ALLOW_INTIMATE_PRODUCTS')
os.environ['SAFETY_ALLOW_SUGGESTIVE'] = 'true'
check('SAFETY_ALLOW_SUGGESTIVE=true downgrades sexy to review',
      sf.scan('sexy dress offer')['severity'] == 'review'
      and not sf.scan('sexy dress offer')['blocked'])
os.environ.pop('SAFETY_ALLOW_SUGGESTIVE')
os.environ['SAFETY_STRICT_REVIEW'] = 'true'
os.environ['SAFETY_ALLOW_SUGGESTIVE'] = 'true'
check('SAFETY_STRICT_REVIEW=true blocks even review findings',
      sf.scan('sexy dress offer')['blocked'])
os.environ.pop('SAFETY_STRICT_REVIEW')
os.environ.pop('SAFETY_ALLOW_SUGGESTIVE')
os.environ['SAFETY_EXTRA_BLOCK_TERMS'] = 'replica,first copy'
check('SAFETY_EXTRA_BLOCK_TERMS blocks an operator term',
      sf.scan('first copy shoes')['blocked']
      and sf.scan('first copy shoes')['category'] == 'operator_term')
check('SAFETY_EXTRA_BLOCK_TERMS does not hit inside words',
      not sf.scan('replicable test kit')['blocked'])
os.environ.pop('SAFETY_EXTRA_BLOCK_TERMS')
sf.reset_cache()

# ---- 6. policy shape and no silent failure --------------------------------
print('\n[6] policy integrity')
summary = sf.policy_summary()
check('every category has key/label/severity/why',
      all(c['key'] and c['label'] and c['severity'] in ('block', 'review') and c['why']
          for c in summary['categories']), repr(summary['categories']))
check('every block category has patterns or domains',
      all(c['patterns'] or c['domains'] for c in summary['categories']
          if c['severity'] == 'block'), repr(summary['categories']))
check('intimate_product and suggestive are block by default',
      {c['key']: c['severity'] for c in summary['categories']}
      .get('intimate_product') == 'block'
      and {c['key']: c['severity'] for c in summary['categories']}.get('suggestive') == 'block')
check('rules file is valid JSON with a version', bool(summary['version']), repr(summary['version']))
try:
    sf.load_rules(ROOT / 'tests/safety_cases.json')
    failures.append('broken rules must raise')
    print('  FAIL  broken rules file must raise')
except (ValueError, KeyError, TypeError):
    print('  PASS  broken rules file raises instead of allowing everything')
else:
    print('  FAIL  broken rules file raises instead of allowing everything')
    failures.append('broken rules file must raise')

# ---- 7. URLs: only our links, and adult domains are refused ---------------
print('\n[7] links')
check('adult domain inside a normal-looking caption',
      sf.scan('deal of the day https://www.xvideos.com/video123')['blocked'])
check('betting domain inside a normal-looking caption',
      sf.scan('loot offer https://1xbet.com/promo')['blocked'])
check('shop links stay clean',
      not sf.scan('https://www.flipkart.com/p/itm123')['blocked']
      and not sf.scan('https://www.myntra.com/tshirt/1')['blocked']
      and not sf.scan('https://bitli.in/gv6qjCD')['blocked'])

print()
if failures:
    print(f'SAFETY: {len(failures)} FAILED')
    for name in failures:
        print(f'  - {name}')
    raise SystemExit(1)
print(f'SAFETY: all checks PASS ({len(CASES)} shared cases, Python and JavaScript agree)')

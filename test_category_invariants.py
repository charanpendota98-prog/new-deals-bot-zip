"""Deterministic adversarial/metamorphic checks: no network, no bot import.

Every case has a policy expectation, not just agreement between two parsers.
"""
import json
import subprocess
from itertools import combinations
from pathlib import Path

from bestgaa.category_classifier import classify_category, load_rules

ROOT = Path(__file__).resolve().parent
rules = load_rules()
cases = []

def add(text, expected, links=()):
    cases.append({"text": text, "links": list(links), "expected": expected})

products = {"fashion": "cotton kurti", "electronics": "wireless earbuds",
            "home_kitchen": "rice cooker", "footwear": "running shoes",
            "beauty": "face wash", "grocery": "basmati rice", "accessories": "backpack"}
for category, phrase in products.items():
    for variant in (phrase, phrase.upper(), phrase.title(), phrase.replace(' ', '-'),
                    ''.join(chr(ord(c) + 0xFEE0) if 'a' <= c <= 'z' else c for c in phrase),
                    phrase.replace(' ', '\u00a0')):
        add(variant, category)
    # Repeated words/URLs must not manufacture a second category or confidence.
    for count in (1, 2, 12):
        add('\n'.join([phrase] * count), category)
    for host in ('bit.ly', 'www.bit.ly', 'hypd.store', 'www.hypd.store', 't.me'):
        add(phrase + ' https://' + host + '/shoe-rack', category)
    add('Deal', category, ['https://shop.test/' + phrase.replace(' ', '-') + '/p/abc12345'])
for a, b in combinations(products.values(), 2):
    for separator in (' and ', '\n', '\r', '\u2028', '&#10;', ' https://bit.ly/opaque '):
        add(a + separator + b, None)
for separator in ('\n', '\r', '\v', '\f', '\x85', '\u2028', '\u2029', '&#10;', '&#13;',
                  '&NewLine;', ' https://bit.ly/opaque ', ' https://hypd.store/93944/afflink/token '):
    add('rice' + separator + 'cooker', None)
for url in ('https://shop.test/rice/../cooker', 'https://shop.test/rice/%2e%2e/cooker',
            'https://shop.test/rice%0acooker', 'https://shop.test/%FF/rice%2Dcooker',
            'https://shop.test/rice%ZZcooker', 'https://user@shop.test/rice-cooker',
            'https://%62it.ly/rice-cooker', 'https://shop.test:invalid/rice-cooker',
            'https://shop.test/rice%1Ccooker', 'https://shop.test/rice%C2%A0cooker'):
    add('Mystery ' + url, None)
for word in ('shoes', 'earbuds', 'shirts'):
    for marker in ('p', 'dp', 'product', 'item', '%70'):
        add('Mystery https://shop.test/' + marker + '/' + word, None)
for delimiter in ('%0A', '%0D', '%E2%80%A8', '%26%2310%3B'):
    add('Mystery https://shop.test/?q=rice' + delimiter + 'cooker', None)
add('&#xFF53;hirt', 'fashion')
add('rice&#32;cooker', 'home_kitchen')

script = """
import fs from 'node:fs';
import {classifyCategory} from './tg-wa-bridge/category_classifier.js';
const {cases,rules}=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify(cases.map(c=>classifyCategory(c.text,c.links,rules))));
"""
js = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script],
    input=json.dumps({'cases': cases, 'rules': rules}).encode(), cwd=ROOT))
for case, counterpart in zip(cases, js, strict=True):
    result = classify_category(case['text'], case['links'], rules)
    assert result['category'] == case['expected'], (case, result)
    assert result == counterpart, (case, result, counterpart)
# Repetition cannot increase scores, even if repeated in supplied URL context.
for phrase in products.values():
    one = classify_category(phrase, rules=rules)
    repeated = classify_category('\n'.join([phrase] * 50), rules=rules)
    assert one == repeated, (phrase, one, repeated)
print(f'Category invariants: {len(cases)} adversarial/metamorphic cases PASS; repetition cannot inflate scores.')

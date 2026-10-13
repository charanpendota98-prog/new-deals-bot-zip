"""Offline shelf regressions and exact Python/JS decision parity."""
import json
import subprocess
from pathlib import Path
from bestgaa.category_classifier import classify_category, load_rules

ROOT = Path(__file__).resolve().parent
cases = json.loads((ROOT / 'tests/category_cases.json').read_text())
script = """
import fs from 'node:fs';
import { classifyCategory } from './tg-wa-bridge/category_classifier.js';
const rules = JSON.parse(fs.readFileSync('tg-wa-bridge/category_rules.json'));
const cases = JSON.parse(fs.readFileSync('tests/category_cases.json'));
console.log(JSON.stringify(cases.map(c => classifyCategory(c.text, c.links || [], rules))));
"""
js = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script], cwd=ROOT))
for case, counterpart in zip(cases, js, strict=True):
    result = classify_category(case['text'], case.get('links', []))
    assert result['category'] == case['expected'], (case, result)
    assert result == counterpart, (case, result, counterpart)
assert len({c['key'] for c in load_rules()['categories']}) == 7
print(f'Category classifier: {len(cases)} cases PASS in Python and JavaScript (decisions, reasons, evidence).')

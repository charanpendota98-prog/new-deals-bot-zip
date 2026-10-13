"""Pure saved-config simulator tests: no network or bot import."""
import copy
import json
from pathlib import Path
from dashboard.preview import preview_batch, route_checks
from bestgaa.channels_config import apply_to_routing

CONFIG = {"channels": [
    {"name": "ClothingShelf", "sources": ["*"], "categories": ["fashion"], "enabled": True},
    {"name": "HomeShelf", "sources": ["deals"], "categories": ["home_kitchen"], "enabled": True},
    {"name": "OffShelf", "sources": ["*"], "categories": ["fashion"], "enabled": False},
    {"name": "GeneralFeed", "sources": ["*"], "categories": [], "enabled": True},
]}
SNAPSHOT = {"main_sources": ["deals", "offers"], "sources": ["deals", "offers", "TrickSource"]}
original = copy.deepcopy(CONFIG)
report = preview_batch([
    {"text": "Cotton kurti", "source": "deals", "expected_category": "fashion"},
    {"text": "Rice cooker", "source": "offers", "expected_category": "home_kitchen"},
    {"text": "Shoes and shoe rack", "source": "deals", "expected_category": None},
    {"text": "Wireless earbuds", "source": "deals", "expected_category": "fashion"},
], CONFIG, SNAPSHOT)
assert report['summary'] == dict(total=4, matched=3, withheld=1, evaluated=4,
                                 expectation_matches=3, expectation_mismatches=1)
checks = {c['channel']: c for c in report['results'][0]['channels']}
assert checks['ClothingShelf']['state'] == 'candidate'
assert checks['OffShelf']['reason'] == 'channel_disabled'
assert checks['HomeShelf']['reason'] == 'category_mismatch'
assert report['results'][1]['channels'][1]['reason'] == 'source_not_selected'
assert report['results'][2]['channels'][0]['reason'] == 'category_withheld'
assert report['results'][2]['channels'][3]['state'] == 'candidate'  # all-category, not send approval
assert CONFIG == original
assert route_checks('fashion', '', CONFIG['channels'], SNAPSHOT)[0]['reason'] == 'source_required'
assert route_checks('fashion', 'deals', CONFIG['channels'], {})[0]['state'] == 'undetermined'
assert route_checks('fashion', 'TrickSource', CONFIG['channels'], SNAPSHOT)[0]['reason'] == 'not_a_main_source'
assert route_checks('fashion', 'newsource', CONFIG['channels'], SNAPSHOT)[0]['state'] == 'blocked'
# Match actual config-merger source eligibility for wildcard/explicit/disabled entries.
routes = {s: [] for s in SNAPSHOT['sources']}
apply_to_routing(routes, set(), CONFIG, tricks_sources={'TrickSource'})
for source in SNAPSHOT['sources']:
    for category in ('fashion', 'home_kitchen', 'electronics', None):
        for check, channel in zip(route_checks(category, source, CONFIG['channels'], SNAPSHOT), CONFIG['channels']):
            expected = channel['name'] in routes[source] and (not channel['categories'] or category in channel['categories'])
            assert (check['state'] == 'candidate') == expected, (source, category, check)
invalid = [None, [], {}, [1], [{'text': ''}], [{'text': 'x' * 12001}],
           [{'text': 'x', 'links': None}], [{'text': 'x', 'source': 1}],
           [{'text': 'x', 'expected_category': []}], [{'text': 'x', 'expected_category': 'bad'}],
           [{'text': 'x'}] * 21, [{'text': 'x' * 12000}] * 6]
for items in invalid:
    try:
        preview_batch(items, CONFIG, SNAPSHOT)
    except ValueError:
        pass
    else:
        raise AssertionError(('accepted invalid batch', str(items)[:80]))
# All shared category fixtures must have the same decision in the batch API path.
cases = json.loads(Path('tests/category_cases.json').read_text())
for i in range(0, len(cases), 20):
    group = cases[i:i+20]
    result = preview_batch([dict(c, expected_category=c['expected']) for c in group], CONFIG, SNAPSHOT)
    assert result['summary']['expectation_mismatches'] == 0
print('Batch preview PASS: bounds, expectations, no mutation, source parity, all 92 classifier fixtures.')

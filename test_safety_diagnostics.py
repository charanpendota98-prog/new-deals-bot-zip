"""Read-only diagnostics must not invent sends, product identity or ownership."""
import contextlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from ops import conversion_report as report
from ops import earnkaro_check as checker

ROOT = Path(__file__).resolve().parent
SHARE = 'https://hypd.store/93944/afflink/diagnostic-token'
PRODUCT = 'https://www.meesho.com/kurti/p/abc12345'
OTHER = 'https://www.meesho.com/earbuds/p/other123'

class Response:
    def __init__(self, url, body='', status=200):
        self.url, self.body, self.status, self.reads = url, body, status, 0
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        self.reads += 1
        return self.body.encode()[:limit]

class DiagnosticSafety(unittest.TestCase):
    def test_resolver_never_reads_merchant_html(self):
        for url, status, expected in [(PRODUCT, 200, PRODUCT), (PRODUCT, 500, ''),
                ('https://www.meesho.com/', 200, ''), (OTHER + '/extra', 200, ''),
                ('https://www.amazon.in/dp/B012345678', 200, '')]:
            with self.subTest(url=url, status=status):
                response = Response(url, PRODUCT, status)
                with patch.object(checker.urllib.request, 'urlopen', return_value=response):
                    self.assertEqual(checker.resolve_hypd_meesho_destination(SHARE, 1)[2], expected)
                self.assertEqual(response.reads, 0)
        error = urllib.error.HTTPError(PRODUCT, 404, 'not found', {}, None)
        with patch.object(checker.urllib.request, 'urlopen', side_effect=error):
            self.assertEqual(checker.resolve_hypd_meesho_destination(SHARE, 1)[2], '')

    def test_wrapper_identity_and_ambiguity(self):
        for body, expected in [(json.dumps({'url': PRODUCT}).replace('/', '\\/'), PRODUCT),
                (f'"{PRODUCT}" "{OTHER}"', ''),
                (f'"{PRODUCT}" "https://www.amazon.in/dp/B012345678"', ''),
                ('"https://www.meesho.com/"', '')]:
            with self.subTest(body=body), patch.object(checker.urllib.request, 'urlopen',
                    return_value=Response(SHARE, body)):
                self.assertEqual(checker.resolve_hypd_meesho_destination(SHARE, 1)[2], expected)
        with patch.object(checker.urllib.request, 'urlopen') as network:
            for bad in ('https://evil.test/93944/afflink/token',
                        'https://hypd.store/93944/afflink', 'https://hypd.store/88888/afflink/token'):
                self.assertEqual(checker.resolve_hypd_meesho_destination(bad, 1)[2], '')
            network.assert_not_called()

    def test_shortener_return_must_be_exact_share(self):
        for returned, status, expected in [(SHARE, '200', (0, 1)), (SHARE, '500', (1, 0)),
                (SHARE, 'HTTP 404', (1, 0)),
                ('https://hypd.store/93944/afflink/different-token', '200', (1, 0)),
                ('https://evil.test/?next=hypd.store/93944/afflink/diagnostic-token', '200', (1, 0)),
                ('https://hypd.store.evil.test/93944/afflink/diagnostic-token', '200', (1, 0))]:
            with self.subTest(returned=returned, status=status), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(checker, 'OUR_HYPD_LINKS', (SHARE,)), \
                    patch.object(checker, 'bitly_tokens', return_value=['dummy']), \
                    patch.object(checker, 'resolve_hypd_meesho_destination', return_value=(SHARE, '200', PRODUCT)), \
                    patch.object(checker, 'bitly_shorten', return_value=('https://bit.ly/test', '')), \
                    patch.object(checker, 'resolve_once', return_value=(returned, status)):
                self.assertEqual(checker.check_our_hypd_links(1, False), expected)

    def test_conversion_report_counts_delivery_evidence_not_renders(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'receipts.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE queue(id INTEGER, created_at REAL, rendered_text TEXT);
                CREATE TABLE deliveries(queue_id INTEGER,target TEXT,status TEXT,last_error TEXT,chunks_sent INTEGER);''')
            db.executemany('INSERT INTO queue VALUES(?,?,?)',
                [(n, time.time(), 'Product https://bit.ly/unproven') for n in range(1, 7)])
            db.executemany('INSERT INTO deliveries VALUES(?,?,?,?,?)', [
                (1, 'a', 'sent', '', 1), (1, 'b', 'sent', '', 1), (1, 'c', 'skipped', 'duplicate', 0),
                (2, 'a', 'skipped', 'duplicate', 0), (3, 'a', 'sent', 'duplicate (old build)', 0),
                (4, 'a', 'pending', '', 0), (5, 'a', 'failed', 'timeout after first chunk', 1),
                (6, 'a', 'held', 'manual review: send outcome unknown', 0)])
            db.commit()
            before = path.read_bytes()
            result = report.db_report(path, 24)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(result['links']['links'], 1)  # once per sent queue, not per target
            self.assertEqual(result['rendered_links']['links'], 6)
            self.assertEqual(result['delivery_summary'], {'evidence': 'status_and_error',
                'rendered_queues': 6, 'queues_with_sends': 1, 'target_sends': 2, 'policy_skips': 3,
                'partial_target_sends': 1, 'held_targets': 1, 'queues_only_skipped': 2, 'queues_without_send_evidence': 5})
            self.assertEqual(result['audit_errors'], [])
            db.execute('DROP TABLE deliveries')
            db.execute('CREATE TABLE deliveries(queue_id INTEGER, status TEXT)')
            db.execute("INSERT INTO deliveries VALUES(1,'sent')")
            db.commit()
            legacy = report.db_report(path, 24)
            self.assertEqual(legacy['links']['links'], 0)
            self.assertEqual(legacy['delivery_summary']['evidence'], 'legacy_schema_unverifiable')
            db.execute('DROP TABLE deliveries')
            db.commit()
            self.assertEqual(report.db_report(path, 24)['delivery_summary']['evidence'], 'unavailable')
            db.close()

    def test_gate_overrides_inherited_operator_paths(self):
        import os
        source = (ROOT / 'ops/test_all.sh').read_text()
        start = source.index('GATE_TMP="$(mktemp ')
        end = source.index("trap 'rm -rf \"$GATE_TMP\"' EXIT", start)
        end = source.index('\n', end)
        # Exercise the actual isolation block, not a second copy of its logic.
        block = source[start:end]
        keys = ['BOT_DB_PATH', 'BOT_LOG_DIR', 'BOT_MEDIA_DIR', 'CHANNELS_JSON_PATH',
                'TELEGRAM_SESSION', 'BESTGAA_DB_PATH']
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / 'operator-file'
            sentinel.write_text('must remain untouched')
            env = dict(os.environ, **{key: str(sentinel) for key in keys})
            probe = 'import os,json; print(json.dumps({k:os.getenv(k) for k in '+repr(keys)+'}))'
            result = subprocess.run(['bash', '-eu', '-c', block + '\n"$1" -c "$2"',
                                     'isolation-check', sys.executable, probe], env=env,
                                    text=True, capture_output=True, check=True)
            paths = json.loads(result.stdout)
            self.assertIsNone(paths.pop('CHANNELS_JSON_PATH'))
            roots = {Path(value).parent for value in paths.values()}
            self.assertEqual(len(roots), 1)
            root = roots.pop()
            self.assertTrue(root.name.startswith('bestgaa-gate-'))
            self.assertFalse(root.exists())  # EXIT trap cleaned only the test directory
            self.assertEqual(sentinel.read_text(), 'must remain untouched')

    def test_bridge_uses_read_only_snapshot_and_rejects_conflicting_pairs(self):
        source = (ROOT / 'tg-wa-bridge/bridge.js').read_text()
        # Execute the actual embedded verifier, without starting WhatsApp/Telegram.
        script = re.search(r'async function verifyBestGaaProvenance.*?const script = String.raw`(.*?)`',
                           source, re.S)[1]
        def verify(path, urls):
            return subprocess.run([sys.executable, '-c', script, str(path), '5478322', 'mama086-21',
                '93944,smartdeals', str(ROOT / 'tg-wa-bridge')], input=json.dumps(urls),
                text=True, capture_output=True, timeout=10)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'missing # db.sqlite3'
            self.assertNotEqual(verify(path, [SHARE]).returncode, 0)
            self.assertFalse(path.exists())  # read checks cannot create a missing live DB
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE link_cache(source_url TEXT,affiliate_url TEXT,resolved_url TEXT,deal_key TEXT);
                CREATE TABLE owned_link_proofs(affiliate_url TEXT,destination_url TEXT,proof_type TEXT,publisher_id TEXT,created_at REAL);
                CREATE TABLE hypd_links(afflink_url TEXT,resolved_url TEXT,product_key TEXT,store TEXT);''')
            db.execute('INSERT INTO hypd_links VALUES(?,?,?,?)', (SHARE, PRODUCT, 'PID:www.meesho.com:abc12345', '93944'))
            short = 'https://bit.ly/proven'
            db.execute('INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)', (short, SHARE, 'hypd', '93944', 1))
            db.commit()
            for destinations, accepted in [([PRODUCT], True), ([PRODUCT, OTHER], False),
                    ([OTHER, PRODUCT], False), ([PRODUCT, PRODUCT], True),
                    (['https://www.amazon.in/dp/B012345678?tag=mama086-21', PRODUCT], False)]:
                with self.subTest(destinations=destinations):
                    db.execute('DELETE FROM link_cache')
                    db.executemany('INSERT INTO link_cache VALUES(?,?,?,?)',
                        [(str(n), short, url, '') for n, url in enumerate(destinations)])
                    db.commit()
                    before = path.read_bytes()
                    result = verify(path, [short])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(short in json.loads(result.stdout), accepted)
                    self.assertEqual(path.read_bytes(), before)
            db.close()

if __name__ == '__main__':
    unittest.main()

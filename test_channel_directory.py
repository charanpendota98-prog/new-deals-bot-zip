"""Every channel the bot session can read -> a pickable list -> live work.

The operator's request: "telegram lo vunna channel anni chupinchandi, search
pettandi, multiple select cheyyandi, categories select cheyyandi, okesari add
cheyyandi" - show the whole account's channels, searchable, multi-select,
categories, one save.

Nothing here touches Telegram. The directory builder is pure, the API runs in
a temporary config directory, and the live-activation pass uses fake entities,
so every claim in this file is checkable offline:

1. what the account can read is listed truthfully (broadcast + @username =
   addable; groups/private visible but refused with a reason);
2. one save adds MANY sources with targets AND categories, partial success is
   explicit and nothing is silently dropped;
3. a watched source's category policy reaches the bot's post path
   (`source_category_blocked`) and blocks only what it must;
4. the activated route is the very dict the message handler reads, so the next
   post from that channel is converted and posted to the chosen target.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace as Entity
from unittest.mock import patch

from bestgaa import channel_directory as directory
from bestgaa import channels_config as cfg
from bestgaa.live_channels import LiveChannels


def dialog(id, title, username=None, broadcast=True, megagroup=False, left=False,
           participants=0, date=0):
    """The shape `iter_dialogs` hands us, reduced to what the builder reads."""
    return Entity(id=id, title=title, username=username, broadcast=broadcast,
                  megagroup=megagroup, gigagroup=False, left=left, deleted=False,
                  participants_count=participants, date=date)


async def dialogs_payload():
    rows = [
        dialog(101, 'Loot Zone India', 'LootZoneIndia11', participants=52000, date=1_700_000_000),
        dialog(102, 'Fashion Finds', 'fashionfinds', participants=9100),
        dialog(103, 'Family Group', None, broadcast=False, megagroup=True),
        dialog(104, 'Private Deals', None),
        dialog(105, 'Left Behind', 'leftbehind', left=True),
        dialog(101, 'Loot Zone India', 'LootZoneIndia11'),          # same channel, second dialog
        dialog(107, 'no username broadcast', None),
        dialog(108, 'Mega Group Dealers', 'megadealers', broadcast=False, megagroup=True,
               participants=400),
    ]
    payload = directory.build(rows, now=1_700_000_500)
    assert payload['schema'] == directory.SCHEMA
    assert payload['count'] == 6, payload['count']          # duplicate folded away
    by_name = {row['username']: row for row in payload['channels'] if row['username']}
    assert by_name['LootZoneIndia11']['addable'] is True
    assert by_name['LootZoneIndia11']['members'] == 52000
    assert by_name['LootZoneIndia11']['title'] == 'Loot Zone India'
    assert by_name['fashionfinds']['addable'] is True
    assert 'leftbehind' not in by_name                        # left = not ours to read
    # Groups are visible (the operator may have a real group) but never addable:
    # a group id is not a channel link and cannot be a broadcast source.
    assert by_name['megadealers']['addable'] is False
    assert by_name['megadealers']['kind'] == 'group'
    # A broadcast channel without a public @username has no link to add.
    nameless = [row for row in payload['channels'] if not row['username']]
    assert nameless and all(row['addable'] is False for row in nameless)
    assert payload['addable'] == 2
    assert payload['skipped'] == 1          # the left channel is dropped, counted
    assert payload['truncated'] == 0
    # Addable rows come first so the picker opens on the useful half.
    flags = [row['addable'] for row in payload['channels']]
    assert flags == sorted(flags, reverse=True)
    assert payload['generated_at'] == 1_700_000_500
    assert 'Nothing here was joined' in payload['note']
    # Hostile titles stay data, and the id is always present for the UI key.
    hostile = directory.build([dialog(300, '<img src=x onerror=alert(1)>', 'xsschan')])
    assert hostile['channels'][0]['title'].startswith('<img')
    assert hostile['channels'][0]['id'] == 300
    # Bounded: a huge account is truncated deliberately and says so.
    big = directory.build([dialog(1000 + i, f'c{i}', f'chan{i}') for i in range(1500)], limit=1000)
    assert big['count'] == 1000 and big['truncated'] == 500 and big['limit'] == 1000
    # Junk entities never crash the loop that produces the list.
    assert directory.build([None, Entity(id=None, title='x'), dialog(0, '')])['channels'] == []
    print('Channel directory PASS: read-only dialog snapshot, duplicate folded, '
          'addable = broadcast + public username, groups/private listed with a reason, '
          'bounded and hostile-safe.')


def config_paths():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        with patch.dict(os.environ, {'CHANNELS_JSON_PATH': str(base / 'channels.json')}):
            assert cfg.directory_path() == base / 'telegram_channels.json'
        with patch.dict(os.environ, {'CHANNELS_JSON_PATH': str(base / 'channels.json'),
                                     'CHANNEL_DIRECTORY_PATH': str(base / 'dir.json')}):
            assert cfg.directory_path() == base / 'dir.json'
    # Categories are a source-level policy: normalised, bounded, and never invented.
    entry = {'channel': '@FashionFinds', 'targets': ['*'],
             'categories': ['Fashion', 'ELECTRONICS', 'fashion']}
    cleaned, errors, _ = cfg.validate_watch(entry, {'LootZoneIndia11'},
                                            known_categories={'fashion', 'electronics'})
    assert not errors, errors
    assert cleaned['categories'] == ['fashion', 'electronics'], cleaned['categories']
    bad, errors, _ = cfg.validate_watch({'channel': '@somefeed', 'categories': ['nonsense']},
                                        {'LootZoneIndia11'}, known_categories={'fashion'})
    assert errors and 'unknown category' in errors[0], errors
    too_many, errors, _ = cfg.validate_watch(
        {'channel': '@somefeed', 'categories': [f'c{i}' for i in range(21)]}, {'LootZoneIndia11'})
    assert errors and 'up to 20' in errors[0], errors
    # While the bot has never reported its taxonomy, an unknown key is a warning
    # (forward compatible), never a silent deletion.
    kept, errors, warnings = cfg.validate_watch({'channel': '@somefeed', 'categories': ['NewThing']},
                                                {'LootZoneIndia11'})
    assert not errors and kept['categories'] == ['newthing'] and warnings
    print('Source categories PASS: bounded, deduplicated, validated against the bot\'s own taxonomy.')


async def bulk_api():
    from aiohttp.test_utils import TestClient, TestServer
    from dashboard import app as dash
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        env = {'CHANNELS_JSON_PATH': str(base / 'channels.json'),
               'CHANNEL_DIRECTORY_PATH': str(base / 'telegram_channels.json'),
               'BOT_DB_PATH': str(base / 'missing.sqlite3'),
               'DASHBOARD_TOKEN': 'studio-test', 'DASHBOARD_RESTART_CMD': ''}
        with patch.dict(os.environ, env):
            cfg.save_channels({'channels': []})
            cfg.snapshot_path().write_text(json.dumps({
                'targets': ['LootZoneIndia11', 'PowerLoots1'],
                'main_feed': ['LootZoneIndia11', 'PowerLoots1'],
                'sources': ['OldFeed'],
                'watch_states': {}, 'watch_configured': []}))
            headers = {'X-Dashboard-Token': 'studio-test'}

            async with TestClient(TestServer(dash.create_app())) as client:
                # 1. Before the bot has ever published: an empty directory with a
                #    reason, never a 500 and never a made-up list.
                response = await client.get('/api/directory', headers=headers)
                assert response.status == 200
                empty = await response.json()
                assert empty['directory']['available'] is False
                assert empty['channels'] == []
                assert 'not published' in empty['directory']['reason']
                assert (await client.get('/api/directory')).status == 401

                # 2. The bot publishes its snapshot; the picker now knows the account.
                payload = directory.build([
                    dialog(101, 'Loot Zone India', 'LootZoneIndia11'),
                    dialog(102, 'Fashion Finds', 'fashionfinds'),
                    dialog(103, 'We Post Here', 'powerloots1'),
                    dialog(104, 'Family Group', None, broadcast=False, megagroup=True),
                ], now=1_700_000_000)
                payload['our_targets'] = ['LootZoneIndia11', 'PowerLoots1']
                (base / 'telegram_channels.json').write_text(json.dumps(payload))

                data = await (await client.get('/api/directory', headers=headers)).json()
                rows = {row['username'].lower(): row for row in data['channels'] if row['username']}
                assert data['directory']['available'] is True
                assert set(rows) == {'lootzoneindia11', 'fashionfinds', 'powerloots1'}
                assert rows['fashionfinds']['addable'] is True
                assert rows['lootzoneindia11']['is_our_target'] is True
                assert rows['powerloots1']['is_our_target'] is True
                assert any(not row['username'] for row in data['channels'])   # group still listed
                assert data['catalog']['main_feed'], 'the picker needs to know the main feed'
                assert data['categories'], 'the picker needs the installed category keys'

                # 3. One save, MANY sources, with targets and categories.
                body = {'channels': ['fashionfinds', '@lootzoneindia11', 'powerloots1',
                                     'nonsense-name', 'fashionfinds'],
                        'targets': ['*'], 'categories': ['fashion'], 'note': 'studio'}
                response = await client.post('/api/watch/bulk', headers=dict(
                    headers, **{'If-Match': data['revision']}), json=body)
                assert response.status == 200, await response.text()
                out = await response.json()
                assert out['added'] == ['fashionfinds'], out
                skipped = {row['channel']: row['errors'] for row in out['skipped']}
                assert 'OUR posting channels' in skipped['@lootzoneindia11'][0]
                assert skipped['powerloots1'] and 'OUR posting channels' in skipped['powerloots1'][0]
                assert 'not' in skipped['nonsense-name'][0].lower() or \
                    'link' in skipped['nonsense-name'][0].lower()
                assert 'Nothing is joined' in out['next'] or 'Nothing is joined' in out['next']
                saved = cfg.load_channels()
                assert [row['channel'] for row in saved['watch']] == ['fashionfinds']
                assert saved['watch'][0]['categories'] == ['fashion']
                assert saved['watch'][0]['targets'] == ['*']

                # 4. Categories are validated against the installed taxonomy.
                data = await (await client.get('/api/directory', headers=headers)).json()
                response = await client.post('/api/watch/bulk', headers=dict(
                    headers, **{'If-Match': data['revision']}),
                    json={'channels': ['anotherfeed'], 'categories': ['not-a-category']})
                assert response.status == 400, await response.text()

                # 5. A stale form cannot overwrite a newer save, and a refusal
                #    writes nothing at all.
                before = cfg.load_channels()
                response = await client.post('/api/watch/bulk',
                                             headers=dict(headers, **{'If-Match': 'stale'}),
                                             json={'channels': ['latenightfeed']})
                assert response.status == 409
                assert cfg.load_channels() == before

                # 6. Empty selection / too many / bad body are refused loudly.
                for bad in ({'channels': []}, {'channels': ['x'] * 51}, {}):
                    response = await client.post('/api/watch/bulk', headers=headers, json=bad)
                    assert response.status == 400, bad
                response = await client.post('/api/watch/bulk', headers=headers,
                                             json={'channels': ['x'], 'targets': 'nope'})
                assert response.status == 400
                assert not (base / 'missing.sqlite3').exists()
    print('Source studio API PASS: empty directory explained, whole account listed, '
          'bulk save with targets+categories, per-refusal reasons, stale-form guard.')


async def live_watch():
    """The saved source is watched by the SAME dict the handler reads."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        routing = {'OldFeed': ['LootZoneIndia11']}
        source_map, target_map = {}, {'LootZoneIndia11': Entity(id=1, broadcast=True, creator=True)}
        cfg.save_channels({'channels': [], 'watch': [
            {'channel': 'fashionfinds', 'targets': ['*'], 'categories': ['fashion'],
             'enabled': True, 'note': ''}]}, base / 'channels.json')

        async def resolve_source(name):
            key = name.lower()
            if key in ('fashionfinds', 'fashiondeals'):
                return Entity(id=900, broadcast=True, left=False,
                              title='Fashion Finds', username=key)
            raise ValueError('not a member')

        reports = []
        manager = LiveChannels(
            routing, set(), {}, source_map, target_map, [], {'LootZoneIndia11'},
            {'fashion', 'electronics'}, set(), lambda name: target_map.get(name),
            path=base / 'channels.json', snapshot_file=base / 'snapshot.json',
            resolve_source=resolve_source, builtin_sources=set(),
            main_feed=['LootZoneIndia11'], on_source_added=lambda *a: None,
            on_source_categories=lambda name, cats: reports.append((name, cats)))
        await manager.sync_once()
        state = manager.watch_states['fashionfinds']
        assert state['state'] == 'watching', state
        # Routing is the live global the post path reads, and both chat-id shapes
        # are mapped - a marked id or a raw id must never miss the source.
        assert routing['fashionfinds'] == ['LootZoneIndia11'], routing
        assert source_map[900][0] == 'fashionfinds' and source_map[-1000000000900][0] == 'fashionfinds'
        assert reports[-1] == ('fashionfinds', ['fashion']), reports
        assert 'categories: fashion' in state['detail']

        # Editing the categories in the dashboard takes effect on the next pass,
        # without restarting the bot.
        cfg.save_channels({'channels': [], 'watch': [
            {'channel': 'fashionfinds', 'targets': ['*'], 'categories': ['electronics'],
             'enabled': True, 'note': ''}]}, base / 'channels.json')
        await manager.sync_once()
        assert reports[-1] == ('fashionfinds', ['electronics']), reports
        # The bot's published snapshot carries the policy AND the verdict, so the
        # dashboard shows exactly what the bot will do - not what was typed.
        snapshot = json.loads((base / 'snapshot.json').read_text())
        assert snapshot['watch_categories'] == {'fashionfinds': ['electronics']}, snapshot
        assert snapshot['watch_states']['fashionfinds']['state'] == 'watching'
        assert 'electronics' in snapshot['watch_states']['fashionfinds']['detail']
        # Clearing them means "every category" and removes the restriction.
        cfg.save_channels({'channels': [], 'watch': [
            {'channel': 'fashionfinds', 'targets': ['*'], 'categories': [],
             'enabled': True, 'note': ''}]}, base / 'channels.json')
        await manager.sync_once()
        assert reports[-1] == ('fashionfinds', []), reports
        snapshot = json.loads((base / 'snapshot.json').read_text())
        assert snapshot['watch_categories'] == {}, snapshot['watch_categories']
    print('Live source watching PASS: saved source becomes a live route on the same dict, '
          'both chat-id shapes, category edits applied without a restart.')


def bot_policy():
    """The post path refuses a category the operator excluded for that source."""
    os.environ.setdefault('TELEGRAM_API_ID', '1')
    os.environ.setdefault('TELEGRAM_API_HASH', 'x')
    os.environ.setdefault('EARNKARO_API_KEY', 'x')
    os.environ.setdefault('EARNKARO_PUBLISHER_ID', '5478322')
    os.environ.setdefault('AMAZON_TAG', 'mama086-21')
    try:
        from bestgaa import main_bot_new as bot
    except Exception as exc:                                    # pragma: no cover - env dependent
        print(f'Bot policy SKIPPED (module not importable here: {type(exc).__name__})')
        return
    bot.SOURCE_CATEGORY_FILTERS.clear()
    bot.SOURCE_CATEGORY_FILTERS['fashionfinds'] = frozenset({'fashion'})
    # Allowed category, unknown source, and no restriction all pass silently.
    assert bot.source_category_blocked('fashionfinds', 'fashion') == ''
    assert bot.source_category_blocked('otherfeed', 'electronics') == ''
    blocked = bot.source_category_blocked('FashionFinds', 'electronics')
    assert blocked and 'fashion' in blocked and 'electronics' in blocked, blocked
    # An unclassified post cannot pass a limited source: silence is not consent.
    # Silence is not consent: a post the classifier could not name is dropped by
    # a limited source, and the reason says so.
    for unclassified in ('', None):
        blocked = bot.source_category_blocked('fashionfinds', unclassified)
        assert blocked and 'unclassified' in blocked, (unclassified, blocked)
    assert 'unknown' in bot.source_category_blocked('fashionfinds', 'unknown')
    assert not bot.source_category_blocked('fashionfinds', 'Fashion')
    bot.SOURCE_CATEGORY_FILTERS.clear()
    print('Source category policy PASS: only the chosen categories pass, unclassified never does.')


if __name__ == '__main__':
    asyncio.run(dialogs_payload())
    config_paths()
    asyncio.run(bulk_api())
    asyncio.run(live_watch())
    bot_policy()

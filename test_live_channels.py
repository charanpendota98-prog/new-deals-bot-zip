"""Offline live-add tests: fake Telegram entities, real isolated config/snapshot."""
import asyncio
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from bestgaa import channels_config as cfg
from bestgaa.live_channels import LiveChannels, revision


def entry(name, category='electronics', **kwargs):
    return dict(name=name, sources=['*'], categories=[category], enabled=True, amazon_tag=False, note='', **kwargs)


async def run():
    with tempfile.TemporaryDirectory() as temp:
        path, snap = Path(temp)/'channels.json', Path(temp)/'routing_snapshot.json'
        routes={'deals':['MainFeed'], 'TrickSource':['TrickFeed']}
        tags={'MainFeed'}
        filters={}
        sources={-1000000000101:('deals',['MainFeed']),-1000000000102:('TrickSource',['TrickFeed'])}
        targets={'MainFeed':SimpleNamespace(id=1)}
        calls=[]
        rights={}
        async def resolve(name):
            calls.append(name)
            if name == "RateLimited":
                class RateLimit(Exception):
                    seconds = 600
                raise RateLimit("Telegram asks us to wait")
            await asyncio.sleep(0)
            if name=='ChangingShelf':
                cfg.save_channels({'channels':[]},path)
            return rights.get(name, SimpleNamespace(id=10+len(calls),broadcast=True,creator=True))
        manager=LiveChannels(routes,tags,filters,sources,targets,[],{'MainFeed','TrickFeed'},
                             {'electronics','fashion'}, {'TrickSource'}, resolve,path=path,snapshot_file=snap)
        cfg.save_channels({'channels':[entry('https://t.me/ElectroShelf')]},path)
        await manager.sync_once()
        assert routes=={'deals':['MainFeed','ElectroShelf'],'TrickSource':['TrickFeed']}
        assert filters=={'electroshelf':frozenset({'electronics'})}
        assert tags=={'MainFeed'}
        assert sources[-1000000000101][1]==['MainFeed','ElectroShelf']
        state=json.loads(snap.read_text())
        assert state['live_channels']['ElectroShelf']['state']=='active'
        assert state['applied_revision']==revision(path.read_bytes())
        await manager.sync_once()
        assert calls==['ElectroShelf']  # idempotent, no history/backfill/send call exists
        active=copy.deepcopy(routes)
        # Existing policy changes/deletions never silently mutate delivery routing.
        cfg.save_channels({'channels':[entry('ElectroShelf','fashion')]},path)
        await manager.sync_once()
        assert routes==active and filters['electroshelf']==frozenset({'electronics'})
        assert json.loads(snap.read_text())['live_channels']['ElectroShelf']['state']=='restart_required'
        cfg.save_channels({'channels':[]},path); await manager.sync_once()
        assert routes==active
        assert json.loads(snap.read_text())['live_channels']['ElectroShelf']['state']=='restart_required'
        path.write_text('{broken'); await manager.sync_once()
        assert routes==active and json.loads(snap.read_text())['live_error']
        # A denied new channel cannot stop/mutate existing routing or opt in its tag.
        denied=entry('NoAccess');denied['amazon_tag']=True
        rights['NoAccess']=SimpleNamespace(id=88,broadcast=True,creator=False,admin_rights=None)
        cfg.save_channels({'channels':[entry('ElectroShelf'),denied]},path)
        await manager.sync_once()
        assert routes==active and 'NoAccess' not in tags
        assert json.loads(snap.read_text())['live_channels']['NoAccess']['state']=='needs_attention'
        count=len(calls); await manager.sync_once(); assert len(calls)==count # backoff
        rights['AliasMain']=SimpleNamespace(id=1,broadcast=True,creator=True)
        rights['SourceLoop']=SimpleNamespace(id=101,broadcast=True,creator=True)
        rights['NotChannel']=SimpleNamespace(id=90,broadcast=False,creator=True)
        for name in ('AliasMain','SourceLoop','NotChannel'):
            cfg.save_channels({'channels':[entry('ElectroShelf'),entry(name)]},path)
            await manager.sync_once(); assert routes==active
            assert json.loads(snap.read_text())['live_channels'][name]['state']=='needs_attention'
        # Invalid and duplicate category specs fail closed.
        for entries in ([entry('BadShelf','typo')], [entry('MainFeed')],
                        [entry('SameShelf'),entry('SameShelf')]):
            cfg.save_channels({'channels':entries},path); await manager.sync_once()
            assert routes==active
            assert any(v['state']=='invalid' for v in json.loads(snap.read_text())['live_channels'].values())
        newsource=entry('NewSourceShelf');newsource['sources']=['not_registered']
        cfg.save_channels({'channels':[entry('ElectroShelf'),newsource]},path)
        await manager.sync_once();assert routes==active
        assert json.loads(snap.read_text())['live_channels']['NewSourceShelf']['state']=='waiting_sources'
        # Permission await can race a user removal: recheck before mutation.
        cfg.save_channels({'channels':[entry('ElectroShelf'),entry('ChangingShelf')]},path)
        await manager.sync_once();assert routes==active
        # Another channel may activate without affecting the first, no main tag opt-in.
        rights['ClothingShelf']=SimpleNamespace(id=200,broadcast=True,creator=False,
                                               admin_rights=SimpleNamespace(post_messages=True))
        cfg.save_channels({'channels':[entry('ElectroShelf'),denied,entry('ClothingShelf','fashion')]},path)
        await manager.sync_once()
        assert routes['deals']==['MainFeed','ElectroShelf','ClothingShelf']
        assert filters['clothingshelf']==frozenset({'fashion'}) and tags=={'MainFeed'}
        # A Telegram FloodWait pauses further *new access checks*, not old routes.
        cfg.save_channels({'channels':[entry('ElectroShelf'),entry('RateLimited'),entry('AfterRateLimit')]},path)
        await manager.sync_once()
        assert 'RateLimited' in calls and 'AfterRateLimit' not in calls
        assert routes['deals']==['MainFeed','ElectroShelf','ClothingShelf']
        assert json.loads(snap.read_text())['live_channels']['AfterRateLimit']['state']=='needs_attention'
        # Stop interrupts the poll, no forever-running task in tests.
        stop=asyncio.Event(); stop.set(); await asyncio.wait_for(manager.run(stop),1)
    print('Live channels PASS: public links, access gating, atomic/idempotent add, old routing intact, edits deferred, failures isolated, race checks, source/tag safeguards.')


if __name__=='__main__':
    for value in ('https://t.me/MyChannel','t.me/MyChannel','@MyChannel','MyChannel','https://telegram.me/MyChannel/'):
        assert cfg.normalize_handle(value)=='MyChannel'
    for value in ('https://t.me/+abc','https://t.me/MyChannel/123','https://evil.test/MyChannel',
                  'https://t.me/MyChannel?start=hello','https://user@t.me/MyChannel','https://t.me/c/123/4'):
        _, errors, _ = cfg.validate_channel({'name':value,'sources':['*']},set(),set())
        assert errors,value
    asyncio.run(run())

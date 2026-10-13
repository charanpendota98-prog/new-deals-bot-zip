"""Channel-local source setup: normalization, staged live activation, no joins/replay."""
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as Entity

from bestgaa import channels_config as cfg
from bestgaa.live_channels import LiveChannels


def channel(name, sources):
    return dict(name=name, sources=sources, categories=['electronics'], enabled=True, amazon_tag=False)


def normalization():
    known={'DealsFeed', 'https://telegram.me/+PrivateHash'}
    assert cfg.source_keys(['https://t.me/dealsfeed', '@DealsFeed', 'DEALSFEED'], known)==['DealsFeed']
    assert cfg.source_keys(['@NewSource','newsource'])==['NewSource']
    assert cfg.source_keys(['https://t.me/+PrivateHash'], known)==['https://telegram.me/+PrivateHash']
    assert cfg.source_keys(['t.me/joinchat/PrivateHash'])==['https://t.me/+PrivateHash']
    assert cfg.source_keys([f'Feed{i:03}' for i in range(100)])[-1]=='Feed099'
    for bad in (['*','DealsFeed'],['https://t.me/DealsFeed/10'],['https://evil.test/DealsFeed'],
                ['https://user@t.me/DealsFeed'],['https://t.me/+foo?bar=1'],['https://t.me/c/123/456'],
                [f'Feed{i:03}' for i in range(101)]):
        try: cfg.source_keys(bad)
        except ValueError: pass
        else: raise AssertionError(bad)
    _, errors, _=cfg.validate_channel(channel('DealsFeed',['@DealsFeed']),known,set(),{'electronics'})
    assert errors
    # Wildcard expansion is order-independent and excludes new channel-local intake.
    routes={'DealsFeed':['MainFeed']}
    cfg.apply_to_routing(routes,set(),{'channels':[channel('CustomShelf',['NewSource']),channel('AllShelf',['*'])]})
    assert routes['NewSource']==['CustomShelf'] and routes['DealsFeed']==['MainFeed','AllShelf']


async def activation():
    with tempfile.TemporaryDirectory() as td:
        path,snapshot=Path(td)/'channels.json',Path(td)/'routing_snapshot.json'
        routes={'DealsFeed':['MainFeed'],'TrickFeed':['TrickTarget']}
        sources={-1000000000001:('DealsFeed',['MainFeed'])}
        targets={'MainFeed':Entity(id=2,broadcast=True,creator=True)}
        calls=[]; applied=[]
        source_entities={'FreshFeed':Entity(id=201,broadcast=True,left=False,title='Fresh Deals'),
                         'SecondFeed':Entity(id=202,broadcast=True,left=False,title='Second Deals'),
                         'LoopFeed':Entity(id=2,broadcast=True,left=False),
                         'AliasFeed':Entity(id=1,broadcast=True,left=False),
                         'NotAChannel':Entity(id=203,broadcast=False,left=False)}
        async def resolve_source(name):
            calls.append(name)
            if name=='JoinLater': raise ValueError('Join with the bot session account first')
            if name=='FloodFeed':
                class FloodWait(Exception): seconds=120
                raise FloodWait('wait')
            if name=='RaceFeed':
                cfg.save_channels({'channels':[]},path)
                return Entity(id=204,broadcast=True,left=False)
            return source_entities[name]
        target_ids = {}
        async def resolve_target(name):
            target_ids.setdefault(name, 300 + len(target_ids))
            return Entity(id=target_ids[name],broadcast=True,creator=True)
        manager=LiveChannels(routes,set(),{},sources,targets,[],{'MainFeed','TrickTarget'},
                            {'electronics'}, {'TrickFeed'},resolve_target, path=path,snapshot_file=snapshot,
                            resolve_source=resolve_source,on_source_added=lambda *args:applied.append(args),
                            builtin_sources={'DealsFeed','TrickFeed'})
        desired=channel('SelectedShelf',['https://t.me/DealsFeed','@FreshFeed','SecondFeed'])
        cfg.save_channels({'channels':[desired]},path)
        await manager.sync_once()
        assert routes['FreshFeed']==routes['SecondFeed']==['SelectedShelf']
        assert routes['DealsFeed']==['MainFeed','SelectedShelf']
        assert sources[-1000000000201]==('FreshFeed',['SelectedShelf'])
        assert sources[201]==sources[-1000000000201]
        assert len(applied)==2 and len(calls)==2
        result=json.loads(snapshot.read_text())
        assert result['source_details']['FreshFeed']['title']=='Fresh Deals'
        assert result['main_sources']==['DealsFeed']
        assert result['live_sources'] and result['live_channels']['SelectedShelf']['state']=='active'
        await manager.sync_once(); assert len(calls)==2
        # Second channel reuses the existing subscription, not a second source identity.
        cfg.save_channels({'channels':[desired,channel('AnotherShelf',['FreshFeed'])]},path)
        await manager.sync_once()
        assert routes['FreshFeed']==['SelectedShelf','AnotherShelf'] and len(calls)==2
        # Failed staging must leave all existing source/target routes untouched.
        baseline=json.dumps(routes,sort_keys=True)
        existing=[desired,channel('AnotherShelf',['FreshFeed'])]
        for bad in ('JoinLater','LoopFeed','AliasFeed','NotAChannel','RaceFeed'):
            cfg.save_channels({'channels':existing+[channel('WaitingShelf',[bad])]},path)
            await manager.sync_once()
            assert json.dumps(routes,sort_keys=True)==baseline, bad
            assert bad not in [entry[0] for entry in sources.values()]
            assert bad in calls
        # Edits stay deferred: removing a selected source must not mutate a queued route.
        cfg.save_channels({'channels':[channel('SelectedShelf',['DealsFeed'])]},path)
        await manager.sync_once()
        assert json.dumps(routes,sort_keys=True)==baseline
        assert json.loads(snapshot.read_text())['live_channels']['SelectedShelf']['state']=='restart_required'
        cfg.save_channels({'channels':existing+[channel('FloodShelf',['FloodFeed']),channel('AfterFlood',['UnknownFeed'])]},path)
        await manager.sync_once()
        assert 'UnknownFeed' not in calls
        assert manager.access_backoff_until>0
        # No join, send, backfill or replay API is available to this manager.
    with tempfile.TemporaryDirectory() as td:
        path,snapshot=Path(td)/'channels.json',Path(td)/'routing_snapshot.json'
        routes={'OneFeed':['StartupShelf'],'TwoFeed':['StartupShelf'],'ThreeFeed':['StartupShelf']}
        saved=channel('StartupShelf',list(routes))
        cfg.save_channels({'channels':[saved]},path)
        calls=[]
        async def target(name): return Entity(id=900,broadcast=True,creator=True)
        async def source(name):
            calls.append(name)
            return Entity(id=1000+list(routes).index(name),broadcast=True,left=False)
        manager=LiveChannels(routes,set(),{}, {}, {}, [saved],set(), {'electronics'},set(), target,
                             path=path,snapshot_file=snapshot,resolve_source=source,builtin_sources=set())
        await manager.sync_once()
        assert len(calls)==2 and not manager.source_map
        assert json.loads(snapshot.read_text())['live_channels']['StartupShelf']['state']=='checking_sources'
        await manager.sync_once()
        assert len(calls)==3 and len(manager.source_map)==6
        assert json.loads(snapshot.read_text())['live_channels']['StartupShelf']['state']=='active'
    print('Source setup PASS: 100-source validation, canonical links, per-channel scope, no wildcard leakage, staged activation, membership failure, aliases/loops, FloodWait, races and immutable existing policies.')


async def api_checks():
    import os
    from unittest.mock import patch
    from aiohttp.test_utils import TestClient, TestServer
    from dashboard import app
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            'BOT_DB_PATH':str(Path(td)/'missing.sqlite3'), 'CHANNELS_JSON_PATH':str(Path(td)/'channels.json'),
            'DASHBOARD_TOKEN':'source-test', 'DASHBOARD_RESTART_CMD':''}):
        cfg.snapshot_path().write_text(json.dumps({'sources':['DealsFeed'], 'targets':[],
            'source_details':{'DealsFeed':{'title':'Known channel name','state':'ready'}}}))
        async with TestClient(TestServer(app.create_app())) as client:
            headers={'X-Dashboard-Token':'source-test'}
            response=await client.get('/api/channels',headers=headers)
            data=await response.json()
            assert len(data['source_catalog'])>=60 and data['source_limit']==100
            assert next(row for row in data['source_catalog'] if row['key']=='DealsFeed')['title']=='Known channel name'
            assert not data['activation']['new_sources_live']
            response=await client.post('/api/channels',headers=dict(headers,**{'If-Match':data['revision']}),
                json=channel('NewShelf',['https://t.me/dealsfeed','@DealsFeed','https://t.me/ExtraFeed','https://t.me/+JoinedHash']))
            assert response.status==200,await response.text()
            saved=(await response.json())['channel']
            assert saved['sources']==['DealsFeed','ExtraFeed','https://t.me/+JoinedHash']
            assert saved['categories']==['electronics']
            response=await client.get('/api/channels',headers=headers)
            assert 'ExtraFeed' in (await response.json())['sources']
            for sources in (['*','DealsFeed'],['https://t.me/DealsFeed/123'],[f'Feed{i:03}' for i in range(101)]):
                response=await client.post('/api/channels',headers=headers,json=channel('InvalidShelf',sources))
                assert response.status==400
            assert not (Path(td)/'missing.sqlite3').exists()  # dashboard did not start/import the bot
    print('Source API PASS: authenticated 60+ source catalog, known titles, canonical multi-select save, pending access and no silent truncation.')


if __name__=='__main__':
    normalization()
    asyncio.run(activation())
    asyncio.run(api_checks())

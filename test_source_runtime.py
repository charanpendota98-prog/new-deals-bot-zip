"""Actual bot source helpers, with fake Telegram requests and isolated state only."""
import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.TemporaryDirectory(prefix='source-runtime-')
os.environ.update(TELEGRAM_API_ID='1', TELEGRAM_API_HASH='test', EARNKARO_API_KEY='test',
                  BOT_DB_PATH=str(Path(_TMP.name)/'bot.sqlite3'), BOT_LOG_DIR=str(Path(_TMP.name)/'logs'),
                  BOT_MEDIA_DIR=str(Path(_TMP.name)/'media'), CHANNELS_JSON_PATH=str(Path(_TMP.name)/'channels.json'))
from bestgaa import main_bot_new as bot  # noqa: E402


class ChatInviteAlready(SimpleNamespace):
    pass


class Client:
    def __init__(self, joined=True, channel=True):
        self.joined=joined;self.requests=[]
        self.entity=SimpleNamespace(id=987654,broadcast=channel,left=False,title='Verified channel title')
    async def get_entity(self, name):
        self.requests.append(('entity',name))
        return self.entity
    async def __call__(self, request):
        self.requests.append(type(request).__name__)
        if isinstance(request, bot.CheckChatInviteRequest):
            return ChatInviteAlready(chat=self.entity) if self.joined else SimpleNamespace()
        assert isinstance(request, bot.GetParticipantRequest), 'Only access checks are allowed; never Join/Import/Send'
        if not self.joined:
            raise ValueError('not a participant')
        return SimpleNamespace(participant=SimpleNamespace())


async def run():
    bundled=json.loads((Path(__file__).parent/'bestgaa/source_catalog.json').read_text())
    assert {row['key'] for row in bundled['sources']}==set(bot.BUILTIN_SOURCE_NAMES)
    for source in ('BrandNewFeed','https://t.me/+AlreadyJoinedHash'):
        client=Client()
        assert (await bot.resolve_dashboard_source(client,source)).title=='Verified channel title'
        assert not any('Import' in str(request) or 'JoinChannel' in str(request) for request in client.requests)
        for invalid in (Client(joined=False),Client(channel=False)):
            try: await bot.resolve_dashboard_source(invalid,source)
            except ValueError: pass
            else: raise AssertionError('unjoined or non-channel source accepted')
    # The plain (non-dashboard) resolver is the one that used to JOIN: an invite
    # link in the source list made the bot call ImportChatInvite on every boot
    # ("JOIN failed https://t.me/+..." in the live log). Membership is now only
    # ever READ, and an unjoined invite yields None plus a reason for the panel.
    invite='https://t.me/+SomePrivateInvite'
    joined=Client()
    assert (await bot.resolve_entity(joined,invite)).title=='Verified channel title'
    assert 'CheckChatInviteRequest' in joined.requests
    assert not any('Import' in str(request) or 'JoinChannel' in str(request)
                   for request in joined.requests), 'resolve_entity must never join'
    outsider=Client(joined=False)
    assert await bot.resolve_entity(outsider,invite) is None
    assert not any('Import' in str(request) or 'JoinChannel' in str(request)
                   for request in outsider.requests), 'an unjoined invite must not be joined'
    bot.SOURCE_DETAILS.pop(invite,None)
    assert not await bot.register_source(outsider,{},invite,['SelectedShelf'],target_map={})
    assert bot.SOURCE_DETAILS[invite]['state']=='join_required'
    assert 'never joins automatically' in bot.SOURCE_DETAILS[invite]['detail']
    source='BrandNewFeed'
    bot.SOURCE_TO_TARGETS[source]=['SelectedShelf']
    bot.DASHBOARD_SCOPED_SOURCES.add(source)
    source_map={}
    client=Client()
    assert await bot.register_source(client,source_map,source,['SelectedShelf'],do_backfill=True,target_map={})
    # Client has no get_messages method: registration cannot fetch history.
    assert source in bot.SOURCE_ACTIVATED_AT
    assert bot.SOURCE_DETAILS[source]['title']=='Verified channel title'
    assert bot.scope_dashboard_targets(source,['LootZoneIndia11','SelectedShelf','PowerLoots1'])==['SelectedShelf']
    assert bot.scope_dashboard_targets('deals',['LootZoneIndia11','SelectedShelf'])==['LootZoneIndia11','SelectedShelf']
    cutoff=bot.SOURCE_ACTIVATED_AT[source]
    before=SimpleNamespace(date=datetime.fromtimestamp(cutoff-1,timezone.utc))
    after=SimpleNamespace(date=datetime.fromtimestamp(cutoff+1,timezone.utc))
    assert not bot.source_message_is_current(source,before)
    assert bot.source_message_is_current(source,after)
    assert not bot.source_message_is_current(source,SimpleNamespace())
    assert bot.source_message_is_current('deals',before)
    assert not await bot.register_source(client,{},source,['SelectedShelf'],target_map={'OtherTarget':client.entity})
    aliases={client.entity.id:('OtherKnownFeed',['MainFeed'])}
    assert not await bot.register_source(client,aliases,source,['SelectedShelf'],target_map={})
    assert aliases[client.entity.id][0]=='OtherKnownFeed'
    print('Runtime source checks PASS: catalog parity, already-joined access only (invite links are read, never joined), no join/history calls, unjoined sources explained, local routing cap, activation cutoffs, target-loop and alias rejection.')

if __name__=='__main__':
    try: asyncio.run(run())
    finally:
        bot.store.conn.close()
        _TMP.cleanup()

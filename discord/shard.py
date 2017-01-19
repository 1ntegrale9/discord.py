# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-2016 Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from .state import AutoShardedConnectionState
from .client import Client
from .gateway import *
from .errors import ConnectionClosed, ClientException
from . import compat
from .enums import Status

import asyncio
import logging
import websockets

log = logging.getLogger(__name__)

class Shard:
    def __init__(self, ws, client):
        self.ws = ws
        self._client = client
        self.loop = self._client.loop
        self._current = asyncio.Future(loop=self.loop)
        self._current.set_result(None) # we just need an already done future

    @property
    def id(self):
        return self.ws.shard_id

    @asyncio.coroutine
    def poll(self):
        try:
            yield from self.ws.poll_event()
        except (ReconnectWebSocket, ResumeWebSocket) as e:
            resume = type(e) is ResumeWebSocket
            log.info('Got ' + type(e).__name__)
            self.ws = yield from DiscordWebSocket.from_client(self._client, resume=resume,
                                                                            shard_id=self.id,
                                                                            session=self.ws.session_id,
                                                                            sequence=self.ws.sequence)
        except ConnectionClosed as e:
            yield from self._client.close()
            if e.code != 1000:
                raise

    def get_future(self):
        if self._current.done():
            self._current = compat.create_task(self.poll(), loop=self.loop)

        return self._current

class AutoShardedClient(Client):
    """A client similar to :class:`Client` except it handles the complications
    of sharding for the user into a more manageable and transparent single
    process bot.

    When using this client, you will be able to use it as-if it was a regular
    :class:`Client` with a single shard when implementation wise internally it
    is split up into multiple shards. This allows you to not have to deal with
    IPC or other complicated infrastructure.

    It is recommended to use this client only if you have surpassed at least
    1000 guilds.

    If no :attr:`shard_count` is provided, then the library will use the
    Bot Gateway endpoint call to figure out how many shards to use.

    If a ``shard_ids`` parameter is given, then those shard IDs will be used
    to launch the internal shards. Note that :attr:`shard_count` must be provided
    if this is used. By default, when omitted, the client will launch shards from
    0 to ``shard_count - 1``.

    Attributes
    ------------
    shard_ids: Optional[List[int]]
        An optional list of shard_ids to launch the shards with.
    """
    def __init__(self, *args, loop=None, **kwargs):
        kwargs.pop('shard_id', None)
        self.shard_ids = kwargs.pop('shard_ids', None)
        super().__init__(*args, loop=loop, **kwargs)

        if self.shard_ids is not None:
            if self.shard_count is None:
                raise ClientException('When passing manual shard_ids, you must provide a shard_count.')
            elif not isinstance(self.shard_ids, (list, tuple)):
                raise ClientException('shard_ids parameter must be a list or a tuple.')

        self.connection = AutoShardedConnectionState(dispatch=self.dispatch, chunker=self.request_offline_members,
                                                     syncer=self._syncer, http=self.http, loop=self.loop, **kwargs)

        # instead of a single websocket, we have multiple
        # the key is the shard_id
        self.shards = {}

        self._still_sharding = True

    @asyncio.coroutine
    def request_offline_members(self, guild, *, shard_id=None):
        """|coro|

        Requests previously offline members from the guild to be filled up
        into the :attr:`Guild.members` cache. This function is usually not
        called.

        When the client logs on and connects to the websocket, Discord does
        not provide the library with offline members if the number of members
        in the guild is larger than 250. You can check if a guild is large
        if :attr:`Guild.large` is ``True``.

        Parameters
        -----------
        guild: :class:`Guild` or list
            The guild to request offline members for. If this parameter is a
            list then it is interpreted as a list of guilds to request offline
            members for.
        """

        try:
            guild_id = guild.id
            shard_id = shard_id or guild.shard_id
        except AttributeError:
            guild_id = [s.id for s in guild]

        payload = {
            'op': 8,
            'd': {
                'guild_id': guild_id,
                'query': '',
                'limit': 0
            }
        }

        ws = self.shards[shard_id].ws
        yield from ws.send_as_json(payload)

    @asyncio.coroutine
    def pending_reads(self, shard):
        try:
            while self._still_sharding:
                yield from shard.poll()
        except asyncio.CancelledError:
            pass

    @asyncio.coroutine
    def launch_shard(self, gateway, shard_id):
        try:
            ws = yield from websockets.connect(gateway, loop=self.loop, klass=DiscordWebSocket)
        except Exception as e:
            log.info('Failed to connect for shard_id: %s. Retrying...' % shard_id)
            yield from asyncio.sleep(5.0, loop=self.loop)
            yield from self.launch_shard(gateway, shard_id)

        ws.token = self.http.token
        ws._connection = self.connection
        ws._dispatch = self.dispatch
        ws.gateway = gateway
        ws.shard_id = shard_id
        ws.shard_count = self.shard_count

        # OP HELLO
        yield from ws.poll_event()
        yield from ws.identify()
        log.info('Sent IDENTIFY payload to create the websocket for shard_id: %s' % shard_id)

        # keep reading the shard while others connect
        self.shards[shard_id] = ret = Shard(ws, self)
        compat.create_task(self.pending_reads(ret), loop=self.loop)
        yield from asyncio.sleep(5.0, loop=self.loop)

    @asyncio.coroutine
    def launch_shards(self):
        if self.shard_count is None:
            self.shard_count, gateway = yield from self.http.get_bot_gateway()
        else:
            gateway = yield from self.http.get_gateway()

        self.connection.shard_count = self.shard_count

        shard_ids = self.shard_ids if self.shard_ids else range(self.shard_count)

        for shard_id in shard_ids:
            yield from self.launch_shard(gateway, shard_id)

        self._still_sharding = False

    @asyncio.coroutine
    def connect(self):
        """|coro|

        Creates a websocket connection and lets the websocket listen
        to messages from discord.

        Raises
        -------
        GatewayNotFound
            If the gateway to connect to discord is not found. Usually if this
            is thrown then there is a discord API outage.
        ConnectionClosed
            The websocket connection has been terminated.
        """
        yield from self.launch_shards()

        while not self.is_closed:
            pollers = [shard.get_future() for shard in self.shards.values()]
            yield from asyncio.wait(pollers, loop=self.loop, return_when=asyncio.FIRST_COMPLETED)

    @asyncio.coroutine
    def close(self):
        """|coro|

        Closes the connection to discord.
        """
        if self.is_closed:
            return

        for shard in self.shards.values():
            yield from shard.ws.close()

        yield from self.http.close()
        self._closed.set()
        self._is_ready.clear()

    @asyncio.coroutine
    def change_presence(self, *, game=None, status=None, afk=False, shard_id=None):
        """|coro|

        Changes the client's presence.

        The game parameter is a Game object (not a string) that represents
        a game being played currently.

        Parameters
        ----------
        game: Optional[:class:`Game`]
            The game being played. None if no game is being played.
        status: Optional[:class:`Status`]
            Indicates what status to change to. If None, then
            :attr:`Status.online` is used.
        afk: bool
            Indicates if you are going AFK. This allows the discord
            client to know how to handle push notifications better
            for you in case you are actually idle and not lying.
        shard_id: Optional[int]
            The shard_id to change the presence to. If not specified
            or ``None``, then it will change the presence of every
            shard the bot can see.

        Raises
        ------
        InvalidArgument
            If the ``game`` parameter is not :class:`Game` or None.
        """

        if status is None:
            status = 'online'
            status_enum = Status.online
        elif status is Status.offline:
            status = 'invisible'
            status_enum = Status.offline
        else:
            status_enum = status
            status = str(status)

        if shard_id is None:
            for shard in self.shards.values():
                yield from shard.ws.change_presence(game=game, status=status, afk=afk)

            guilds = self.connection.guilds
        else:
            shard = self.shards[shard_id]
            yield from shard.ws.change_presence(game=game, status=status, afk=afk)
            guilds = [g for g in self.connection.guilds if g.shard_id == shard_id]

        for guild in guilds:
            me = guild.me
            if me is None:
                continue

            me.game = game
            me.status = status_enum
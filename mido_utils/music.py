from __future__ import annotations

import asyncio
import collections
import itertools
import random
import time
from typing import List

import discord
from async_timeout import timeout
from wavelink import InvalidIDProvided, Node, Player, Track, TrackPlaylist

from mido_utils.context import Context
from mido_utils.exceptions import *
from mido_utils.resources import Resources
from mido_utils.time_stuff import Time


class VoicePlayer(Player):
    def __init__(self, bot, guild_id: int, node: Node, **kwargs):
        super().__init__(bot, guild_id, node, **kwargs)

        self.wavelink = bot.wavelink
        self.song_queue: SongQueue = SongQueue()
        self.next = asyncio.Event()

        self.loop = False
        self.skip_votes = []

        self.last_song: Song = self.current

        self.task = self.bot.loop.create_task(self.player_loop(), name=f"Music Player of {guild_id}")

    @property
    def position(self):
        if not self.is_playing or not self.current or not self.last_update:
            return 0

        if self.paused:
            return min(self.last_position, self.current.duration)

        difference = (time.time() * 1000) - self.last_update
        position = self.last_position + difference

        return min(position, self.current.duration)

    @property
    def position_in_seconds(self) -> int:
        return int(self.position / 1000)

    @property
    def position_str(self):
        return Time.parse_seconds_to_str(self.position_in_seconds, short=True, sep=':')

    async def destroy(self) -> None:
        try:
            await super().destroy()
        except (InvalidIDProvided, KeyError):
            pass
        finally:
            self.task.cancel()

    async def add_songs(self, ctx: Context, *songs: Song):
        for i, song in enumerate(songs):
            if len(self.song_queue) > 500 and i == 0:
                raise OnCooldownError("You can't add more than 500 songs.")

            if isinstance(song, Track):
                song = Song.convert(song, ctx)

            await self.song_queue.put(song)

    async def _get_tracks_from_query(self, ctx, query: str) -> List[Song]:
        original_query = query
        if not query.startswith('http'):  # if not a link
            query = f'ytsearch:{query}'

        song = None
        attempt = 0
        while attempt < 5:
            song = await self.wavelink.get_tracks(query=query, retry_on_failure=True)
            if song:
                break
            attempt += 1

        if not song:
            raise NotFoundError(f"Couldn't find anything that matches the query:\n"
                                f"`{original_query}`.")

        if isinstance(song, TrackPlaylist):
            songs = song.tracks
        else:
            songs = [song[0]]

        return list(map(lambda x: Song.convert(x, ctx), songs))

    async def parse_query_and_add_songs(self, ctx: Context, query: str, spotify):
        if query.startswith('https://open.spotify.com/'):  # spotify link
            songs_to_add = [x for x in await spotify.get_songs(ctx, query)]
        else:
            songs_to_add = await self._get_tracks_from_query(ctx, query)

        await self.add_songs(ctx, *songs_to_add)

        return songs_to_add

    async def skip(self):
        await self.stop()

    async def parse_and_get_the_next_song(self) -> Song:
        song = await self.song_queue.get()

        if not isinstance(song, Song):
            song: Song = (await self._get_tracks_from_query(song.ctx, song.search_query))[0]
        return song

    async def player_loop(self):
        while True:
            self.next.clear()
            self.skip_votes.clear()

            if self.loop is False:
                try:
                    async with timeout(180):
                        try:
                            self.current = await self.parse_and_get_the_next_song()
                        except NotFoundError as e:  # might be annoying
                            await self.bot.get_cog('ErrorHandling').on_error(e)
                            continue
                        else:
                            self.last_song = self.current
                except asyncio.TimeoutError:
                    return await self.destroy()
            else:
                self.current = self.last_song

            await self.play(self.current)

            if not self.loop:
                try:
                    await self.current.send_np_embed()
                except (discord.Forbidden, discord.NotFound):
                    pass

            await self.next.wait()

    def get_current(self):
        return self.current or self.last_song


class BaseSong:
    def __init__(self, ctx, title: str, duration_in_ms: int, url: str):
        self.ctx = ctx

        self.title: str = title
        self.url: str = url
        self.duration: int = duration_in_ms

    @property
    def duration_in_seconds(self) -> int:
        return int(self.duration / 1000)

    @property
    def duration_str(self) -> str:
        return Time.parse_seconds_to_str(self.duration_in_seconds, short=True, sep=':')

    @property
    def requester(self) -> discord.Member:
        return self.ctx.author

    @property
    def text_channel(self) -> discord.TextChannel:
        return self.ctx.channel

    @property
    def search_query(self) -> str:
        return self.title + ' Audio'

    @classmethod
    def convert_from_spotify_track(cls, ctx, track: dict):
        title = ", ".join(artist['name'] for artist in track['artists'])
        title += f" - {track['name']}"

        url = track["external_urls"]["spotify"]
        duration = track["duration_ms"]

        return cls(ctx, title, duration, url)


class Song(Track, BaseSong):
    def __init__(self,
                 _id: str,
                 info: dict,
                 query: str,
                 ctx: Context):
        super().__init__(_id, info, query)

        self.ctx: Context = ctx
        self.player: VoicePlayer = ctx.voice_player

    @property
    def url(self) -> str:
        return self.uri

    @classmethod
    def convert(cls,
                track: Track,
                ctx: Context):
        """Converts a native wavelink track object to a local Song object."""
        return cls(track.id, track.info, track.query, ctx)

    async def send_np_embed(self):
        e = self.create_np_embed()
        await self.text_channel.send(embed=e, delete_after=self.duration_in_seconds)

    def create_np_embed(self):
        e = discord.Embed(
            title=self.title,
            color=0x15a34a)

        e.set_author(
            icon_url=Resources.images.now_playing,
            name="Now Playing",
            url=self.url)

        e.add_field(name='Duration', value=f"{self.player.position_str}/{self.duration_str}")
        e.add_field(name='Requester', value=self.requester.mention)
        e.add_field(name='Uploader', value=self.author)

        # if self.source.upload_date:
        #     e.add_field(name="Upload Date", value=self.source.upload_date)
        #
        # e.add_field(name="View Count", value='{:,}'.format(self.source.views))
        #
        # if self.source.likes and self.source.dislikes:
        #     likes = self.source.likes
        #     dislikes = self.source.dislikes
        #     e.add_field(name="Like/Dislike Count",
        #                 value="{:,}/{:,}\n(**{:.2f}%**)".format(likes, dislikes, (likes * 100 / (likes + dislikes))))

        e.set_footer(text=f"Volume: {self.player.volume}%",
                     icon_url=Resources.images.volume)

        if self.thumb:
            e.set_thumbnail(url=self.thumb)

        return e


class SongQueue(asyncio.Queue):
    def _init(self, maxsize):
        self._queue = collections.deque()

    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        # noinspection PyTypeChecker
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]

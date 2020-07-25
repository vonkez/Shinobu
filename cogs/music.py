import asyncio
import audioop
import collections
import functools
import itertools
import math
import random

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands

from db.models import GuildDB, MidoTime
from main import MidoBot
from services import context, menu_stuff
from services.apis import SomeRandomAPI, SpotifyAPI
from services.base_embed import BaseEmbed
from services.exceptions import MusicError, NotFoundError


class YTDLSource(discord.PCMVolumeTransformer):
    # youtube_dl.utils.std_headers['User-Agent'] = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/51.0.2704.103 Safari/537.36'
    youtube_dl.utils.std_headers[
        'User-Agent'] = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'

    YTDL_OPTIONS = {
        'format'            : 'bestaudio/best',
        'extractaudio'      : True,
        'audioformat'       : 'mp3',
        # 'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames' : True,
        # 'playlistend': 100,
        'nocheckcertificate': True,
        'ignoreerrors'      : True,
        'logtostderr'       : False,
        'quiet'             : True,
        'no_warnings'       : True,
        'default_search'    : 'auto',
        'source_address'    : '0.0.0.0',
        'cachedir'          : False,
        # 'cookiefile': 'other/cookies.txt',
        'verbose'           : True
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options'       : '-vn',
    }

    BLACKLISTED_TITLES = [
        '[Deleted video]',
        '[Private video]'
    ]

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: context.MidoContext, source: discord.FFmpegPCMAudio, *, data: dict, volume: int = 10):
        super().__init__(source, volume / 100)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data
        self.ctx = ctx

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')

        self.upload_date = data.get('upload_date')
        if self.upload_date:
            self.upload_date = self.upload_date[6:8] + '.' + self.upload_date[4:6] + '.' + self.upload_date[0:4]

        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = MidoTime.parse_seconds_to_str(int(data.get('duration')), short=True, sep=':')
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')
        self._played_duration = 0

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls,
                            ctx: context.MidoContext,
                            search: str,
                            process: bool = True,
                            loop: asyncio.BaseEventLoop = asyncio.get_event_loop()):
        if search in cls.BLACKLISTED_TITLES:
            return None

        try:
            partial = functools.partial(cls.ytdl.extract_info, url=search, download=False, process=process)
            processed_info = await loop.run_in_executor(None, partial)
            if not processed_info:
                raise youtube_dl.DownloadError('No processed info.')
        except youtube_dl.DownloadError:
            return None

        # if we have a list of entries (most likely a playlist or a search)
        if 'entries' in processed_info:
            if process is True:
                return [cls(ctx, discord.FFmpegPCMAudio(song['url'], **cls.FFMPEG_OPTIONS), data=song)
                        for song in processed_info['entries']]
            else:
                return [song['title'] for song in processed_info['entries']]

        # if a song link is provided
        else:
            if process is True:
                return [
                    cls(ctx, discord.FFmpegPCMAudio(processed_info['url'], **cls.FFMPEG_OPTIONS), data=processed_info)]
            else:
                return [processed_info['webpage_url']]

    @property
    def played_duration(self) -> str:
        return MidoTime.parse_seconds_to_str(int(self._played_duration), short=True, sep=':')

    def read(self):
        self._played_duration += 0.02
        ret = self.original.read()
        return audioop.mul(ret, 2, min(self._volume, 2.0))


class Song:
    __slots__ = ('ctx', 'source', 'requester')

    def __init__(self, source: YTDLSource):
        self.ctx = source.ctx
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        e = discord.Embed(
            title=self.source.title,
            color=0x15a34a)

        e.set_author(
            icon_url="https://cdn.discordapp.com/attachments/244405453948321792/707797956295655434/PngItem_2087614.png",
            name="Now Playing",
            url=self.source.url)

        e.add_field(name='Duration', value=f"{self.source.played_duration}/{self.source.duration}")
        e.add_field(name='Requester', value=self.requester.mention)
        e.add_field(name='Uploader', value=f'[{self.source.uploader}]({self.source.uploader_url})')

        if self.source.upload_date:
            e.add_field(name="Upload Date", value=self.source.upload_date)

        e.add_field(name="View Count", value='{:,}'.format(self.source.views))

        if self.source.likes and self.source.dislikes:
            likes = self.source.likes
            dislikes = self.source.dislikes
            e.add_field(name="Like/Dislike Count",
                        value="{:,}/{:,}\n(**{:.2f}%**)".format(likes, dislikes, (likes * 100 / (likes + dislikes))))

        e.set_footer(text=f"Volume: {int(self.source.volume * 100)}%",
                     icon_url="https://i.imgur.com/T0532pn.png")

        if self.source.thumbnail:
            e.set_thumbnail(url=self.source.thumbnail)

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
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: MidoBot):
        self.bot = bot

        self.current = None
        # noinspection PyTypeChecker
        self.voice: discord.VoiceClient = None
        self.exists = True
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 10
        self.skip_votes = []

        self.played_duration = 0
        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, new_vol: int):
        self._volume = new_vol
        if self.current:
            self.current.source.volume = new_vol / 100

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                try:
                    async with timeout(180):
                        self.current: Song = await self.songs.get()
                except asyncio.TimeoutError:
                    return self.bot.loop.create_task(self.stop())

            self.current.source.volume = self._volume / 100
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise Exception(str(error))
        self.skip_votes.clear()
        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect(force=True)
            self.voice = None
            self.exists = False


class Music(commands.Cog):
    def __init__(self, bot: MidoBot):
        self.bot = bot

        self.forcekip_by_default = True
        self.voice_states = {}

        self.sri_api = SomeRandomAPI(self.bot.http_session)
        self.spotify_api = SpotifyAPI(self.bot.http_session, self.bot.config['spotify_credentials'])

    def get_voice_state(self, guild: GuildDB) -> VoiceState:
        state = self.voice_states.get(guild.id)

        if not state or not state.exists:
            state = VoiceState(self.bot)
            state.volume = guild.volume
            self.voice_states[guild.id] = state

        return state

    async def cog_check(self, ctx: context.MidoContext):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')

        return True

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    async def cog_before_invoke(self, ctx: context.MidoContext):
        ctx.voice_state = self.get_voice_state(ctx.guild_db)

    @commands.command(name='connect')
    async def _join(self, ctx: context.MidoContext):
        """Make me connect to your voice channel."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send_error('You are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                return await ctx.send_error('Bot is already in a voice channel.')

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            return await ctx.voice_state.voice.move_to(destination)

        if not destination.permissions_for(ctx.guild.me).is_superset(discord.Permissions(1049600)):
            raise MusicError("I do not have permission to connect to that voice channel!")

        try:
            ctx.voice_state.voice = await destination.connect()
        except asyncio.TimeoutError:
            raise MusicError("I could not connect to the voice channel. Please try again later.")
        except discord.ClientException as e:
            raise MusicError(str(e))
        else:
            await ctx.message.add_reaction('👍')

    @commands.command(name='disconnect', aliases=['destroy', 'd'])
    async def _leave(self, ctx: context.MidoContext):
        """Make me disconnect from your voice channel."""
        if ctx.voice_client:
            await ctx.voice_client.disconnect(force=True)

            await ctx.voice_state.stop()
            del self.voice_states[ctx.guild.id]

            await ctx.send_success("I've successfully left the voice channel.")

        else:
            return await ctx.send_error("I'm not currently not in a voice channel! (or am I 🤔)")

    @commands.command(name='volume', aliases=['vol', 'v'])
    async def _volume(self, ctx: context.MidoContext, volume: int = None):
        """Change or see the volume."""
        if not ctx.voice_state.is_playing:
            return await ctx.send_error('Nothing is being played at the moment.')

        if volume is None:
            return await ctx.send_success(f'Current volume: **{ctx.voice_state.volume}**%')

        elif volume == 0:
            return await ctx.send_error(f"Just do `{ctx.prefix}pause` rather than setting volume to 0.")

        elif volume < 0 or volume > 100:
            return await ctx.send_error('The volume must be **between 0 and 100!**')

        ctx.voice_state.volume = volume
        await ctx.guild_db.change_volume(volume)

        await ctx.send_success(f'Volume is set to **{volume}**%')

    @commands.command(name='now', aliases=['current', 'playing', 'nowplaying', 'np'])
    async def _now(self, ctx: context.MidoContext):
        """See what's currently playing."""
        if ctx.voice_state.current:
            await ctx.send(embed=ctx.voice_state.current.create_embed())
        else:
            await ctx.send_error("I'm not currently playing any music!")

    @commands.command(name='pause', aliases=['p'])
    async def _pause(self, ctx: context.MidoContext):
        """Pause the song."""
        if ctx.voice_state.voice.is_paused():
            await ctx.send_error(f"It's already paused! Use `{ctx.prefix}resume` to resume.")

        elif ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.send_success("⏯ Paused.")

        else:
            await ctx.send_error("I'm not currently playing any music!")

    @commands.command(name='resume')
    async def _resume(self, ctx: context.MidoContext):
        if not ctx.voice_state.voice.is_paused():
            await ctx.send_error(f"It's not paused! Use `{ctx.prefix}pause` to pause.")

        elif ctx.voice_state.is_playing:
            ctx.voice_state.voice.resume()
            await ctx.send_success('⏯ Resumed.')

        else:
            await ctx.send_error("I'm not currently playing any music!")

    @commands.command(name='stop')
    async def _stop(self, ctx: context.MidoContext):
        """Stop playing and clear the queue."""

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            ctx.voice_state.songs.clear()
            await ctx.send_success('⏹ Stopped.')
        else:
            await ctx.send_error("I'm not currently playing any music!")

    @commands.command(name='skip', aliases=['next'])
    async def _skip(self, ctx: context.MidoContext):
        """Skip the currently playing song."""
        if not ctx.voice_state.is_playing:
            return await ctx.send_error('Not playing any music right now...')

        voter = ctx.message.author
        vc = ctx.voice_state.voice.channel
        if ctx.author not in vc.members:
            return await ctx.send_error("You are not in the voice channel!")

        people_in_vc = len(vc.members) - 1
        if people_in_vc <= 2:
            required_votes = people_in_vc
        else:
            required_votes = math.floor(people_in_vc * 0.8)

        if (voter == ctx.voice_state.current.requester  # if its the requester
                or len(ctx.voice_state.skip_votes) >= required_votes  # if it reached the required vote amount
                or self.forcekip_by_default):  # if forceskip is enabled
            ctx.voice_state.skip()
            await ctx.send_success('⏭ Skipped.')

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.append(voter.id)

            total_votes = len(ctx.voice_state.skip_votes)
            if total_votes >= required_votes:
                ctx.voice_state.skip()
                await ctx.send_success('⏭ Skipped.')

            else:
                base_string = f'Skip vote added, currently at **{total_votes}/{required_votes}**'
                if ctx.author.guild_permissions.manage_guild is True:
                    base_string += f'\n\n**You can force this action by typing `{ctx.prefix}forceskip`**'

                return await ctx.send_success(base_string)

        else:
            await ctx.send_error('You have already voted to skip this song.')

    @commands.command(name='forceskip', aliases=['fskip'])
    @commands.has_permissions(manage_guild=True)
    async def _force_skip(self, ctx: context.MidoContext):
        """Skip the currently playing song without requiring votes if enabled.

        You need the **Manage Server** permission to use this command."""
        if not ctx.voice_state.is_playing:
            return await ctx.send_error('Not playing any music right now...')

        ctx.voice_state.skip()
        await ctx.send_success('⏭ Skipped.')

    @commands.command(name='queue', aliases=['q'])
    async def _queue(self, ctx: context.MidoContext):
        """See the current song queue."""
        if len(ctx.voice_state.songs) == 0 and not ctx.voice_state.current:
            return await ctx.send_error(f'The queue is empty. Try queueing songs with `{ctx.prefix}play song_name`')

        blocks = []
        current = ctx.voice_state.current
        queue_duration = current.source.data.get('duration')

        # currently playing
        blocks.append(f"🎶 **[{current.source.title}]({current.source.url})**\n"
                      f"`{current.source.duration} | "
                      f"{current.requester}`\n")

        for i, song in enumerate(ctx.voice_state.songs, 1):
            blocks.append(f"**{i}**. **[{song.source.title}]({song.source.url})**\n"
                          f"`{song.source.duration} | "
                          f"{song.requester}`")
            queue_duration += song.source.data.get('duration')

        embed = (discord.Embed(color=self.bot.main_color)
                 .set_author(icon_url=ctx.guild.icon_url, name=f"{ctx.guild.name} Music Queue - ")
                 .set_footer(text=f"{int(current.source.volume * 100)}% | "
                                  f"{len(ctx.voice_state.songs) + 1} Songs | "
                                  f"{MidoTime.parse_seconds_to_str(queue_duration, short=True, sep=':')} in Total",
                             icon_url="https://i.imgur.com/T0532pn.png")
                 )
        await menu_stuff.paginate(self.bot, ctx, embed, blocks, item_per_page=5, add_page_info_to='author')

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: context.MidoContext):
        """Shuffle the song queue."""
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send_error('The queue is empty.')

        ctx.voice_state.songs.shuffle()
        await ctx.send_success('Successfully shuffled the song queue.')

    @commands.command(name='remove')
    async def _remove(self, ctx: context.MidoContext, index: int):
        """Remove a song from the song queue."""
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send_error('The queue is empty.')

        if not 0 < index <= len(ctx.voice_state.songs):
            return await ctx.send_error("Please specify a proper index!")

        if ctx.author.id != ctx.voice_state.songs[index - 1].requester.id:
            return await ctx.send_error("You are not the requester of this song!")

        ctx.voice_state.songs.remove(index - 1)
        await ctx.send_success('✅ Removed the song.')

    # This command has been disabled due to issues its causing.
    # @commands.command(name='loop')
    # async def _loop(self, ctx: context.Context):
    #     if not ctx.voice_state.is_playing:
    #         return await ctx.send('Nothing being played at the moment.')
    #
    #     # Inverse boolean value to loop and unloop.
    #     ctx.voice_state.loop = not ctx.voice_state.loop
    #     await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: context.MidoContext, *, search: str):
        """Queue a song to play!"""
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send_error('You are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                return await ctx.send_error('Bot is already in a voice channel.')

        if not ctx.voice_state.voice:
            self.bot.loop.create_task(ctx.invoke(self._join))

        msg_task = self.bot.loop.create_task(ctx.send_success("Processing..."))

        # checks
        async with ctx.typing():
            # get song names
            if search.startswith('https://open.spotify.com/'):
                search: list = await self.spotify_api.get_song_names(search)
            else:
                search: list = await YTDLSource.create_source(ctx, search, process=False, loop=self.bot.loop)

            songs = []
            for query in search:
                source = await YTDLSource.create_source(ctx, query, process=True, loop=self.bot.loop)
                if source:
                    s_obj = Song(source[0])
                    songs.append(s_obj)
                    await ctx.voice_state.songs.put(s_obj)

            msg = msg_task.result()
            # if its a playlist
            if len(songs) > 1:
                await ctx.edit_custom(msg, f'**{len(songs)}** songs have been successfully added to the queue!\n\n'
                                           f'You can type `{ctx.prefix}queue` to see it.')
            # single query
            elif len(songs) == 1:
                await ctx.edit_custom(msg, f'**{songs[0].source.title}** has been successfully added to the queue.\n\n'
                                           f'You can type `{ctx.prefix}queue` to see it.')
            else:
                await ctx.edit_custom(msg, f"Couldn't find anything that matches `{search[0]}`.")

    @commands.command()
    async def lyrics(self, ctx: context.MidoContext, *, song_name: str = None):
        """See the lyrics of the current song or a specific song."""
        if not song_name and not ctx.voice_state.current:
            return await ctx.send_error("You need to play a song then use this command or specify a song name!")
        elif not song_name:
            song_name = ctx.voice_state.current.source.title

        try:
            song_title, lyrics_pages, thumbnail = await self.sri_api.get_lyrics(song_name)
        except NotFoundError:
            return await ctx.send_error(f"I couldn't find the lyrics of **{song_name}**.\n"
                                        f"Try writing the title in a simpler form.")

        e = BaseEmbed(bot=self.bot, title=song_title, default_footer=True)
        e.set_thumbnail(url=thumbnail)

        await menu_stuff.paginate(bot=self.bot,
                                  ctx=ctx,
                                  embed=e,
                                  item_per_page=1,
                                  blocks=lyrics_pages,
                                  extra_sep='\n')


def setup(bot):
    bot.add_cog(Music(bot))

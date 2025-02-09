"""Namegame, where you try to remember a team number starting with the last number of the previous played team"""
import asyncio
import gzip
import pickle
import traceback
import typing
from collections import OrderedDict
from functools import wraps

import aiotba
import discord
from discord.ext.commands import has_permissions
from fuzzywuzzy import fuzz

from dozer.bot import dozer_logger
from ._utils import *
from ..asyncdb.orm import orm
from ..asyncdb import psqlt

SUPPORTED_MODES = ["frc", "ftc"]


def keep_alive(func):
    """Keeps the wrapped async function alive; functions must have self and ctx as args"""
    @wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        """Wraps namegame"""
        while True:
            try:
                return await func(self, ctx, *args, **kwargs)
            except Exception as e:
                # CancelledErrors are normal part of operation, ignore them
                if isinstance(e, asyncio.CancelledError):
                    return
                # panic to the console, and to chat
                dozer_logger.error(traceback.format_exc())
                await ctx.send(f"```Error in game loop:\n{e.__class__.__name__}: {e}```")

    return wrapper


def game_is_running(func):
    """Check if there's an active game in a channel"""
    @wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        """Wraps the checker"""
        if ctx.channel.id not in self.games:
            await ctx.send(f"There's not a game going on! Start one with `{ctx.prefix}ng startround`")
            return

        return await func(self, ctx, *args, **kwargs)

    return wrapper


class NameGameSession():
    """NameGame session object"""
    def __init__(self, mode):
        self.running = True
        self.pings_enabled = False
        self.players = OrderedDict()
        self.removed_players = []
        self.picked = []
        self.mode = mode
        self.time = 60
        self.vote_time = -1
        self.number = 0
        self.current_player = None
        self.last_name = ""
        self.last_team = 0
        self.state_lock = None
        self.turn_msg = None
        self.turn_embed = None
        self.turn_task = None
        self.turn_count = 0

        self.pass_tally = 0
        self.fail_tally = 0

        self.vote_correct = False
        self.vote_player = None
        self.vote_msg = None
        self.vote_embed = None
        self.vote_task = None

    def create_embed(self, title="", description="", color=discord.Color.blurple(), extra_fields=None, start=False):
        """Creates an embed."""
        extra_fields = extra_fields or []
        v = "Starting " if start else "Current "
        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="Players", value=", ".join([p.display_name for p in self.players.keys()]) or "n/a")
        embed.add_field(name=v + "Player", value=self.current_player)
        embed.add_field(name=v + "Number", value=self.number or "Wildcard")
        embed.add_field(name="Time Left", value=str(self.time))

        for name, value in extra_fields:
            embed.add_field(name=name, value=value)
        return embed

    async def check_name(self, ctx, team, name):
        """Checks the name of the team"""
        tba_parser = ctx.cog.tba_parser

        actual_name = ""

        if self.mode == "frc":
            # check for existence
            try:
                team_data = await tba_parser.team(team)
            except aiotba.http.AioTBAError:
                # oopsy whoopsy guess it doesn't exist
                return -1
            actual_name = team_data.nickname
        elif self.mode == "ftc":
            team_data = await ctx.bot.cogs["TOA"].get_teamdata(team)
            if not team_data:  # doesn't exist
                return -1
            actual_name = team_data['seasons'][0]['name']

        self.last_name = actual_name
        self.last_team = team
        return fuzz.ratio(actual_name.lower(), name.lower())

    def next_turn(self):
        """Processes the next turn transition"""
        self.turn_count += 1
        self.pass_tally = 0
        self.fail_tally = 0
        self.time = 60

        players = list(self.players.keys())
        # set the current player to the next handle in the list

        self.current_player = players[(players.index(self.current_player) + 1) % len(players)]

    # self._idx = (self._idx + 1) % len(self.players)

    def strike(self, player):
        """Gives players strikes"""
        self.players[player] += 1
        if self.players[player] >= 3 or len(self.players) == 1:
            self.removed_players.append(player)
            self.players.pop(player)
            return True
        return False

    def check_win(self):
        """Checks if someone won the game"""
        return len(self.players) == 1 and self.turn_count > 6

    def get_picked(self):
        """Gets the picked teams"""
        return ", ".join(map(str, sorted(self.picked))) or "No Picked Teams"


class NameGame(Cog):
    """Namegame commands"""
    def __init__(self, bot):
        super().__init__(bot)
        self.games = {}

        tba_config = bot.config['tba']
        self.tba_parser = aiotba.TBASession(tba_config['key'], self.bot.http_session)
        # tbapi.TBAParser(tba_config['key'], cache=False)

    @group(invoke_without_command=True, case_insensitive=True)
    async def ng(self, ctx):
        """Show info about and participate in a robotics team namegame.
        Run the help command on each of the subcommands for more detailed help.
        List of subcommands:
            ng info
            ng startround
            ng addplayer
            ng pick
            ng drop
            ng skip
            ng gameinfo
        """
        # await self.info.callback(self, ctx)
        await ctx.send(f"Please pick a subcommand: `{ctx.prefix}ng {{info|startround|addplayer|pick|drop|skip|gameinfo}}`")

    ng.example_usage = """
    `{prefix}ng` - show a description on how the robotics team namegame works. 
    """

    @ng.command()
    @bot_has_permissions(embed_links=True)
    async def info(self, ctx):
        """Show a description of the robotics team name game and how to play."""
        game_embed = discord.Embed(color=discord.Color.magenta())
        game_embed.title = "How to play"
        game_embed.description = "This is a very simple little game where players will name a team number and name that " \
                                 "starts with the last digit of the last named team. Some more specific rules are below:"
        game_embed.add_field(name="No Double Picking", value="Only pick teams once.")
        game_embed.add_field(name="Three Strikes, You're Out!",
                             value="You are only allowed three strikes, which are given by picking out of turn, "
                                   "getting the team name wrong, picking a non existant team, being voted that your "
                                   "pick is incorrect, not picking in time, or picking a already picked team.")
        game_embed.add_field(name="No Cheatsy Doodles",
                             value="No looking up teams on TBA, TOA, VexDB, or other methods, that's just unfair.")
        game_embed.add_field(name="Times up!",
                             value="You have 60 seconds to make a pick, or you get skipped and get a strike.")
        game_embed.add_field(name="Shaking Things Up",
                             value="Any team number that ends in a 0 mean that the next player has a wildcard, "
                                   "and can pick any legal team.")
        game_embed.add_field(name="Pesky Commands", value=(f"To start a game, type `{ctx.prefix}ng startround` and "
                                                           f"mention the players you want to play with. "
                                                           f"You can add people with `{ctx.prefix}ng addplayer <user_pings>`. "
                                                           f"When it's your turn, type `{ctx.prefix}ng pick <team> "
                                                           f"<teamname>` to execute your pick. "
                                                           f"If you need to skip, typing `{ctx.prefix}ng skip` gives you"
                                                           f" a strike and skips your turn."
                                                           f"You can always do `{ctx.prefix}ng gameinfo` to get the "
                                                           f"current game status. "
                                                           f"If you ever need to quit, running `{ctx.prefix}ng drop` "
                                                           f"removes you from the game. "
                                                           f"For more detailed command help, run `{ctx.prefix}help ng.`"))
        game_embed.add_field(name="Different Game Modes",
                             value=f"You can play the name game with FTC teams too! To start a game playing with "
                                   f"FTC teams, run `{ctx.prefix}ng startround ftc`")
        await ctx.send(embed=game_embed)

    info.example_usage = """
    `{prefix}ng help` - show a description on how the robotics team namegame works
    """

    @ng.group(invoke_without_command=True)
    async def config(self, ctx):
        """Configuration for namegame"""
        await ctx.send(f"""`{ctx.prefix}ng config` reference:
                `{ctx.prefix}ng config defaultmode [mode]` - set tbe default game mode used when startround is used with no arguments
                `{ctx.prefix}ng config setchannel [channel_mention]` - set the channel that games are allowed to be run in
                `{ctx.prefix}ng config clearsetchannel` - clear the set channel for games""")

    @config.command()
    @has_permissions(manage_guild=True)
    async def defaultmode(self, ctx, mode: str = None):
        """Configuration of the default game mode (FRC, FTC, etc.)"""
        config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
        if mode is None:
            mode = SUPPORTED_MODES[0] if config is None else config.mode
            await ctx.send(f"The current default game mode for this server is `{mode}`")
        else:
            if mode not in SUPPORTED_MODES:
                await ctx.send(
                    f"Game mode `{mode}` not supported! Please pick a mode that is one of: `{', '.join(SUPPORTED_MODES)}`")
                return
            if config is None:
                config = NameGameConfig(guild_id=ctx.guild.id, channel_id=None, mode=mode, pings_enabled=False)
                await config.insert()
                #session.add(config)
            else:
                config.mode = mode
                await config.update()
                await ctx.send(f"Default game mode updated to `{mode}`")

    @config.command()
    @has_permissions(manage_guild=True)
    async def setchannel(self, ctx, channel: discord.TextChannel = None):
        """Sets the namegame channel"""
        config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
        if channel is None:
            if config is None or config.channel_id is None:
                await ctx.send(
                    f"There is no currently set namegame channel.\nTo set a channel, run `{ctx.prefix}ng config "
                    f"setchannel [channel_mention]`")
            else:
                await ctx.send(
                    f"The currently set namegame channel is {ctx.guild.get_channel(config.channel_id).mention}.\n"
                    f"To clear this, run `{ctx.prefix}ng config clearsetchannel`")
        else:
            if config is None:
                config = NameGameConfig(guild_id=ctx.guild.id, channel_id=channel.id, mode=SUPPORTED_MODES[0],
                                        pings_enabled=False)
                await config.insert()
            else:
                config.channel_id = channel.id
                await config.update()
            await ctx.send(f"Namegame channel set to {channel.mention}!")

    @config.command()
    @has_permissions(manage_guild=True)
    async def clearsetchannel(self, ctx):
        """Clears the set namegame channel"""
        config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
        if config is not None:
            config.channel_id = None
            await config.update()
        await ctx.send("Namegame channel cleared!")

    @config.command()
    @has_permissions(manage_guild=True)
    async def setpings(self, ctx, enabled: bool):
        """Sets whether or not pings are enabled"""
        config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
        if config is None:
            config = NameGameConfig(guild_id=ctx.guild.id, channel_id=None, mode=SUPPORTED_MODES[0],
                                    pings_enabled=int(enabled))
            await config.insert()
        else:
            config.pings_enabled = int(enabled)
            await config.update()
        await ctx.send(f"Pings enabled set to `{enabled}`!")

    @config.command()
    @has_permissions(manage_guild=True)
    async def leaderboardedit(self, ctx, mode: str, user: typing.Union[discord.User, int], wins: int):
        """Edits the leaderboard"""

        if isinstance(user, int):
            uid = user
        else:
            uid = user.id
        if mode not in SUPPORTED_MODES:
            await ctx.send(
                f"Game mode `{mode}` not supported! Please pick a mode that is one of: `{', '.join(SUPPORTED_MODES)}`")
            return

        record = await NameGameLeaderboard.select_one(game_mode=mode, user_id=uid, guild_id=ctx.guild.id)
        if record is None:
            await ctx.send("User not on leaderboard!")
            return
        record.wins = wins
        await record.update()
        if isinstance(user, int):
            await ctx.send(f"<@!{user}>'s wins now set to : **{wins}**")
        else:
            await ctx.send(f"{user.display_name}'s wins now set to: **{wins}**")

    @config.command()
    @has_permissions(manage_guild=True)
    async def leaderboardclear(self, ctx, mode: str):
        """Clears the leaderboard"""
        if mode not in SUPPORTED_MODES:
            await ctx.send(
                f"Game mode `{mode}` not supported! Please pick a mode that is one of: `{', '.join(SUPPORTED_MODES)}`")
            return
        await NameGameLeaderboard.delete_all(game_mode=mode, guild_id=ctx.guild.id)
        await ctx.send(f"Cleared leaderboard for mode {mode}")

    # TODO: configurable time limits, ping on event, etc
    # MORE TODO:
    """
    add pings
    i hate bots
    figure out these stupid turn issues
    
    thanks past self i have no idea what this means
    """

    @ng.command()
    @game_is_running
    async def unheck(self, ctx):
        """
        Emergency removal of a haywire session.
        """
        game = self.games[ctx.channel.id]
        game.running = False
        try:
            game.vote_task.cancel()
        except Exception:
            pass
        try:
            game.turn_task.cancel()
        except Exception:
            pass

        self.games.pop(game)

    @ng.command()
    async def modes(self, ctx):
        """Returns a list of supported modes"""
        await ctx.send(f"Supported game modes: `{', '.join(SUPPORTED_MODES)}`")

    @ng.command()
    async def startround(self, ctx, mode: str = None):
        """
        Starts a namegame session.
        One can select the robotics program by specifying one of "FRC" or "FTC".
        """
        if mode is None or mode.lower() not in SUPPORTED_MODES:
            config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
            mode = SUPPORTED_MODES[0] if config is None else config.mode
            await ctx.send(
                f"Unspecified or invalid game mode,  assuming game mode `{mode}`. For a full list of game modes, run "
                f"`{ctx.prefix}ng modes`")

        config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
        if config is not None and config.channel_id is not None and config.channel_id != ctx.channel.id:
            await ctx.send("Games cannot be started in this channel!")
            return
        pings_enabled = (config is not None and config.pings_enabled)

        if ctx.channel.id in self.games:
            await ctx.send("A game is currently going on! Wait till the players finish up to start again.")
            return
        game = NameGameSession(mode.lower())
        game.state_lock = asyncio.Lock(loop=self.bot.loop)
        game.pings_enabled = pings_enabled
        game.players[ctx.author] = 0
        game.current_player = ctx.author
        for player in ctx.message.mentions:
            if player == ctx.author:
                continue
            if player.bot:
                await ctx.send(f"You can't invite bot users like {player.mention}!")
                continue
            game.players[player] = 0
        await self.send_turn_embed(ctx, game,
                                   title=f"{mode.upper()} Name Game",
                                   description="A game has been started! The info about the game is as follows:",
                                   color=discord.Color.green())
        await self.notify(ctx, game, f"{game.current_player.mention}, start us off!")
        # await ctx.send(f"{game.current_player.mention}, start us off!")
        self.games[ctx.channel.id] = game
        game.turn_task = self.bot.loop.create_task(self.game_turn_countdown(ctx, game))

    startround.example_usage = """
    `{prefix}ng startround frc` - start an FRC namegame session.
    """

    @ng.command()
    @game_is_running
    async def addplayer(self, ctx):
        """Add players to the current game.
        Only works if the user is currently playing."""
        if ctx.channel.id not in self.games:
            await ctx.send(f"There's not a game going on! Start one with `{ctx.prefix}ng startround`")
            return
        game = self.games[ctx.channel.id]

        with await game.state_lock:
            added = False
            players = ctx.message.mentions or [ctx.author]
            for player in ctx.message.mentions:
                if player.bot:
                    await ctx.send(f"You can't invite bot users like {player.mention}!")
                    continue

                if player in game.removed_players:
                    await ctx.send(f"{player.mention} is already out of the game and can't be added back in.")
                elif player in game.players:
                    await ctx.send(f"{player.mention} is already in the game!")
                game.players[player] = 0
                added = True

            if not added:
                return
            await ctx.send(embed=game.create_embed(
                title="Players have been added to the game.",
                description="See below for an updated player list.",
                color=discord.Color.blurple()
            ))

    addplayer.example_usage = """
    `{prefix}ng addplayer @user1, @user2` - add user1 and user2 to the game.
    """

    @ng.command()
    @game_is_running
    async def pick(self, ctx, team: int, *, name):
        """Attempt to pick a team in a game."""
        game = self.games[ctx.channel.id]

        with await game.state_lock:
            if ctx.author != game.current_player:
                if ctx.author in game.players:
                    await ctx.send(
                        "It's not your turn! You've been given a strike for this behaviour! Don't let it happen again...")
                    await self.strike(ctx, game, ctx.author)
                else:
                    await ctx.send(
                        f"Let the people playing play! If you want to join, ask one of the people currently playing to "
                        f"run `{ctx.prefix}ng addplayer {ctx.author.display_name}`")
                return

            if game.time < 0:
                await ctx.send("Vote on the current team before picking the next!")
                return

            if game.number != 0 and str(game.number) != str(team)[0]:
                await self.skip_player(ctx, game, ctx.author,
                                       "Your team doesn't start with the correct digit! Strike given, moving onto the next player!")
                return
            if team in game.picked:
                await self.skip_player(ctx, game, ctx.author,
                                       "That team has already been picked! You have been skipped and given a strike.")
                return

            ratio = await game.check_name(ctx, team, name)
            if ratio == -1:
                # nonexistant team
                await self.skip_player(ctx, game, ctx.author,
                                       f"Team {team} doesn't exist! Strike given, moving onto the next player!")
                return
            if ratio > 60:
                game.picked.append(team)
                game.number = game.last_team % 10
                game.next_turn()
                game.vote_correct = True
                game.vote_time = 20
                game.vote_player = ctx.author
                await self.send_turn_embed(ctx, game,
                                           title="Team correct!",
                                           description=f"Team {team} ({game.last_name}) was {ratio}% correct! Moving "
                                                       f"onto the next player as follows. Click the red X to override "
                                                       f"this decision.",
                                           color=discord.Color.green(),
                                           extra_fields=[("Voting Time", game.vote_time)])
                await game.turn_msg.add_reaction('❌')
                await self.notify(ctx, game, f"{game.current_player.mention}, you're up! Current number: {game.number}")
                game.vote_msg = game.turn_msg
                game.vote_embed = game.turn_embed

            # EXTREMELY INCOMPLETE LOL
            # (not anymore)
            else:
                game.time = -1
                game.vote_time = 60
                game.vote_player = ctx.author
                game.vote_correct = False
                vote_embed = discord.Embed()
                vote_embed.color = discord.Color.gold()
                vote_embed.title = "A vote is needed!"
                vote_embed.description = "A player has made a choice with less than 50% similarity. The details of the " \
                                         "pick are below. Click on the two emoji to vote if this is correct or not. A" \
                                         " 50% majority of players is required to accept it, otherwise the player will " \
                                         "get a strike."
                vote_embed.add_field(name="Player", value=game.current_player.mention)
                vote_embed.add_field(name="Team", value=team)
                vote_embed.add_field(name="Said Name", value=name)
                vote_embed.add_field(name="Actual Name", value=game.last_name)
                vote_embed.add_field(name="Similarity", value=f"{ratio}%")
                vote_embed.add_field(name="Voting Time", value=game.vote_time)
                game.vote_embed = vote_embed
                game.vote_msg = await ctx.send(embed=vote_embed)
                await game.vote_msg.add_reaction('✅')
                await game.vote_msg.add_reaction('❌')
                game.vote_task = self.bot.loop.create_task(self.game_vote_countdown(ctx, game))

    pick.example_usage = """
    `{prefix}ng pick 254 poofy cheeses` - attempt to guess team 254 with a specified name of "poofy cheeses".
    """

    @ng.command()
    @game_is_running
    async def drop(self, ctx):
        """Drops a player from the current game by eliminating them. Once dropped, they can no longer rejoin."""
        game = self.games[ctx.channel.id]
        with await game.state_lock:
            if ctx.author not in game.players:
                await ctx.send("You can't leave a game you're not in!")
                return
            game.players[ctx.author] = 2
            if ctx.author == game.current_player:
                await self.skip_player(ctx, game, ctx.author)
            else:
                await self.strike(ctx, game, ctx.author)
            if game.running:
                await self.display_info(ctx, game)

    drop.example_usage = """
    `{prefix}ng drop` - remove the initiator of the command from the current game
    """

    @ng.command()
    @game_is_running
    async def skip(self, ctx):
        """Skips the current player if the player wishes to forfeit their turn."""
        game = self.games[ctx.channel.id]
        with await game.state_lock:
            if ctx.author != game.current_player:
                await ctx.send("It's not your turn! Only the current player can skip their turn!")
            else:
                await self.skip_player(ctx, game, ctx.author)

    skip.example_usage = """
    `{prefix}ng skip` - skip the current player's turn
    """

    @ng.command()
    @game_is_running
    async def gameinfo(self, ctx):
        """Display info about the currently running game."""
        game = self.games[ctx.channel.id]
        await self.display_info(ctx, game)

    gameinfo.example_usage = """
    `{prefix}ng gameinfo` - display info about the currently running game.
    """

    @ng.command()
    async def leaderboard(self, ctx, mode: str = None):
        """Display top numbers of wins for the specified game mode"""
        if mode is None:
            config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
            mode = SUPPORTED_MODES[0] if config is None else config.mode
        if mode not in SUPPORTED_MODES:
            await ctx.send(
                f"Game mode `{mode}` not supported! Please pick a mode that is one of: `{', '.join(SUPPORTED_MODES)}`")
            return

        leaderboard = await NameGameLeaderboard.select(game_mode=mode, _extra_sql="ORDER BY wins DESC LIMIT 10")
        #sorted(session.query(NameGameLeaderboard).filter_by(game_mode=mode).all(),
        #          key=lambda i: i.wins, reverse=True)[:10]
        embed = discord.Embed(color=discord.Color.gold(), title=f"{mode.upper()} Name Game Leaderboard")
        for idx, entry in enumerate(leaderboard, 1):
            user = ctx.bot.get_user(entry.user_id)
            if user is None:
                embed.add_field(name=f"#{idx}: {entry.user_id}", value=entry.wins)
            else:
                embed.add_field(name=f"#{idx}: {user.display_name}", value=entry.wins)

        await ctx.send(embed=embed)

    leaderboard.example_usage = """
    `{prefix}ng leaderboard ftc` - display the namegame winning leaderboards for FTC.
    """

    @ng.command()
    async def teamstats(self, ctx, team: int = None, mode: str = None):
        """Display team usage statistics for either a specific team or the top 10 most used teams."""
        if mode is None:
            config = await NameGameConfig.select_one(guild_id=ctx.guild.id)
            mode = SUPPORTED_MODES[0] if config is None else config.mode
        if mode not in SUPPORTED_MODES:
            await ctx.send(
                f"Game mode `{mode}` not supported! Please pick a mode that is one of: `{', '.join(SUPPORTED_MODES)}`")
            return
        if team is None:
            leaderboard = await NameGameTeamStats.select(game_mode=mode, _extra_sql="ORDER BY uses DESC LIMIT 10")
            embed = discord.Embed(color=discord.Color.gold(), title=f"{mode.upper()} Name Game Team Usage Leaderboard")
            for idx, entry in enumerate(leaderboard, 1):
                # TODO: add name field to team
                embed.add_field(name=f"#{idx}: {entry.team_id}", value=entry.uses)
            await ctx.send(embed=embed)
        else:
            entry = await NameGameTeamStats.select_one(game_mode=mode, team_id=team)
            if entry is None:
                await ctx.send(f"Sadly, it appears nobody has picked `{team}` in a game yet. Maybe you can be the first?")
            else:
                ret = f"`{team}` has been used in name games **{entry.uses}** time(s)."
                if (str(team)[0] == '2' or str(team)[-1] == '2') and entry.uses > 10:
                    ret += "\nMaybe it has something to do with the 2..."
                if mode.lower() == 'ftc':
                    ret += "\n" + {
                        1: "Unlimited potential, I'm telling you.",
                        9421: "Hey, isn't that Ilan's team? I don't remember their name.",
                        9656: "Hey, isn't that Justin's team? I don't remember their name either.",
                        16236: "It's the funny juice team.",
                        }.get(team, "")
                await ctx.send(ret)


    async def strike(self, ctx, game, player):
        """Gives a player a strike."""
        if game.strike(player):
            await ctx.send(f"Player {player.mention} is ELIMINATED!")
        if len(game.players) == 0 or game.turn_count <= 6:
            await ctx.send("Game disbanded, no winner called!")
            game.running = False
        if game.check_win():
            # winning condition
            winner = list(game.players.keys())[0]

            record = await NameGameLeaderboard.select_one(user_id=winner.id, guild_id=ctx.guild.id, game_mode=game.mode)
            if record is None:
                record = NameGameLeaderboard(user_id=winner.id, guild_id=ctx.guild.id, wins=1, game_mode=game.mode)
                await record.insert()
            else:
                record.wins += 1
                await record.update()
            win_embed = discord.Embed()
            win_embed.color = discord.Color.gold()
            win_embed.title = "We have a winner!"
            win_embed.add_field(name="Winning Player", value=winner)
            win_embed.add_field(name="Wins Total", value=record.wins)
            win_embed.add_field(name="Teams Picked", value=game.get_picked())
            await ctx.send(embed=win_embed)

            game.running = False

        if not game.running:
            async with orm.acquire() as conn:
                for team in game.picked:
                    stats = await NameGameTeamStats.select_one(team_id=team, game_mode=game.mode, _conn=conn)
                    if stats is None:
                        stats = NameGameTeamStats(team_id=team, game_mode=game.mode, uses=1)
                        await stats.insert(_conn=conn)
                    else:
                        stats.uses += 1
                        await stats.update(_conn=conn)

            self.games.pop(ctx.channel.id)

    async def display_info(self, ctx, game):
        """Displays info about the current game"""
        info_embed = discord.Embed(title="Current Game Info", color=discord.Color.blue())
        info_embed.add_field(name="Game Type", value=game.mode.upper())
        info_embed.add_field(
            name="Strikes",
            value="\n".join([f"{player.display_name}: {strikes}" for player, strikes in game.players.items()])
        )
        info_embed.add_field(name="Current Player", value=game.current_player)
        info_embed.add_field(name="Current Number", value=game.number or "Wildcard")
        info_embed.add_field(name="Time Left", value=game.time)
        info_embed.add_field(name="Teams Picked", value=game.get_picked())
        await ctx.send(embed=info_embed)

    async def skip_player(self, ctx, game, player, msg=None):
        """Skips a player"""
        if msg is not None:
            await ctx.send(msg)
        game.vote_time = -1
        game.next_turn()
        await self.send_turn_embed(ctx, game,
                                   title=f"Player {player.display_name} was skipped and now has {game.players[player]+1} strike(s)!",
                                   color=discord.Color.red())
        if player != game.current_player:
            await self.notify(ctx, game, f"{game.current_player.mention}, you're up! Current number: {game.number}")
        await self.strike(ctx, game, player)

    # send an embed that starts a new turn
    async def send_turn_embed(self, ctx, game, **kwargs):
        """Sends an embed that starts a new turn"""
        game.turn_embed = game.create_embed(**kwargs)
        game.turn_msg = await ctx.send(embed=game.turn_embed)

    async def notify(self, ctx, game, msg):
        """Notifies people in the channel when it's their turn."""
        if game.pings_enabled:
            await ctx.send(msg)

    @Cog.listener("on_reaction_add")
    async def on_reaction_add(self, reaction, user):
        """When reactions are added, trigger the voting handler"""
        if reaction.message.channel.id not in self.games:
            return
        game = self.games[reaction.message.channel.id]
        with await game.state_lock:
            if game.vote_msg is None or game.vote_time <= 0:
                return
            self._on_reaction(game, reaction, user, 1)

            # also handle voting logic
            ctx = await self.bot.get_context(reaction.message)
            if game.vote_correct:
                if game.fail_tally > .5 * len(game.players):
                    await ctx.send(f"The decision was overruled! Player {game.vote_player.mention} is given a strike!")
                    await self.strike(ctx, game, game.vote_player)
                    game.vote_time = -1
            else:
                if game.pass_tally >= .5 * len(game.players):
                    game.picked.append(game.last_team)
                    game.number = game.last_team % 10
                    game.next_turn()
                    await self.send_turn_embed(ctx, game,
                                               title="Team correct!",
                                               description=f"Team {game.last_team} ({game.last_name}) was correct! "
                                                           f"Moving onto the next player as follows.",
                                               color=discord.Color.green())
                    await self.notify(ctx, game,
                                      f"{game.current_player.mention}, you're up! Current number: {game.number}")
                    game.vote_time = -1
                elif game.fail_tally >= .5 * len(game.players):
                    await ctx.send(
                        f"Team {game.last_team} was guessed wrong! Strike given to the responsible player and player is skipped.")
                    await self.skip_player(ctx, game, game.current_player)
                    game.vote_time = -1

    @Cog.listener()
    async def on_reaction_remove(self, reaction, user):
        """When a reaction is removed, do vote handling"""
        if reaction.message.channel.id not in self.games:
            return
        game = self.games[reaction.message.channel.id]

        with await game.state_lock:
            if game.vote_msg is None or game.vote_time <= 0:
                return
            self._on_reaction(game, reaction, user, -1)

    def _on_reaction(self, game, reaction, user, inc):
        """Handles pass/fail reactions"""
        if reaction.message.id == game.vote_msg.id and user in game.players:
            if reaction.emoji == '❌':
                game.fail_tally += inc

            if reaction.emoji == '✅':
                game.pass_tally += inc
        return game

    @keep_alive
    async def game_turn_countdown(self, ctx, game):
        """Counts down the time remaining left in the turn"""
        await asyncio.sleep(1)
        with await game.state_lock:
            if not game.running:
                return
            if game.time > 0:
                game.time -= 1
                game.turn_embed.set_field_at(3, name="Time Left", value=game.time)

            if game.vote_time > 0 and game.vote_correct:
                game.vote_time -= 1
                game.turn_embed.set_field_at(4, name="Voting Time", value=game.vote_time)

            if game.time % 5 == 0:
                await game.turn_msg.edit(embed=game.turn_embed)

            if game.time == 0:
                await self.skip_player(ctx, game, game.current_player)
            game.turn_task = self.bot.loop.create_task(self.game_turn_countdown(ctx, game))

    @keep_alive
    async def game_vote_countdown(self, ctx, game):
        """Counts down the time remaining left to vote"""
        await asyncio.sleep(1)
        with await game.state_lock:
            if not (game.running and not game.vote_correct and game.vote_embed and game.vote_time > 0):
                return
            game.vote_time -= 1
            game.vote_embed.set_field_at(5, name="Voting Time", value=game.vote_time)
            if game.vote_time % 5 == 0:
                await game.vote_msg.edit(embed=game.vote_embed)
            if game.vote_time == 0:
                await ctx.send(
                    "The vote did not reach 50% in favor or in failure, so the responsible player is given a strike and skipped.")
                await self.skip_player(ctx, game, game.current_player)

            game.vote_task = self.bot.loop.create_task(self.game_vote_countdown(ctx, game))


class NameGameConfig(orm.Model):
    """Configuration storage object"""
    __tablename__ = "namegame_config"
    __primary_key__ = ("guild_id",)
    guild_id: psqlt.bigint
    channel_id: psqlt.bigint
    mode: psqlt.text
    pings_enabled: psqlt.bigint

# DROP namegame_leaderboard;
class NameGameLeaderboard(orm.Model):
    """Leaderboard storage object"""
    __tablename__ = "namegame_leaderboard"
    __primary_key__ = ("user_id", "guild_id")
    user_id: psqlt.bigint
    guild_id: psqlt.bigint
    wins: psqlt.bigint
    game_mode: psqlt.text

class NameGameTeamStats(orm.Model):
    """interesting team frequency stats"""
    __tablename__ = "namegame_teamstats"
    __primary_key__ = ("team_id", "game_mode")
    team_id: psqlt.bigint
    game_mode: psqlt.text
    team_nickname: psqlt.text
    uses: psqlt.bigint

def setup(bot):
    """Adds the namegame cog to the bot"""
    bot.add_cog(NameGame(bot))

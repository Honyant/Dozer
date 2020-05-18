"""Commands for making and seeing robotics team associations."""

import collections
import discord
from discord.ext.commands import BadArgument, guild_only

from ._utils import *
from ..asyncdb.orm import orm
from ..asyncdb import psqlt

# alter table team_numbers alter column team_number type text
class Teams(Cog):
    """Commands for making and seeing robotics team associations."""

    @classmethod
    def validate(cls, team_type, team_number):
        if not team_number.isalnum() or not team_number.isascii():
            raise BadArgument("Team numbers must be alphanumeric!")
        z = team_type.casefold()
        if z not in ("fll", "ftc", 'frc', 'vrc', 'vex', 'vexu'):
            raise BadArgument("Unrecognized team type " + team_type[:32])

        if z in ("fll", "ftc", 'frc'):
            if not team_number.isdigit():
                raise BadArgument("FIRST team numbers must be numeric!")

        if z == 'vexu':
            if len(team_number) > 6:
                raise BadArgument("Invalid VexU team number specified!")
        
        if z == 'vex':
            z = 'vrc'
        if z == 'vrc':
            if not (len(team_number) <= 2 and team_number[:-1].isdigit() and team_number[1].isalpha()):
                raise BadArgument("Invalid Vex team number specified!")

        return z, team_number.upper()

    @command()
    async def setteam(self, ctx, team_type, team_number):
        """Sets an association with your team in the database."""
        team_type, team_number = self.validate(team_type, team_number)

        dbcheck = await TeamNumbers.select_one(user_id=ctx.author.id, team_number=team_number, team_type=team_type)
        if dbcheck is None:
            dbtransaction = TeamNumbers(user_id=ctx.author.id, team_number=team_number, team_type=team_type)
            await dbtransaction.insert()
            await ctx.send("Team number set! Note that unlike FRC Dozer, this will not affect your nickname "
                           "when joining other servers.")
        else:
            raise BadArgument("You are already associated with that team!")

    setteam.example_usage = """
    `{prefix}setteam type team_number` - Creates an association in the database with a specified team
    """

    @command()
    async def removeteam(self, ctx, team_type, team_number):
        """Removes an association with a team in the database."""
        team_type, team_number = self.validate(team_type, team_number)
        results = await TeamNumbers.select_one(user_id=ctx.author.id, team_number=team_number, team_type=team_type)
        if results is not None:
            await results.delete()
            await ctx.send("Removed association with {} team {}".format(team_type, team_number))
        if results is None:
            await ctx.send("Couldn't find any associations with that team!")

    removeteam.example_usage = """
    `{prefix}removeteam type team_number` - Removes your associations with a specified team
    """

    @command()
    @guild_only()
    async def teamsfor(self, ctx, user: discord.Member = None):
        """Allows you to see the teams for the mentioned user. If no user is mentioned, your teams are displayed."""
        if user is None:
            user = ctx.author
        
        teams = await TeamNumbers.select(user_id=user.id)
        if not teams:
            raise BadArgument("Couldn't find any team associations for that user!")
        else:
            e = discord.Embed(type='rich')
            e.title = 'Teams for {}'.format(user.display_name)
            e.description = "Teams: \n"
            for i in teams:
                e.description = "{} {} Team {} \n".format(e.description, i.team_type.upper(), i.team_number)
            await ctx.send(embed=e)

    teamsfor.example_usage = """
    `{prefix}teamsfor member` - Returns all team associations with the mentioned user. Assumes caller if blank.
    """

    @group(invoke_without_command=True)
    @guild_only()
    async def onteam(self, ctx, team_type, team_number):
        """Allows you to see who has associated themselves with a particular team."""
        team_type, team_number = self.validate(team_type, team_number)
        users = await TeamNumbers.select(team_number=team_number, team_type=team_type)
        if not users:
            await ctx.send("Nobody on that team found!")
        else:
            e = discord.Embed(type='rich')
            e.title = 'Users on team {}'.format(team_number)
            e.description = "Users: \n"
            for i in users:
                user = ctx.guild.get_member(i.user_id)
                if user is not None:
                    e.description = "{}{} {} \n".format(e.description, user.display_name, user.mention)
            await ctx.send(embed=e)

    onteam.example_usage = """
    `{prefix}onteam type team_number` - Returns a list of users associated with a given team type and number
    """

    @onteam.command()
    @guild_only()
    async def top(self, ctx):
        """Show the top 10 teams by number of members in this guild."""

        # adapted from the FRC Dozer's equivalent.
        query = f"""SELECT team_type, team_number, count(*)
                FROM {TeamNumbers.table_name()}
                WHERE user_id = ANY($1) --first param: list of user IDs
                GROUP BY team_type, team_number
                ORDER BY count DESC, team_type, team_number
                LIMIT 10"""

        async with orm.acquire() as conn:
            counts = await conn.fetch(query, [member.id for member in ctx.guild.members])

        embed = discord.Embed(title=f'Top teams in {ctx.guild.name}', color=discord.Color.blue())
        embed.description = '\n'.join(
            f'{ent["team_type"].upper()} team {ent["team_number"]} '
            f'({ent["count"]} member{"s" if ent["count"] > 1 else ""})' for ent in counts)
        await ctx.send(embed=embed)

    top.example_usage = """
    `{prefix}onteam top` - List the 10 teams with the most members in this guild
    """

class TeamNumbers(orm.Model):
    """DB object for tracking team associations."""
    __tablename__ = 'team_numbers'
    __primary_key__ = ("user_id", "team_number", "team_type")
    user_id: psqlt.bigint
    team_number: psqlt.text
    team_type: psqlt.text


def setup(bot):
    """Adds this cog to the main bot"""
    bot.add_cog(Teams(bot))

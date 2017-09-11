import discord
from discord.ext.commands import bot_has_permissions, has_permissions, BadArgument
from .. import db
from ._utils import *

class Roles(Cog):
	@group(invoke_without_command=True)
	@bot_has_permissions(manage_roles=True)
	async def giveme(self, ctx, *, roles):
		"""Give you one or more giveable roles, separated by commas."""
		requests = set(name.strip().casefold() for name in roles.split(','))
		giveables = self.giveable_roles(ctx.guild)
		
		already_have = set(role for name, role in giveables.items() if name in requests and role in ctx.author.roles)
		valid = set(role for name, role in giveables.items() if name in requests and role not in already_have)
		
		await ctx.author.add_roles(*valid)
		
		e = discord.Embed(color=discord.Color.blue())
		if valid:
			e.add_field(name='Gave you {} role(s)!'.format(len(valid)), value='\n'.join(role.name for role in valid), inline=False)
		if already_have:
			e.add_field(name='You already have {} role(s)!'.format(len(already_have)), value='\n'.join(role.name for role in already_have), inline=False)
		extra = len(requests) - (len(already_have) + len(valid))
		if extra > 0:
			e.add_field(name='{} role(s) could not be found!'.format(extra), value='Use `{0.prefix}{0.invoked_with} list` to find valid giveable roles!'.format(ctx), inline=False)
		await ctx.send(embed=e)
	
	@giveme.command()
	@bot_has_permissions(manage_roles=True)
	@has_permissions(manage_guild=True)
	async def add(self, ctx, *, name):
		"""Makes an existing role giveable, or creates one if it doesn't exist. Name must not contain commas.
		Similar to create, but will use an existing role if one exists."""
		if ',' in name:
			raise BadArgument('giveable role names must not contain commas!')
		role = discord.utils.get(ctx.guild.roles, name=name)
		if role is None:
			role = await ctx.guild.create_role(name=name, reason='Giveable role created by {}'.format(ctx.author))
		elif name.strip().casefold() in self.giveable_roles(ctx.guild):
			raise BadArgument('that role already exists and is giveable!')
		with db.Session() as session:
			giveables = session.query(GiveableRoles).filter_by(guild_id=ctx.guild.id).first()
			if giveables is None:
				giveables = GiveableRoles(guild_id=ctx.guild.id, role_ids=str(role.id))
				session.add(giveables)
			else:
				giveables.role_ids += ' ' + str(role.id)
		await ctx.send('Added giveable role {0.name!r}! Use `{1}{2} {0}` to get it!'.format(role, ctx.prefix, ctx.command.parent))
	
	@giveme.command()
	@bot_has_permissions(manage_roles=True)
	@has_permissions(manage_guild=True)
	async def create(self, ctx, *, name):
		"""Create a giveable role. Name must not contain commas.
		Similar to add, but will always create a new role."""
		if ',' in name:
			raise BadArgument('giveable role names must not contain commas!')
		if name.strip().casefold() in self.giveable_roles(ctx.guild):
			raise BadArgument('a duplicate role is giveable and would conflict!')
		role = await ctx.guild.create_role(name=name, reason='Giveable role created by {}'.format(ctx.author))
		with db.Session() as session:
			giveables = session.query(GiveableRoles).filter_by(guild_id=ctx.guild.id).first()
			if giveables is None:
				giveables = GiveableRoles(guild_id=ctx.guild.id, role_ids=str(role.id))
				session.add(giveables)
			else:
				giveables.role_ids += ' ' + str(role.id)
		await ctx.send('Created giveable role {0.name!r}! Use `{1}{2} {0}` to get it!'.format(role, ctx.prefix, ctx.command.parent))
	
	@giveme.command()
	@bot_has_permissions(manage_roles=True)
	async def remove(self, ctx, *, roles):
		"""Removes multiple giveable roles from you. Names must be separated by commas."""
		requests = set(name.strip().casefold() for name in roles.split(','))
		giveables = self.giveable_roles(ctx.guild)
		
		valid = set(role for name, role in giveables.items() if name in requests and role in ctx.author.roles)
		dont_have = set(role for name, role in giveables.items() if name in requests and role not in valid)
		
		await ctx.author.remove_roles(*valid)
		
		e = discord.Embed(color=discord.Color.blue())
		if valid:
			e.add_field(name='Removed {} role(s)!'.format(len(valid)), value='\n'.join(role.name for role in valid), inline=False)
		if dont_have:
			e.add_field(name='You didn\'t have {} role(s)!'.format(len(dont_have)), value='\n'.join(role.name for role in dont_have), inline=False)
		extra = len(requests) - (len(valid) + len(dont_have))
		if extra > 0:
			e.add_field(name='{} role(s) could not be found!'.format(extra), value='Use `{0.prefix}{0.command.parent} list` to find valid giveable roles!'.format(ctx), inline=False)
		await ctx.send(embed=e)
	
	@giveme.command()
	@bot_has_permissions(manage_roles=True)
	@has_permissions(manage_guild=True)
	async def delete(self, ctx, *, name):
		"""Deletes and removes a giveable role."""
		roles = self.giveable_roles(ctx.guild)
		stripped = name.casefold().strip()
		if stripped not in roles:
			raise BadArgument('{} is not a giveable role!'.format(name))
		role = roles[stripped]
		await role.delete()
		with db.Session() as session:
			giveables = session.query(GiveableRoles).filter_by(guild_id=ctx.guild.id).first() # Null-checked by giveables containing name
			giveables.role_ids = giveables.role_ids.replace(str(role.id), '').replace('  ', ' ').strip()
		await ctx.send('Role {0.name!r} has been deleted!'.format(role))
	
	@giveme.command(name='list')
	@bot_has_permissions(manage_roles=True)
	async def list_roles(self, ctx):
		roles = self.giveable_roles(ctx.guild)
		e = discord.Embed(color=discord.Color.blue(), title='Giveable roles')
		e.description = '\n'.join(role.name for role in roles.values())
		await ctx.send(embed=e)
	
	@staticmethod
	def giveable_roles(guild):
		with db.Session() as session:
			roles = session.query(GiveableRoles).filter_by(guild_id=guild.id).first()
		if roles is None:
			return {}
		giveable_ids = [int(id_) for id_ in roles.role_ids.split(' ')]
		return {role.name.strip().casefold(): role for role in guild.roles if role.id in giveable_ids}

class GiveableRoles(db.DatabaseObject):
	__tablename__ = 'giveable_roles'
	guild_id = db.Column(db.Integer, primary_key=True)
	role_ids = db.Column(db.String, nullable=True)

def setup(bot):
	bot.add_cog(Roles(bot))

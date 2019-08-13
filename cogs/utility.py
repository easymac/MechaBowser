import asyncio
import logging
import re
import typing
import datetime
import aiohttp

import pymongo
import discord
from discord import Webhook, AsyncWebhookAdapter
from discord.ext import commands, tasks
import pymarkovchain

import config
import utils

mclient = pymongo.MongoClient(
	config.mongoHost,
	username=config.mongoUser,
	password=config.mongoPass
)

serverLogs = None
modLogs = None

class MarkovChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.markovChain = pymarkovchain.MarkovChain('markov')
        #self.dump_markov.start() # pylint: disable=no-member

    def cog_unload(self):
        pass
        #self.dump_markov.cancel() # pylint: disable=no-member

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.content or message.content.startswith('!') or message.content.startswith(','):
            # Might be an embed or a command, not useful
            return

        self.markovChain.generateDatabase(message.content)
        self.markovChain.dumpdb()

    @tasks.loop(seconds=30)
    async def dump_markov(self):
        logging.info('Taking a dump')
        self.markovChain.dumpdb()
        logging.info('I\'m done')

    @commands.command(name='markov')
    @commands.is_owner()
    async def _markov(self, ctx, seed: typing.Optional[str]):
        try:
            if seed:
                return await ctx.send(self.markovChain.generateStringWithSeed(seed))

            else:
                return await ctx.send(self.markovChain.generateString())
        except pymarkovchain.StringContinuationImpossibleError:
            return await ctx.send(':warning: Unable to generate chain with provided seed')

class ChatControl(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.modLogs = self.bot.get_channel(config.modChannel)
        self.linkRe = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        self.SMM2LevelID = re.compile(r'([0-9a-z]{3}-[0-9a-z]{3}-[0-9a-z]{3})', re.I | re.M)
        self.SMM2LevelPost = re.compile(r'Name: ?(\S.*)\n\n?(?:Level )?ID:\s*((?:[0-9a-z]{3}-){2}[0-9a-z]{3})(?:\s+)?\n\n?Style: ?(\S.*)\n\n?(?:Theme: ?(\S.*)\n\n?)?(?:Tags: ?(\S.*)\n\n?)?Difficulty: ?(\S.*)\n\n?Description: ?(\S.*)', re.I)
        self.affiliateLinks = re.compile(r'(https?:\/\/(?:.*\.)?(?:(?:amazon)|(?:bhphotovideo)|(?:bestbuy)|(?:ebay)|(?:gamestop)|(?:groupon)|(?:newegg(?:business)?)|(?:stacksocial)|(?:target)|(?:tigerdirect)|(?:walmart))\.[a-z\.]{2,7}\/.*)(?:\?.+)', re.I)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.type != discord.MessageType.default:
            return

        #Filter test for afiliate links
        if re.search(self.affiliateLinks, message.content):
            hooks = await message.channel.webhooks()
            useHook = await message.channel.create_webhook(name=f'mab_{message.channel.id}', reason='No webhooks existed; 1<= required for chat filtering') if not hooks else hooks[0]

            await message.delete()
            async with aiohttp.ClientSession() as session:
                name = message.author.name if not message.author.nick else message.author.nick
                webhook = Webhook.from_url(useHook.url, adapter=AsyncWebhookAdapter(session))
                await webhook.send(content=re.sub(self.affiliateLinks, r'\1', message.content), username=name, avatar_url=message.author.avatar_url)

        #Filter for #mario
        if message.channel.id == 325430144993067049: # #mario
            if re.search(self.SMM2LevelID, message.content):
                if re.search(self.linkRe, message.content):
                    return # TODO: Check if SMM2LevelID found in linkRe to correct edge case

                await message.delete()
                response = await message.channel.send(f'<:redTick:402505117733224448> <@{message.author.id}> Please do not post Super Mario Maker 2 level codes '\
                    'here. Post in <#595203237108252672> with the pinned template instead.')

                await response.delete(delay=20)
            return

        #Filter for #smm2-levels
        if message.channel.id == 595203237108252672:
            if not re.search(self.SMM2LevelID, message.content):
                # We only want to filter posts with a level id
                return

            block = re.search(self.SMM2LevelPost, message.content)
            if not block:
                # No match for a properly formatted level post
                response = await message.channel.send(f'<:redTick:402505117733224448> <@{message.author.id}> Your level is formatted incorrectly, please see the pinned messages for the format. A copy '\
                    f'of your message is included and will be deleted shortly. You can resubmit your level at any time.\n\n```{message.content}```')
                await message.delete()
                return await response.delete(delay=25)

            # Lets make this readable
            levelName = block.group(1)
            levelID = block.group(2)
            levelStyle = block.group(3)
            levelTheme = block.group(4)
            levelTags = block.group(5)
            levelDifficulty = block.group(6)
            levelDescription = block.group(7)

            embed = discord.Embed(color=discord.Color(0x6600FF))
            embed.set_author(name=str(message.author), icon_url=message.author.avatar_url)
            embed.add_field(name='Name', value=levelName, inline=True)
            embed.add_field(name='Level ID', value=levelID, inline=True)
            embed.add_field(name='Description', value=levelDescription, inline=False)
            embed.add_field(name='Style', value=levelStyle, inline=True)
            embed.add_field(name='Difficulty', value=levelDifficulty, inline=True)
            if levelTheme:
                embed.add_field(name='Theme', value=levelTheme, inline=False)
            if levelTags:
                embed.add_field(name='Tags', value=levelTags, inline=False)

            try:
                await message.channel.send(embed=embed)
                await message.delete()

            except discord.errors.Forbidden:
                # Fall back to leaving user text
                logging.error(f'[Filter] Unable to send embed to {message.channel.id}')
            return

#        # Splatoon splatfest event - ended 7/21/19
#        if message.channel.id == 278557283019915274:
#            pearl = re.compile(r'(<:pearl:332557519958310912>)+', re.I)
#            marina = re.compile(r'(<:marina:332557579815485451>)+', re.I)
#            orderRole = message.guild.get_role(601458524723216385)
#            chaosRole = message.guild.get_role(601458570197860449)
#            if re.search(pearl, message.content) and re.search(marina, message.content):
#                return
#
#            try:    
#                if re.search(pearl, message.content):
#                    if orderRole in message.author.roles:
#                        await message.author.remove_roles(orderRole)
#
#                    if chaosRole not in message.author.roles:
#                        msg = await message.channel.send(f'<@{message.author.id}> You are now registered as a member of Team Chaos')
#                        await msg.delete(delay=5.0)
#                        await message.author.add_roles(chaosRole)
#
#                elif re.search(marina, message.content):
#                    if chaosRole in message.author.roles:
#                        await message.author.remove_roles(chaosRole)
#
#                    if orderRole not in message.author.roles:
#                        msg = await message.channel.send(f'<@{message.author.id}> You are now registered as a member of Team Order')
#                        await msg.delete(delay=5.0)
#                        await message.author.add_roles(orderRole)
#
#            except (discord.Forbidden, discord.HTTPException):
#                pass

    @commands.command(name='ping')
    async def _ping(self, ctx):
        initiated = ctx.message.created_at
        msg = await ctx.send('Evaluating...')
        return await msg.edit(content=f'Pong! Roundtrip latency {(msg.created_at - initiated).total_seconds()} seconds')

    @commands.command(name='clean')
    @commands.has_any_role(config.moderator, config.eh)
    async def _clean(self, ctx, messages: int, members: commands.Greedy[discord.Member]):
        if messages >= 100:
            def confirm_check(reaction, member):
                return member == ctx.author and str(reaction.emoji) in [config.redTick, config.greenTick]

            confirmMsg = await ctx.send(f'This action will delete up to {messages}, are you sure you want to proceed?')
            await confirmMsg.add_reaction(config.greenTick)
            await confirmMsg.add_reaction(config.redTick)
            try:
                reaction = await self.bot.wait_for('reaction_add', timeout=15, check=confirm_check)
                if str(reaction[0]) != config.greenTick:
                    await confirmMsg.edit(content='Clean action canceled.')
                    return await confirmMsg.clear_reactions()

            except asyncio.TimeoutError:
                await confirmMsg.edit(content='Confirmation timed out, clean action canceled.')
                return await confirmMsg.clear_reactions()

            else:
                await confirmMsg.delete()
            
        memberList = None if not members else [x.id for x in members]

        def message_filter(message):
            return True if not memberList or message.author.id in memberList else False

        await ctx.message.delete()
        deleted = await ctx.channel.purge(limit=messages, check=message_filter, bulk=True)
    
        m = await ctx.send(f'{config.greenTick} Clean action complete')
        archiveID = await utils.message_archive(list(reversed(deleted)))

        embed = discord.Embed(description=f'Archive URL: {config.baseUrl}/archive/{archiveID}', color=0xF5A623, timestamp=datetime.datetime.utcnow())
        await self.bot.get_channel(config.logChannel).send(f':printer: New message archive generated for {ctx.channel.mention}', embed=embed)

        return await m.delete(delay=5)

    @commands.command(name='info')
    @commands.has_any_role(config.moderator, config.eh)
    async def _info(self, ctx, user: typing.Union[discord.Member, int]):
        inServer = True
        if type(user) == int:
            # User doesn't share the ctx server, fetch it instead
            dbUser = mclient.bowser.users.find_one({'_id': user})
            inServer = False
            try:
                user = await self.bot.fetch_user(user)

            except discord.NotFound:
                return await ctx.send(f'{config.redTick} User does not exist')

            if not dbUser:
                embed = discord.Embed(color=discord.Color(0x18EE1C), description=f'Fetched information about {user.mention} from the API because they are not in this server. There is little information to display as such')
                embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.avatar_url)
                embed.set_thumbnail(url=user.avatar_url)
                embed.add_field(name='Created', value=user.created_at.strftime('%B %d, %Y %H:%M:%S UTC'))
                return await ctx.send(embed=embed) # TODO: Return DB info if it exists as well

        else:
            dbUser = mclient.bowser.users.find_one({'_id': user.id})

        # Member object, loads of info to work with
        messages = mclient.bowser.messages.find({'author': user.id})
        msgCount = 0 if not messages else messages.count()

        desc = f'Fetched user {user.mention}' if inServer else f'Fetched information about previous member {user.mention} ' \
            'from the API because they are not in this server. ' \
            'Showing last know data from before they left.'
        embed = discord.Embed(color=discord.Color(0x18EE1C), description=desc)
        embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.avatar_url)
        embed.set_thumbnail(url=user.avatar_url)
        embed.add_field(name='Messages', value=str(msgCount), inline=True)
        if inServer:
            embed.add_field(name='Join date', value=user.joined_at.strftime('%B %d, %Y %H:%M:%S UTC'), inline=True)
        roleList = []
        if inServer:
            for role in reversed(user.roles):
                if role.id == user.guild.id:
                    continue

                roleList.append(role.name)

        else:
            roleList = dbUser['roles']
            
        if not roleList:
            # Empty; no roles
            roles = '*User has no roles*'

        else:
            roles = ', '.join(roleList)

        embed.add_field(name='Roles', value=roles, inline=False)

        lastMsg = 'N/a' if msgCount == 0 else datetime.datetime.utcfromtimestamp(messages.sort('timestamp',pymongo.DESCENDING)[0]['timestamp']).strftime('%B %d, %Y %H:%M:%S UTC')
        embed.add_field(name='Last message', value=lastMsg, inline=True)
        embed.add_field(name='Created', value=user.created_at.strftime('%B %d, %Y %H:%M:%S UTC'), inline=True)
        punishments = ''
        punsCol = mclient.bowser.puns.find({'user': user.id})
        if not punsCol.count():
            punishments = '__*No punishments on record*__'

        else:
            puns = 0
            for pun in punsCol:
                if puns >= 5:
                    break

                puns += 1
                stamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%m/%d/%y %H:%M:%S UTC')
                punType = config.punStrs[pun['type']]
                if pun['type'] in ['clear', 'unmute', 'unban', 'unblacklist']:
                    punishments += f'- [{stamp}] {punType}\n'

                else:
                    punishments += f'+ [{stamp}] {punType}\n'

            punishments = f'Showing {puns}/{punsCol.count()} punishment entries. ' \
                f'For a full history including responsible moderator, active status, and more use `{ctx.prefix}history @{str(user)}` or `{ctx.prefix}history {user.id}`' \
                f'\n```diff\n{punishments}```'
        embed.add_field(name='Punishments', value=punishments, inline=False)
        return await ctx.send(embed=embed)

    @commands.command(name='history')
    @commands.has_any_role(config.moderator, config.eh)
    async def _history(self, ctx, user: discord.User):
        db = mclient.bowser.puns
        puns = db.find({'user': user.id})
        if not puns.count():
            return await ctx.send(f'{config.redTick} User has no punishments on record')

        punNames = {
            'tier1': 'T1 Warn',
            'tier2': 'T2 Warn',
            'tier3': 'T3 Warn',
            'clear': 'Warn Clear',
            'mute': 'Mute',
            'unmute': 'Unmute',
            'kick': 'Kick',
            'ban': 'Ban',
            'unban': 'Unban',
            'blacklist': 'Blacklist ({})',
            'unblacklist': 'Unblacklist ({})'
        }

        if puns.count() == 1:
            desc = f'There is __1__ infraction record for this user:'

        else:
            desc = f'There are __{puns.count()}__ infraction records for this user:'

        embed = discord.Embed(title='Infraction History', description=desc, color=0x18EE1C)
        embed.set_author(name=f'{user} | {user.id}', icon_url=user.avatar_url)

        for pun in puns.sort('timestamp', pymongo.ASCENDING):
            datestamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%b %d, %y %H:%M UTC')
            moderator = ctx.guild.get_member(pun['moderator'])
            if not moderator:
                moderator = await self.bot.fetch_user(pun['moderator'])

            if pun['type'] in ['blacklist', 'unblacklist']:
                inf = punNames[pun['type']].format(pun['context'])

            else:
                inf = punNames[pun['type']]

            embed.add_field(name=datestamp, value=f'**Moderator:** {moderator}\n**Details:** [{inf}] {pun["reason"]}')

        return await ctx.send(embed=embed)
            

    @commands.command(name='roles')
    @commands.has_any_role(config.moderator, config.eh)
    async def _roles(self, ctx):
        roleList = 'List of roles in guild:\n```\n'
        for role in reversed(ctx.guild.roles):
            roleList += f'{role.name} ({role.id})\n'

        await ctx.send(f'{roleList}```')

    @commands.command(name='blacklist')
    @commands.has_any_role(config.moderator, config.eh)
    async def _roles_set(self, ctx, member: discord.Member, channel: discord.TextChannel, *, reason='-No reason specified-'):
        statusText = ''
        if channel.id == config.suggestions:
            suggestionsRole = ctx.guild.get_role(config.noSuggestions)
            if suggestionsRole in member.roles: # Toggle role off
                await member.remove_roles(suggestionsRole)
                statusText = 'Unblacklisted'

            else: # Toggle role on
                await member.add_roles(suggestionsRole)
                statusText = 'Blacklisted'

        elif channel.id == config.spoilers:
            spoilersRole = ctx.guild.get_role(config.noSpoilers)
            if spoilersRole in member.roles: # Toggle role off
                await member.remove_roles(spoilersRole)
                statusText = 'Unblacklisted'

            else: # Toggle role on
                await member.add_roles(spoilersRole)
                statusText = 'Blacklisted'         

        else:
            return await ctx.send(f'{config.redTick} You cannot blacklist a user from that channel')

        db = mclient.bowser.puns
        if statusText.lower() == 'blacklisted':
            await utils.issue_pun(member.id, ctx.author.id, 'blacklist', reason, context=channel.name)

        else:
            db.find_one_and_update({'user': member.id, 'type': 'blacklist', 'active': True, 'context': channel.name}, {'$set':{
            'active': False
            }})
            await utils.issue_pun(member.id, ctx.author.id, 'unblacklist', reason, active=False, context=channel.name)

        embed = discord.Embed(color=discord.Color(0xF5A623), timestamp=datetime.datetime.utcnow())
        embed.set_author(name=f'{statusText} | {str(member)}')
        embed.add_field(name='User', value=f'<@{member.id}>', inline=True)
        embed.add_field(name='Moderator', value=f'<@{ctx.author.id}>', inline=True)
        embed.add_field(name='Channel', value=channel.mention)
        embed.add_field(name='Reason', value=reason)

        await self.modLogs.send(embed=embed)

        try:
            statusText = 'blacklist' if statusText == 'Blacklisted' else 'unblacklist'
            await member.send(utils.format_pundm(statusText, reason, ctx.author, channel.mention))
        except (discord.Forbidden, AttributeError): # User has DMs off, or cannot send to Obj
            pass

        if await utils.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        await ctx.send(f'{config.greenTick} {member} has been {statusText.lower()}ed from {channel.mention}')

def setup(bot):
    global serverLogs
    global modLogs

    serverLogs = bot.get_channel(config.logChannel)
    modLogs = bot.get_channel(config.modChannel)

    bot.add_cog(ChatControl(bot))
    #bot.add_cog(MarkovChat(bot))
    logging.info('[Extension] Utility module loaded')

def teardown(bot):
    bot.remove_cog('ChatControl')
    #bot.remove_cog('MarkovChat')
    logging.info('[Extension] Utility module unloaded')
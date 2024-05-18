import asyncio
import io
import logging
import pathlib
import re
import time
import typing
import urllib.parse
from datetime import datetime, timezone

import aiohttp
import config
import discord
import pymongo
from discord import Webhook, WebhookType
from discord.ext import commands, tasks

import tools


mclient = pymongo.MongoClient(config.mongoURI)

serverLogs = None
modLogs = None


class ChatControl(commands.Cog, name='Utility Commands'):
    def __init__(self, bot):
        self.bot = bot
        self.modLogs = self.bot.get_channel(config.modChannel)
        self.adminChannel = self.bot.get_channel(config.adminChannel)
        self.boostChannel = self.bot.get_channel(config.boostChannel)
        self.affiliateTags = {
            "*": ["awc"],
            "amazon.*": ["colid", "coliid", "tag", "ascsubtag"],
            "bestbuy.*": ["aid", "cjpid", "lid", "pid"],
            "bhphotovideo.com": ["sid"],
            "ebay.*": ["afepn", "campid", "pid"],
            "gamestop.com": ["affid", "cid", "sourceid"],
            "groupon.*": ["affid"],
            "newegg*.*": ["aid", "pid"],
            "play-asia.com": ["tagid"],
            "stacksocial.com": ["aid", "rid"],
            "store.nintendo.co.uk": ["affil"],
            "tigerdirect.com": ["affiliateid", "srccode"],
            "walmart.*": ["sourceid", "veh", "wmlspartner"],
        }

    # Called after automod filter finished, because of the affilite link reposter. We also want to wait for other items in this function to complete to call said reposter.
    async def on_automod_finished(self, message):
        if message.type == discord.MessageType.premium_guild_subscription:
            boost_message = message.system_content.replace(
                message.author.name, f'{message.author.name} ({message.author.mention})'
            )
            await self.adminChannel.send(boost_message)
            await self.boostChannel.send(boost_message)

        if message.author.bot or message.type not in [discord.MessageType.default, discord.MessageType.reply]:
            logging.debug(f'on_automod_finished discarding non-normal-message: {message.type=}, {message.id=}')
            return

        # Filter and clean affiliate links
        # We want to call this last to ensure all above items are complete.
        links = tools.linkRe.finditer(message.content)
        if links:
            contentModified = False
            content = message.content
            for link in links:
                linkModified = False

                try:
                    urlParts = urllib.parse.urlsplit(link[0])
                except ValueError:  # Invalid URL edge case
                    continue

                urlPartsList = list(urlParts)

                query_raw = dict(urllib.parse.parse_qsl(urlPartsList[3]))
                # Make all keynames lowercase in dict, this shouldn't break a website, I hope...
                query = {k.lower(): v for k, v in query_raw.items()}

                # For each domain level of hostname, eg. foo.bar.example => foo.bar.example, bar.example, example
                labels = urlParts.hostname.split(".")
                for i in range(0, len(labels)):
                    domain = ".".join(labels[i - len(labels) :])

                    # Special case: rewrite 'amazon.*/exec/obidos/ASIN/.../' to 'amazon.*/dp/.../'
                    if pathlib.PurePath(domain).match('amazon.*'):
                        match = re.match(r'^/exec/obidos/ASIN/(\w+)/.*$', urlParts.path)
                        if match:
                            linkModified = True
                            urlPartsList[2] = f'/dp/{match.group(1)}'  # 2 = path

                    for glob, tags in self.affiliateTags.items():
                        if pathlib.PurePath(domain).match(glob):
                            for tag in tags:
                                if tag in query:
                                    linkModified = True
                                    query.pop(tag, None)

                if linkModified:
                    urlPartsList[3] = urllib.parse.urlencode(query)
                    url = urllib.parse.urlunsplit(urlPartsList)

                    contentModified = True
                    content = content.replace(link[0], url)

            if contentModified:
                useHook = None
                for h in await message.channel.webhooks():
                    if h.type == WebhookType.incoming and h.token:
                        useHook = h

                if not useHook:
                    # An incoming webhook does not exist
                    useHook = await message.channel.create_webhook(
                        name=f'mab_{message.channel.id}',
                        reason='No webhooks existed; 1 or more is required for affiliate filtering',
                    )

                async with aiohttp.ClientSession() as session:
                    webhook = Webhook.from_url(useHook.url, session=session)
                    webhook_message = await webhook.send(
                        content=content,
                        username=message.author.display_name,
                        avatar_url=message.author.display_avatar.url,
                        wait=True,
                    )

                    try:
                        await message.delete()
                    except Exception:
                        pass

                    embed = discord.Embed(
                        description='The above message was automatically reposted by Mecha Bowser to remove an affiliate marketing link. The author may react with 🗑️ to delete these messages.'
                    )

                    # #mab_remover is the special sauce that allows users to delete their messages, see on_raw_reaction_add()
                    icon_url = (
                        f'{message.author.display_avatar.url}#mab_remover_{message.author.id}_{webhook_message.id}'
                    )
                    embed.set_footer(text=f'Author: {str(message.author)} ({message.author.id})', icon_url=icon_url)

                    # A seperate message is sent so that the original message has embeds
                    embed_message = await message.channel.send(embed=embed)
                    await embed_message.add_reaction('🗑️')

    # Handle :wastebasket: reactions for user deletions on messages reposed on a user's behalf
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if not payload.member:
            return  # Not in a guild
        if payload.emoji.name != '🗑️':
            return  # Not a :wastebasket: emoji
        if payload.user_id == self.bot.user.id:
            return  # This reaction was added by this bot

        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        embed = None if not message.embeds else message.embeds[0]

        if message.author.id != self.bot.user.id:
            return  # Message is not from the bot
        if not embed:
            return  # Message does not have an embed

        allowed_remover = None
        target_message = None
        # Search for special url tag in footer/author icon urls:
        # ...#mab_remover_{remover} or ..#mab_remover_{remover}_{message}
        for icon_url in [embed.author.icon_url, embed.footer.icon_url]:
            if not icon_url:
                continue  # Location does not have an icon_url

            match = re.search(r'#mab_remover_(\d{15,25})(?:_(\d{15,25}))?$', icon_url)
            if not match:
                continue  # No special url tag here

            allowed_remover = match.group(1)
            target_message = match.group(2)
            break

        if not allowed_remover:  # No special url tag detected
            return
        if str(payload.user_id) != str(allowed_remover):  # Reactor is not the allowed remover
            try:
                await message.remove_reaction(payload.emoji, payload.member)
            except:
                pass
            return

        try:
            if target_message:
                msg = await channel.fetch_message(target_message)
                await msg.delete()

            await message.delete()
        except Exception as e:
            logging.warning(e)
            pass

    # Large block of old event commented out code was removed on 12/02/2020
    # Includes: Holiday season celebration, 30k members celebration, Splatoon splatfest event, Pokemon sword/shield event
    # https://github.com/rNintendoSwitch/MechaBowser/commit/373cef69aa5b9da7fe5945599b7dde387caf0700

    #    @commands.command(name='archive')
    #    async def _archive(self, ctx, members: commands.Greey[discord.Member], channels: commands.Greedy[discord.Channel], limit: typing.Optional[int] = 200, channel_limiter: typing.Greedy[discord.Channel]):
    #        pass

    @commands.command(name='clean')
    @commands.has_any_role(config.moderator, config.eh)
    async def _clean(self, ctx, messages: int, users: commands.Greedy[discord.User], extra: typing.Optional[str]):
        if extra:
            # This var will contain data if the greedy fails, such as if a non-ID or message ID are provided instead of a user
            return await ctx.send(
                f'{config.redTick} Some user(s) passed are invalid. Please check the ID(s) and ensure they are correct'
            )

        if messages > 2000 or messages <= 0:
            return await ctx.send(
                f'{config.redTick} Invalid message count {messages}. Must be greater than 0 and not more than 2000'
            )

        if messages >= 100:

            def confirm_check(reaction, member):
                return member == ctx.author and str(reaction.emoji) in [config.redTick, config.greenTick]

            confirmMsg = await ctx.send(
                f'This action will scan and delete up to {messages}, are you sure you want to proceed?'
            )
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

        userList = None if not users else [x.id for x in users]

        def message_filter(message):
            return True if not userList or message.author.id in userList else False

        await ctx.message.delete()
        deleted = await ctx.channel.purge(limit=messages, check=message_filter, bulk=True)

        m = await ctx.send(f'{config.greenTick} Clean action complete')
        return await m.delete(delay=5)

    @commands.group(name='slowmode', invoke_without_command=True)
    @commands.has_any_role(config.moderator, config.eh)
    async def _slowmode(self, ctx, duration, channel: typing.Optional[discord.TextChannel]):
        if not channel:
            channel = ctx.channel

        try:
            time, seconds = tools.resolve_duration(duration, include_seconds=True)
            time = tools.humanize_duration(time)
            seconds = int(seconds)
            if seconds < 1:
                return ctx.send(
                    f'{config.redTick} You cannot set the duration to less than one second. If you would like to clear the slowmode, use the `{ctx.prefix}slowmode clear` command'
                )

            elif seconds > 60 * 60 * 6:  # Six hour API limit
                return ctx.send(f'{config.redTick} You cannot set the duration greater than six hours')

        except KeyError:
            return await ctx.send(f'{config.redTick} Invalid duration passed')

        if channel.slowmode_delay == seconds:
            return await ctx.send(f'{config.redTick} The slowmode is already set to {time}')

        await channel.edit(slowmode_delay=seconds, reason=f'{ctx.author} has changed the slowmode delay')
        await channel.send(
            f':stopwatch: This channel now has a **{time}** slowmode in effect. Please be mindful of spam per the server rules'
        )
        if channel.id == ctx.channel.id or tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        await ctx.send(f'{config.greenTick} {channel.mention} now has a {time} slowmode')

    @_slowmode.command(name='clear')
    @commands.has_any_role(config.moderator, config.eh)
    async def _slowmode_clear(self, ctx, channel: typing.Optional[discord.TextChannel]):
        if not channel:
            channel = ctx.channel

        if channel.slowmode_delay == 0:
            return await ctx.send(f'{config.redTick} {channel.mention} is not under a slowmode')

        await channel.edit(slowmode_delay=0, reason=f'{ctx.author} has removed the slowmode delay')
        await channel.send(
            f':stopwatch: Slowmode for this channel is no longer in effect. Please be mindful of spam per the server rules'
        )
        if channel.id == ctx.channel.id or tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        return await ctx.send(f'{config.greenTick} {channel.mention} no longer has slowmode')

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
                desc = (
                    f'Fetched information about {user.mention} from the API because they are not in this server. '
                    'There is little information to display as they have not been recorded joining the server before'
                )

                infractions = mclient.bowser.puns.find({'user': user.id}).count()
                if infractions:
                    desc += f'\n\nUser has {infractions} infraction entr{"y" if infractions == 1 else "ies"}, use `{ctx.prefix}history {user.id}` to view'

                embed = discord.Embed(color=discord.Color(0x18EE1C), description=desc)
                embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.display_avatar.url)
                embed.set_thumbnail(url=user.display_avatar.url)
                embed.add_field(name='Created', value=f'<t:{int(user.created_at.timestamp())}:f>')

                return await ctx.send(embed=embed)  # TODO: Return DB info if it exists as well

        else:
            dbUser = mclient.bowser.users.find_one({'_id': user.id})

        # Member object, loads of info to work with
        messages = mclient.bowser.messages.find({'author': user.id})
        msgCount = 0 if not messages else messages.count()

        desc = (
            f'Fetched user {user.mention}.'
            if inServer
            else (
                f'Fetched information about previous member {user.mention} '
                'from the API because they are not in this server. '
                'Showing last known data from before they left'
            )
        )

        embed = discord.Embed(color=discord.Color(0x18EE1C), description=desc)
        embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name='Messages', value=str(msgCount), inline=True)
        if inServer:
            embed.add_field(name='Join date', value=f'<t:{int(user.joined_at.timestamp())}:f>', inline=True)
        roleList = []
        if inServer:
            for role in reversed(user.roles):
                if role.id == user.guild.id:
                    continue

                roleList.append(role.mention)

        else:
            roleList = dbUser['roles']

        if not roleList:
            # Empty; no roles
            roles = '*User has no roles*'

        else:
            if not inServer:
                tempList = []
                for x in reversed(roleList):
                    y = ctx.guild.get_role(x)
                    name = '*deleted role*' if not y else y.mention
                    tempList.append(name)

                roleList = tempList

            roles = ', '.join(roleList)

        embed.add_field(name='Roles', value=roles, inline=False)

        lastMsg = (
            'N/a' if msgCount == 0 else f'<t:{int(messages.sort("timestamp", pymongo.DESCENDING)[0]["timestamp"])}:f>'
        )
        embed.add_field(name='Last message', value=lastMsg, inline=True)
        embed.add_field(name='Created', value=f'<t:{int(user.created_at.timestamp())}:f>', inline=True)

        noteDocs = mclient.bowser.puns.find({'user': user.id, 'type': 'note'})
        fieldValue = 'View history to get full details on all notes\n\n'
        if noteDocs.count():
            noteCnt = noteDocs.count()
            noteList = []
            for x in noteDocs.sort('timestamp', pymongo.DESCENDING):
                stamp = f'[<t:{int(x["timestamp"])}:d>]'
                noteContent = f'{stamp}: {x["reason"]}'

                fieldLength = 0
                for value in noteList:
                    fieldLength += len(value)
                if len(noteContent) + fieldLength > 924:
                    fieldValue = f'Only showing {len(noteList)}/{noteCnt} notes. ' + fieldValue
                    break

                noteList.append(noteContent)

            embed.add_field(name='User notes', value=fieldValue + '\n'.join(noteList), inline=False)

        punishments = ''
        punsCol = mclient.bowser.puns.find({'user': user.id, 'type': {'$ne': 'note'}})
        if not punsCol.count():
            punishments = '__*No punishments on record*__'

        else:
            puns = 0
            activeStrikes = 0
            totalStrikes = 0
            activeMute = None
            for pun in punsCol.sort('timestamp', pymongo.DESCENDING):
                if pun['type'] == 'strike':
                    totalStrikes += pun['strike_count']
                    activeStrikes += pun['active_strike_count']

                elif pun['type'] == 'destrike':
                    totalStrikes -= pun['strike_count']

                elif pun['type'] == 'mute':
                    if pun['active']:
                        activeMute = pun['expiry']

                if puns >= 5:
                    continue

                puns += 1
                stamp = f'<t:{int(pun["timestamp"])}:f>'
                punType = config.punStrs[pun['type']]
                if pun['type'] in ['clear', 'unmute', 'unban', 'unblacklist', 'destrike']:
                    if pun['type'] == 'destrike':
                        punType = f'Removed {pun["strike_count"]} Strike{"s" if pun["strike_count"] > 1 else ""}'

                    punishments += f'> {config.removeTick} {stamp} **{punType}**\n'

                else:
                    if pun['type'] == 'strike':
                        punType = f'{pun["strike_count"]} Strike{"s" if pun["strike_count"] > 1 else ""}'

                    punishments += f'> {config.addTick} {stamp} **{punType}**\n'

            punishments = (
                f'Showing {puns}/{punsCol.count()} punishment entries. '
                f'For a full history including responsible moderator, active status, and more use `{ctx.prefix}history {user.id}`'
                f'\n\n{punishments}'
            )

            if activeMute:
                embed.description += f'\n**User is currently muted until <t:{activeMute}:f>**'

            if totalStrikes:
                embed.description += f'\nUser currently has {activeStrikes} active strike{"s" if activeStrikes != 1 else ""} ({totalStrikes} in total)'

        embed.add_field(name='Punishments', value=punishments, inline=False)
        return await ctx.send(embed=embed)

    @commands.command(name='history')
    async def _history(self, ctx, user: typing.Union[discord.User, int, None] = None):
        if user is None:
            user = ctx.author

        if type(user) == int:
            # User doesn't share the ctx server, fetch it instead
            try:
                user = await self.bot.fetch_user(user)

            except discord.NotFound:
                return await ctx.send(f'{config.redTick} User does not exist')

        if (
            ctx.guild.get_role(config.moderator) not in ctx.author.roles
            and ctx.guild.get_role(config.eh) not in ctx.author.roles
        ):
            self_check = True

            #  If they are not mod and not running on themselves, they do not have permssion.
            if user != ctx.author:
                await ctx.message.delete()
                return await ctx.send(
                    f'{config.redTick} You do not have permission to run this command on other users', delete_after=15
                )

            if ctx.channel.id != config.commandsChannel:
                await ctx.message.delete()
                return await ctx.send(
                    f'{config.redTick} {ctx.author.mention} Please use bot commands in <#{config.commandsChannel}>, not {ctx.channel.mention}',
                    delete_after=15,
                )

        else:
            self_check = False

        db = mclient.bowser.puns
        puns = db.find({'user': user.id, 'type': {'$ne': 'note'}}) if self_check else db.find({'user': user.id})

        deictic_language = {
            'no_punishments': ('User has no punishments on record.', 'You have no available punishments on record.'),
            'single_inf': (
                'There is **1** infraction record for this user:',
                'You have **1** available infraction record:',
            ),
            'multiple_infs': (
                'There are **{}** infraction records for this user:',
                'You have **{}** available infraction records:',
            ),
            'total_strikes': (
                'User currently has **{}** active strikes (**{}** in total.)\n',
                'You currently have **{}** active strikes (**{}** in total.)\n',
            ),
        }

        punNames = {
            'strike': '{} Strike{}',
            'destrike': 'Removed {} Strike{}',
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
            'unblacklist': 'Unblacklist ({})',
            'appealdeny': 'Denied ban appeal ({})',
            'note': 'User note',
        }

        if puns.count() == 0:
            desc = deictic_language["no_punishments"][self_check]
        elif puns.count() == 1:
            desc = deictic_language['single_inf'][self_check]
        else:
            desc = deictic_language['multiple_infs'][self_check].format(puns.count())

        fields = []
        activeStrikes = 0
        totalStrikes = 0
        for pun in puns.sort('timestamp', pymongo.DESCENDING):
            datestamp = f'<t:{int(pun["timestamp"])}:f>'
            moderator = ctx.guild.get_member(pun['moderator'])
            if not moderator:
                moderator = await self.bot.fetch_user(pun['moderator'])

            if pun['type'] == 'strike':
                activeStrikes += pun['active_strike_count']
                totalStrikes += pun['strike_count']
                inf = punNames[pun['type']].format(pun['strike_count'], "s" if pun['strike_count'] > 1 else "")

            elif pun['type'] == 'destrike':
                totalStrikes -= pun['strike_count']
                inf = punNames[pun['type']].format(pun['strike_count'], "s" if pun['strike_count'] > 1 else "")

            elif pun['type'] in ['blacklist', 'unblacklist']:
                inf = punNames[pun['type']].format(pun['context'])

            elif pun['type'] == 'appealdeny':
                inf = punNames[pun['type']].format(
                    f'until <t:{int(pun["expiry"])}:D>' if pun["expiry"] else "permanently"
                )

            else:
                inf = punNames[pun['type']]

            value = f'**Moderator:** {moderator}\n**Details:** [{inf}] {pun["reason"]}'

            if len(value) > 1024:  # This shouldn't happen, but it does -- split long values up
                strings = []
                offsets = list(range(0, len(value), 1018))  # 1024 - 6 = 1018

                for i, o in enumerate(offsets):
                    segment = value[o : (o + 1018)]

                    if i == 0:  # First segment
                        segment = f'{segment}...'
                    elif i == len(offsets) - 1:  # Last segment
                        segment = f'...{segment}'
                    else:
                        segment = f'...{segment}...'

                    strings.append(segment)

                for i, string in enumerate(strings):
                    fields.append({'name': f'{datestamp} ({i+1}/{len(strings)})', 'value': string})

            else:
                fields.append({'name': datestamp, 'value': value})

        if totalStrikes:
            desc = deictic_language['total_strikes'][self_check].format(activeStrikes, totalStrikes) + desc

        try:
            channel = ctx.author if self_check else ctx.channel

            if self_check:
                await channel.send(
                    'You requested the following copy of your current infraction history. If you have questions concerning your history,'
                    + f' you may contact the moderation team by sending a DM to our modmail bot, Parakarry (<@{config.parakarry}>)'
                )
                await ctx.message.add_reaction('📬')

            author = {'name': f'{user} | {user.id}', 'icon_url': user.display_avatar.url}
            await tools.send_paginated_embed(
                self.bot, channel, fields, title='Infraction History', description=desc, color=0x18EE1C, author=author
            )

        except discord.Forbidden:
            if self_check:
                await ctx.send(
                    f'{config.redTick} {ctx.author.mention} I was unable to DM you. Please make sure your DMs are open and try again',
                    delete_after=10,
                )
            else:
                raise

    @commands.command(name='echoreply')
    @commands.has_any_role(config.moderator, config.eh)
    async def _reply(self, ctx: commands.Context, message: discord.Message, *, text: str = ""):
        files = []
        for file in ctx.message.attachments:
            data = io.BytesIO()
            await file.save(data)
            files.append(discord.File(data, file.filename))
        await message.reply(text, files=files)

    @commands.command(name='echo')
    @commands.has_any_role(config.moderator, config.eh)
    async def _echo(self, ctx: commands.Context, channel: discord.TextChannel, *, text: str = ""):
        files = []
        for file in ctx.message.attachments:
            data = io.BytesIO()
            await file.save(data)
            files.append(discord.File(data, file.filename))
        await channel.send(text, files=files)

    @commands.command(name='roles')
    @commands.has_any_role(config.moderator, config.eh)
    async def _roles(self, ctx):
        lines = []
        for role in reversed(ctx.guild.roles):
            lines.append(f'{role.name} ({role.id})')

        fields = tools.convert_list_to_fields(lines, codeblock=True)
        return await tools.send_paginated_embed(
            self.bot,
            ctx.channel,
            fields,
            owner=ctx.author,
            title='List of roles in guild:',
            description='',
            page_character_limit=1500,
        )

    @commands.group(name='tag', aliases=['tags'], invoke_without_command=True)
    async def _tag(self, ctx, *, query=None):
        db = mclient.bowser.tags

        if query:
            query = query.lower()
            tag = db.find_one({'_id': query, 'active': True})

            if not tag:
                return await ctx.send(f'{config.redTick} A tag with that name does not exist', delete_after=10)

            await ctx.message.delete()

            embed = discord.Embed(title=tag['_id'], description=tag['content'])
            embed.set_footer(text=f'Requested by {ctx.author}', icon_url=ctx.author.display_avatar.url)

            if 'img_main' in tag and tag['img_main']:
                embed.set_image(url=tag['img_main'])
            if 'img_thumb' in tag and tag['img_thumb']:
                embed.set_thumbnail(url=tag['img_thumb'])

            return await ctx.send(embed=embed)

        else:
            await self._tag_list(ctx)

    @_tag.command(name='list', aliases=['search'])
    async def _tag_list(self, ctx, *, search: typing.Optional[str] = ''):
        db = mclient.bowser.tags

        tagList = []
        for tag in db.find({'active': True}):
            description = '' if not 'desc' in tag else tag['desc']
            tagList.append({'name': tag['_id'].lower(), 'desc': description, 'content': tag['content']})

        tagList.sort(key=lambda x: x['name'])

        if not tagList:
            return await ctx.send('{config.redTick} This server has no tags!')

        # Called from the !tag command instead of !tag list, so we print the simple list
        if ctx.invoked_with.lower() in ['tag', 'tags']:
            tags = ', '.join([tag['name'] for tag in tagList])

            embed = discord.Embed(
                title='Tag List',
                description=(
                    f'Here is a list of tags you can access:\n\n> {tags}\n\nType `{ctx.prefix}tag <name>` to request a tag or `{ctx.prefix}tag list` to view tags with their descriptions'
                ),
            )
            return await ctx.send(embed=embed)

        else:  # Complex list
            # If the command is being not being run in commands channel, they must be a mod or helpful user to run it.
            if ctx.channel.id != config.commandsChannel:
                if not (
                    ctx.guild.get_role(config.moderator) in ctx.author.roles
                    or ctx.guild.get_role(config.helpfulUser) in ctx.author.roles
                    or ctx.guild.get_role(config.trialHelpfulUser) in ctx.author.roles
                ):
                    return await ctx.send(
                        f'{config.redTick} {ctx.author.mention} Please use this command in <#{config.commandsChannel}>, not {ctx.channel.mention}',
                        delete_after=15,
                    )

            if search:
                embed_desc = f'Here is a list of tags you can access matching query `{search}`:\n*(Type `{ctx.prefix}tag <name>` to request a tag)*'
            else:
                embed_desc = f'Here is a list of all tags you can access:\n*(Type `{ctx.prefix}tag <name>` to request a tag or `{ctx.prefix}tag {ctx.invoked_with} <search>` to search tags)*'

            if search:
                search = search.lower()
                searchRanks = [0] * len(tagList)  # Init search rankings to 0

                # Search name first
                for i, name in enumerate([tag['name'] for tag in tagList]):
                    if name.startswith(search):
                        searchRanks[i] = 1000
                    elif search in name:
                        searchRanks[i] = 800

                # Search descriptions and tag bodies next
                for i, tag in enumerate(tagList):
                    # add 15 * number of matches in desc
                    searchRanks[i] += tag['desc'].lower().count(search) * 15
                    # add 1 * number of matches in content
                    searchRanks[i] += tag['content'].lower().count(search) * 1

                sort_joined_list = [(searchRanks[i], tagList[i]) for i in range(0, len(tagList))]
                sort_joined_list.sort(key=lambda e: e[0], reverse=True)  # Sort from highest rank to lowest

                matches = list(filter(lambda x: x[0] > 0, sort_joined_list))  # Filter to those with matches

                tagList = [x[1] for x in matches]  # Resolve back to tags

            if tagList:
                longest_name = len(max([tag['name'] for tag in tagList], key=len))
                lines = []

                for tag in tagList:
                    name = tag['name'].ljust(longest_name)
                    desc = '*No description*' if not tag['desc'] else tag['desc']

                    lines.append(f'`{name}` {desc}')

            else:
                lines = ['*No results found*']

            fields = tools.convert_list_to_fields(lines, codeblock=False)
            return await tools.send_paginated_embed(
                self.bot,
                ctx.channel,
                fields,
                owner=ctx.author,
                title='Tag List',
                description=embed_desc,
                page_character_limit=1500,
            )

    @_tag.command(name='edit')
    @commands.has_any_role(config.moderator, config.helpfulUser, config.trialHelpfulUser)
    async def _tag_create(self, ctx, name, *, content):
        db = mclient.bowser.tags
        name = name.lower()
        tag = db.find_one({'_id': name})
        if name in ['list', 'search', 'edit', 'delete', 'source', 'setdesc', 'setimg']:  # Name blacklist
            return await ctx.send(f'{config.redTick} You cannot use that name for a tag', delete_after=10)

        if tag:
            db.update_one(
                {'_id': tag['_id']},
                {
                    '$push': {'revisions': {str(int(time.time())): {'content': tag['content'], 'user': ctx.author.id}}},
                    '$set': {'content': content, 'active': True},
                },
            )
            msg = f'{config.greenTick} The **{name}** tag has been '
            msg += 'updated' if tag['active'] else 'created'
            await ctx.message.delete()
            return await ctx.send(msg, delete_after=10)

        else:
            db.insert_one({'_id': name, 'content': content, 'revisions': [], 'active': True})
            return await ctx.send(f'{config.greenTick} The **{name}** tag has been created', delete_after=10)

    @_tag.command(name='delete')
    @commands.has_any_role(config.moderator, config.helpfulUser, config.trialHelpfulUser)
    async def _tag_delete(self, ctx, *, name):
        db = mclient.bowser.tags
        name = name.lower()
        tag = db.find_one({'_id': name})
        await ctx.message.delete()
        if tag:

            def confirm_check(reaction, member):
                return member == ctx.author and str(reaction.emoji) in [config.redTick, config.greenTick]

            confirmMsg = await ctx.send(f'This action will delete the tag "{name}", are you sure you want to proceed?')
            await confirmMsg.add_reaction(config.greenTick)
            await confirmMsg.add_reaction(config.redTick)
            try:
                reaction = await self.bot.wait_for('reaction_add', timeout=15, check=confirm_check)
                if str(reaction[0]) != config.greenTick:
                    await confirmMsg.edit(content='Delete canceled')
                    return await confirmMsg.clear_reactions()

            except asyncio.TimeoutError:
                await confirmMsg.edit(content='Reaction timed out. Rerun command to try again')
                return await confirmMsg.clear_reactions()

            else:
                db.update_one({'_id': name}, {'$set': {'active': False}})
                await confirmMsg.edit(content=f'{config.greenTick} The "{name}" tag has been deleted')
                await confirmMsg.clear_reactions()

        else:
            return await ctx.send(f'{config.redTick} The tag "{name}" does not exist')

    @_tag.command(name='setdesc')
    @commands.has_any_role(config.moderator, config.helpfulUser, config.trialHelpfulUser)
    async def _tag_setdesc(self, ctx, name, *, content: typing.Optional[str] = ''):
        db = mclient.bowser.tags
        name = name.lower()
        tag = db.find_one({'_id': name})

        content = ' '.join(content.splitlines())

        if tag:
            db.update_one({'_id': tag['_id']}, {'$set': {'desc': content}})

            status = 'updated' if content else 'cleared'
            await ctx.message.delete()
            return await ctx.send(
                f'{config.greenTick} The **{name}** tag description has been {status}', delete_after=10
            )

        else:
            return await ctx.send(f'{config.redTick} The tag "{name}" does not exist')

    @_tag.command(name='setimg')
    @commands.has_any_role(config.moderator, config.helpfulUser, config.trialHelpfulUser)
    async def _tag_setimg(self, ctx, name, img_type_arg, *, url: typing.Optional[str] = ''):
        db = mclient.bowser.tags
        name = name.lower()
        tag = db.find_one({'_id': name})

        IMG_TYPES = {
            'main': {'key': 'img_main', 'name': 'main'},
            'thumb': {'key': 'img_thumb', 'name': 'thumbnail'},
            'thumbnail': {'key': 'img_thumb', 'name': 'thumbnail'},
        }

        if img_type_arg.lower() in IMG_TYPES:
            img_type = IMG_TYPES[img_type_arg]
        else:
            return await ctx.send(
                f'{config.redTick} An invalid image type, `{img_type_arg}`, was given. Image type must be: {", ". join(IMG_TYPES.keys())}'
            )

        url = ' '.join(url.splitlines())
        match = tools.linkRe.match(url)
        if url and (
            not match or match.span()[0] != 0
        ):  # If url argument does not match or does not begin with a valid url
            return await ctx.send(f'{config.redTick} An invalid url, `{url}`, was given')

        if tag:
            db.update_one({'_id': tag['_id']}, {'$set': {img_type['key']: url}})

            status = 'updated' if url else 'cleared'
            await ctx.message.delete()
            return await ctx.send(
                f'{config.greenTick} The **{name}** tag\'s {img_type["name"]} image has been {status}', delete_after=10
            )
        else:
            return await ctx.send(f'{config.redTick} The tag "{name}" does not exist')

    @_tag.command(name='source')
    @commands.has_any_role(config.moderator, config.helpfulUser, config.trialHelpfulUser)
    async def _tag_source(self, ctx, *, name):
        db = mclient.bowser.tags
        name = name.lower()
        tag = db.find_one({'_id': name})
        await ctx.message.delete()

        if tag:
            embed = discord.Embed(title=f'{name} source', description=f'```md\n{tag["content"]}\n```')

            description = '' if not 'desc' in tag else tag['desc']
            img_main = '' if not 'img_main' in tag else tag['img_main']
            img_thumb = '' if not 'img_thumb' in tag else tag['img_thumb']

            embed.add_field(
                name='Description', value='*No description*' if not description else description, inline=True
            )
            embed.add_field(name='Main Image', value='*No URL set*' if not img_main else img_main, inline=True)
            embed.add_field(name='Thumbnail Image', value='*No URL set*' if not img_thumb else img_thumb, inline=True)

            return await ctx.send(embed=embed)

        else:
            return await ctx.send(f'{config.redTick} The tag "{name}" does not exist')

    @commands.command(name='blacklist')
    @commands.has_any_role(config.moderator, config.eh)
    async def _blacklist_set(
        self,
        ctx,
        member: discord.Member,
        channel: typing.Union[discord.TextChannel, discord.CategoryChannel, str],
        *,
        reason='-No reason specified-',
    ):
        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Blacklist reason is too long, reduce it by at least {len(reason) - 990} characters'
            )
        statusText = ''
        if type(channel) == str:
            # Arg blacklist
            if channel in ['mail', 'modmail']:
                context = 'modmail'
                mention = context
                users = mclient.bowser.users
                dbUser = users.find_one({'_id': member.id})

                if dbUser['modmail']:
                    users.update_one({'_id': member.id}, {'$set': {'modmail': False}})
                    statusText = 'Blacklisted'

                else:
                    users.update_one({'_id': member.id}, {'$set': {'modmail': True}})
                    statusText = 'Unblacklisted'

            elif channel in ['reactions', 'reaction', 'react']:
                context = 'reaction'
                mention = 'reactions'
                reactionsRole = ctx.guild.get_role(config.noReactions)
                if reactionsRole in member.roles:  # Toggle role off
                    await member.remove_roles(reactionsRole)
                    statusText = 'Unblacklisted'

                else:  # Toggle role on
                    await member.add_roles(reactionsRole)
                    statusText = 'Blacklisted'

            elif channel in ['attach', 'attachments', 'embed', 'embeds']:
                context = 'attachment/embed'
                mention = 'attachments/embeds'
                noEmbeds = ctx.guild.get_role(config.noEmbeds)
                if noEmbeds in member.roles:  # Toggle role off
                    await member.remove_roles(noEmbeds)
                    statusText = 'Unblacklisted'

                else:  # Toggle role on
                    await member.add_roles(noEmbeds)
                    statusText = 'Blacklisted'

            else:
                return await ctx.send(f'{config.redTick} You cannot blacklist a user from that function')

        elif channel.id == config.suggestions:
            context = channel.name
            mention = channel.mention + ' channel'
            suggestionsRole = ctx.guild.get_role(config.noSuggestions)
            if suggestionsRole in member.roles:  # Toggle role off
                await member.remove_roles(suggestionsRole)
                statusText = 'Unblacklisted'

            else:  # Toggle role on
                await member.add_roles(suggestionsRole)
                statusText = 'Blacklisted'

        elif channel.id == config.spoilers:
            context = channel.name
            mention = channel.mention + ' channel'
            spoilersRole = ctx.guild.get_role(config.noSpoilers)
            if spoilersRole in member.roles:  # Toggle role off
                await member.remove_roles(spoilersRole)
                statusText = 'Unblacklisted'

            else:  # Toggle role on
                await member.add_roles(spoilersRole)
                statusText = 'Blacklisted'

        elif channel.category_id == config.eventCat:
            context = 'events'
            mention = 'event'
            eventsRole = ctx.guild.get_role(config.noEvents)
            if eventsRole in member.roles:  # Toggle role off
                await member.remove_roles(eventsRole)
                statusText = 'Unblacklisted'

            else:  # Toggle role on
                await member.add_roles(eventsRole)
                statusText = 'Blacklisted'

        else:
            return await ctx.send(f'{config.redTick} You cannot blacklist a user from that channel')

        public_notify = False
        try:
            await member.send(tools.format_pundm(statusText.lower()[:-2], reason, ctx.author, mention))

        except (discord.Forbidden, AttributeError):  # User has DMs off, or cannot send to Obj
            public_notify = True

        db = mclient.bowser.puns

        if statusText.lower() == 'blacklisted':
            docID = await tools.issue_pun(
                member.id, ctx.author.id, 'blacklist', reason, context=context, public_notify=public_notify
            )

        else:
            db.find_one_and_update(
                {'user': member.id, 'type': 'blacklist', 'active': True, 'context': context},
                {'$set': {'active': False}},
            )
            docID = await tools.issue_pun(
                member.id,
                ctx.author.id,
                'unblacklist',
                reason,
                active=False,
                context=context,
                public_notify=public_notify,
            )

        await tools.send_modlog(
            self.bot,
            self.modLogs,
            statusText.lower()[:-2],
            docID,
            reason,
            user=member,
            moderator=ctx.author,
            extra_author=context,
            public=True,
        )

        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        await ctx.send(f'{config.greenTick} {member} has been {statusText.lower()} from {mention}')

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if not ctx.command:
            return

        cmd_str = ctx.command.full_parent_name + ' ' + ctx.command.name if ctx.command.parent else ctx.command.name
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(
                f'{config.redTick} Missing one or more required arguments. See `{ctx.prefix}help {cmd_str}`',
                delete_after=15,
            )

        elif isinstance(error, commands.CommandOnCooldown):
            return await ctx.send(
                f'{config.redTick} You are using that command too fast, try again in a few seconds', delete_after=15
            )

        elif isinstance(error, commands.BadArgument):
            return await ctx.send(
                f'{config.redTick} One or more provided arguments are invalid. See `{ctx.prefix}help {cmd_str}`',
                delete_after=15,
            )

        elif isinstance(error, commands.CheckFailure):
            return await ctx.send(f'{config.redTick} You do not have permission to run this command.', delete_after=15)

        else:
            await ctx.send(
                f'{config.redTick} An unknown exception has occured, if this continues to happen contact the developer.',
                delete_after=15,
            )
            raise error


async def setup(bot):
    global serverLogs
    global modLogs

    serverLogs = bot.get_channel(config.logChannel)
    modLogs = bot.get_channel(config.modChannel)

    await bot.add_cog(ChatControl(bot))
    logging.info('[Extension] Utility module loaded')


async def teardown(bot):
    bot.remove_cog('ChatControl')
    logging.info('[Extension] Utility module unloaded')

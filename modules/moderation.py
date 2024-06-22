import asyncio
import copy
import logging
import time
import typing
from datetime import datetime, timezone

import config
import discord
import pymongo
from discord.ext import commands, tasks

import tools


mclient = pymongo.MongoClient(config.mongoURI)


class StrikeRange(commands.Converter):
    async def convert(self, ctx, argument):
        if not argument:
            raise commands.BadArgument

        try:
            arg = int(argument)

        except:
            raise commands.BadArgument

        if not 0 <= arg <= 16:
            raise commands.BadArgument

        return arg


class Moderation(commands.Cog, name='Moderation Commands'):
    def __init__(self, bot):
        self.bot = bot
        self.serverLogs = self.bot.get_channel(config.logChannel)
        self.modLogs = self.bot.get_channel(config.modChannel)
        self.publicModLogs = self.bot.get_channel(config.publicModChannel)
        self.taskHandles = {}
        self.NS = self.bot.get_guild(config.nintendoswitch)
        self.roles = {'mute': self.NS.get_role(config.mute)}

    async def cog_load(self):
        # Publish all unposted/pending public modlogs on cog load
        db = mclient.bowser.puns
        pendingLogs = db.find({'public': True, 'public_log_message': None, 'type': {'$ne': 'note'}})
        for log in pendingLogs:
            await tools.send_public_modlog(self.bot, log['_id'], self.publicModLogs)

        # Run expiration tasks
        userDB = mclient.bowser.users
        pendingPuns = db.find({'active': True, 'type': {'$in': ['strike', 'mute']}})
        twelveHr = 60 * 60 * 12
        trackedStrikes = []  # List of unique users
        logging.info('[Moderation] Starting infraction expiration checks')
        for pun in pendingPuns:
            await asyncio.sleep(0.5)
            if pun['type'] == 'strike':
                if pun['user'] in trackedStrikes:
                    continue  # We don't want to create many tasks when we only remove one
                user = userDB.find_one({'_id': pun['user']})
                trackedStrikes.append(pun['user'])
                if user['strike_check'] > time.time():  # In the future
                    tryTime = (
                        twelveHr
                        if user['strike_check'] - time.time() > twelveHr
                        else user['strike_check'] - time.time()
                    )
                    self.schedule_task(tryTime, pun['_id'], config.nintendoswitch)

                else:  # In the past
                    self.schedule_task(0, pun['_id'], config.nintendoswitch)

            elif pun['type'] == 'mute':
                tryTime = twelveHr if pun['expiry'] - time.time() > twelveHr else pun['expiry'] - time.time()
                self.schedule_task(tryTime, pun['_id'], config.nintendoswitch)

        logging.info('[Moderation] Infraction expiration checks complete')

    async def cog_unload(self):
        for task in self.taskHandles.values():
            task.cancel()

    @commands.command(name='hide', aliases=['unhide'])
    @commands.has_any_role(config.moderator, config.eh)
    @commands.max_concurrency(1, commands.BucketType.guild, wait=True)
    async def _hide_modlog(self, ctx, uuid):
        db = mclient.bowser.puns
        doc = db.find_one({'_id': uuid})

        if not doc:
            return await ctx.send(f'{config.redTick} No punishment with that UUID exists')

        sensitive = True if not doc['sensitive'] else False  # Toggle sensitive value

        if not doc['public_log_message']:
            # Public log has not been posted yet
            db.update_one({'_id': uuid}, {'$set': {'sensitive': sensitive}})
            return await ctx.send(
                f'{config.greenTick} Successfully {"" if sensitive else "un"}marked modlog as sensitive'
            )

        else:
            # public_mod_log has a set value, meaning the log has been posted. We need to edit both msg and db now
            try:
                channel = self.bot.get_channel(doc['public_log_channel'])
                message = await channel.fetch_message(doc['public_log_message'])

                if not channel:
                    raise ValueError

            except (ValueError, discord.NotFound, discord.Forbidden):
                return await ctx.send(
                    f'{config.redTick} There was an issue toggling that log\'s sensitive status; the message may have been deleted or I do not have permission to view the channel'
                )

            embed = message.embeds[0]
            embedDict = embed.to_dict()
            newEmbedDict = copy.deepcopy(embedDict)
            listIndex = 0
            for field in embedDict['fields']:
                # We are working with the dict because some logs can have `reason` at different indexes and we should not assume index position
                if (
                    field['name'] == 'Reason'
                ):  # This is subject to a breaking change if `name` updated, but I'll take the risk
                    if sensitive:
                        newEmbedDict['fields'][listIndex][
                            'value'
                        ] = 'This action\'s reason has been marked sensitive by the moderation team and is hidden. See <#671003325495509012> for more information on why logs are marked sensitive'

                    else:
                        newEmbedDict['fields'][listIndex]['value'] = doc['reason']

                    break

                listIndex += 1

            assert (
                embedDict['fields'] != newEmbedDict['fields']
            )  # Will fail if message was unchanged, this is likely because of a breaking change upstream in the pun flow
            db.update_one({'_id': uuid}, {'$set': {'sensitive': sensitive}})
            newEmbed = discord.Embed.from_dict(newEmbedDict)
            await message.edit(embed=newEmbed)

        await ctx.send(f'{config.greenTick} Successfully toggled the sensitive status for that infraction')

    @commands.group(name='infraction', aliases=['inf'], invoke_without_command=True)
    @commands.has_any_role(config.moderator, config.eh)
    async def _infraction(self, ctx):
        return await ctx.send_help(self._infraction)

    @_infraction.command(name='reason')
    @commands.has_any_role(config.moderator, config.eh)
    async def _infraction_reason(self, ctx, infraction, *, reason):
        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Mute reason is too long, reduce it by at least {len(reason) - 990} characters'
            )

        await self._infraction_editing(ctx, infraction, reason)

    @_infraction.command(name='duration', aliases=['dur', 'time'])
    @commands.has_any_role(config.moderator, config.eh)
    async def _infraction_duration(self, ctx, infraction, duration, *, reason):
        await self._infraction_editing(ctx, infraction, reason, duration)

    async def _infraction_editing(self, ctx, infraction, reason, duration=None):
        db = mclient.bowser.puns
        doc = db.find_one({'_id': infraction})
        if not doc:
            return await ctx.send(f'{config.redTick} An invalid infraction id was provided')

        if not doc['active'] and duration:
            return await ctx.send(
                f'{config.redTick} That infraction has already expired and the duration cannot be edited'
            )

        if duration and doc['type'] != 'mute':  # TODO: Should we support strikes in the future?
            return ctx.send(f'{config.redTick} Setting durations is not supported for {doc["type"]}')

        user = await self.bot.fetch_user(doc['user'])
        try:
            member = await ctx.guild.fetch_member(doc['user'])

        except:
            member = None

        if duration:
            try:
                _duration = tools.resolve_duration(duration)
                stamp = _duration.timestamp()
                expireStr = f'<t:{int(stamp)}:f> (<t:{int(stamp)}:R>)'
                try:
                    if int(duration):
                        raise TypeError

                except ValueError:
                    pass

            except (KeyError, TypeError):
                return await ctx.send(f'{config.redTick} Invalid duration passed')

            if stamp - time.time() < 60:  # Less than a minute
                return await ctx.send(f'{config.redTick} Cannot set the new duration to be less than one minute')

            twelveHr = 60 * 60 * 12
            tryTime = twelveHr if stamp - time.time() > twelveHr else stamp - time.time()
            self.schedule_task(tryTime, infraction, config.nintendoswitch)

            if member:
                await member.edit(timed_out_until=_duration, reason='Mute duration modified by moderator')

            db.update_one({'_id': infraction}, {'$set': {'expiry': int(stamp)}})
            await tools.send_modlog(
                self.bot,
                self.modLogs,
                'duration-update',
                doc['_id'],
                reason,
                user=user,
                moderator=ctx.author,
                expires=expireStr,
                extra_author=doc['type'].capitalize(),
            )

        else:
            db.update_one({'_id': infraction}, {'$set': {'reason': reason}})
            await tools.send_modlog(
                self.bot,
                self.modLogs,
                'reason-update',
                doc['_id'],
                reason,
                user=user,
                moderator=ctx.author,
                extra_author=doc['type'].capitalize(),
                updated=doc['reason'],
            )

        if doc['public_log_message']:
            # This could be None if the edit was done before the log post duration has passed
            try:
                pubChannel = self.bot.get_channel(doc['public_log_channel'])
                pubMessage = await pubChannel.fetch_message(doc['public_log_message'])
                embed = pubMessage.embeds[0]
                embedDict = embed.to_dict()
                newEmbedDict = copy.deepcopy(embedDict)
                listIndex = 0
                for field in embedDict['fields']:
                    # We are working with the dict because some logs can have `reason` at different indexes and we should not assume index position
                    if duration and field['name'] == 'Expires':
                        # This is subject to a breaking change if `name` updated, but I'll take the risk
                        newEmbedDict['fields'][listIndex]['value'] = expireStr
                        break

                    elif not duration and field['name'] == 'Reason':
                        newEmbedDict['fields'][listIndex]['value'] = reason
                        break

                    listIndex += 1

                assert (
                    embedDict['fields'] != newEmbedDict['fields']
                )  # Will fail if message was unchanged, this is likely because of a breaking change upstream in the pun flow
                newEmbed = discord.Embed.from_dict(newEmbedDict)
                await pubMessage.edit(embed=newEmbed)

            except Exception as e:
                logging.error(f'[Moderation] _infraction_duration: {e}')

        error = ''
        try:
            member = await ctx.guild.fetch_member(doc['user'])
            if duration:
                await member.send(tools.format_pundm('duration-update', reason, details=(doc['type'], expireStr)))

            else:
                await member.send(
                    tools.format_pundm(
                        'reason-update',
                        reason,
                        details=(
                            doc['type'],
                            f'<t:{int(doc["timestamp"])}:f>',
                        ),
                    )
                )

        except (discord.NotFound, discord.Forbidden, AttributeError):
            error = '. I was not able to DM them about this action'

        await ctx.send(
            f'{config.greenTick} The {doc["type"]} {"duration" if duration else "reason"} has been successfully updated for {user} ({user.id}){error}'
        )

    @commands.is_owner()
    @_infraction.command('remove')
    async def _inf_revoke(self, ctx, _id):
        db = mclient.bowser.puns
        doc = db.find_one_and_delete({'_id': _id})
        if not doc:  # Delete did nothing if doc is None
            return ctx.send(f'{config.redTick} No matching infraction found')

        await ctx.send(f'{config.greenTick} removed {_id}: {doc["type"]} against {doc["user"]} by {doc["moderator"]}')

    @commands.command(name='ban', aliases=['banid', 'forceban'])
    @commands.has_any_role(config.moderator, config.eh)
    @commands.max_concurrency(1, commands.BucketType.guild, wait=True)
    async def _banning(self, ctx, users: commands.Greedy[tools.ResolveUser], *, reason='-No reason specified-'):
        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Ban reason is too long, reduce it by at least {len(reason) - 990} characters'
            )
        if not users:
            return await ctx.send(f'{config.redTick} An invalid user was provided')

        banCount = 0
        failedBans = 0
        couldNotDM = False

        for user in users:
            userid = user if (type(user) is int) else user.id
            username = userid if (type(user) is int) else f'{str(user)}'

            # If not a user, manually contruct a user object
            user = discord.Object(id=userid) if (type(user) is int) else user

            try:
                member = await ctx.guild.fetch_member(userid)
                usr_role_pos = member.top_role.position
            except:
                usr_role_pos = -1

            if (usr_role_pos >= ctx.guild.me.top_role.position) or (usr_role_pos >= ctx.author.top_role.position):
                if len(users) == 1:
                    return await ctx.send(f'{config.redTick} Insufficent permissions to ban {username}')
                else:
                    failedBans += 1
                    continue

            try:
                await ctx.guild.fetch_ban(user)
                if len(users) == 1:
                    if ctx.author.id == self.bot.user.id:  # Non-command invoke, such as automod
                        # We could do custom exception types, but the whole "automod context" is already a hack anyway.
                        raise ValueError
                    else:
                        return await ctx.send(f'{config.redTick} {username} is already banned')

                else:
                    # If a many-user ban, don't exit if a user is already banned
                    failedBans += 1
                    continue

            except discord.NotFound:
                pass

            try:
                await user.send(tools.format_pundm('ban', reason, ctx.author, auto=ctx.author.id == self.bot.user.id))

            except (discord.Forbidden, AttributeError):
                couldNotDM = True
                pass

            try:
                await ctx.guild.ban(user, reason=f'Ban action performed by moderator', delete_message_days=3)

            except discord.NotFound:
                # User does not exist
                if len(users) == 1:
                    return await ctx.send(f'{config.redTick} User {userid} does not exist')

                failedBans += 1
                continue

            docID = await tools.issue_pun(userid, ctx.author.id, 'ban', reason=reason)
            await tools.send_modlog(
                self.bot,
                self.modLogs,
                'ban',
                docID,
                reason,
                username=username,
                userid=userid,
                moderator=ctx.author,
                public=True,
            )
            banCount += 1

        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        if ctx.author.id != self.bot.user.id:  # Command invoke, i.e. anything not automod
            if len(users) == 1:
                resp = f'{config.greenTick} {users[0]} has been successfully banned'
                if couldNotDM:
                    resp += '. I was not able to DM them about this action'

            else:
                resp = f'{config.greenTick} **{banCount}** users have been successfully banned'
                if failedBans:
                    resp += f'. Failed to ban **{failedBans}** from the provided list'

            return await ctx.send(resp)

    @commands.command(name='unban')
    @commands.has_any_role(config.moderator, config.eh)
    @commands.max_concurrency(1, commands.BucketType.guild, wait=True)
    async def _unbanning(self, ctx, user: int, *, reason='-No reason specified-'):
        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Unban reason is too long, reduce it by at least {len(reason) - 990} characters'
            )
        db = mclient.bowser.puns
        userObj = discord.Object(id=user)
        try:
            await ctx.guild.fetch_ban(userObj)

        except discord.NotFound:
            return await ctx.send(f'{config.redTick} {user} is not currently banned')

        openAppeal = mclient.modmail.logs.find_one({'open': True, 'ban_appeal': True, 'recipient.id': str(user)})
        if openAppeal:
            return await ctx.send(
                f'{config.redTick} You cannot use the unban command on {user} while a ban appeal is in-progress. You can accept the appeal in <#{int(openAppeal["channel_id"])}> with `/appeal accept [reason]`'
            )

        db.find_one_and_update({'user': user, 'type': 'ban', 'active': True}, {'$set': {'active': False}})
        docID = await tools.issue_pun(user, ctx.author.id, 'unban', reason, active=False)
        await ctx.guild.unban(userObj, reason='Unban action performed by moderator')
        await tools.send_modlog(
            self.bot,
            self.modLogs,
            'unban',
            docID,
            reason,
            username=str(user),
            userid=user,
            moderator=ctx.author,
            public=True,
        )
        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        await ctx.send(f'{config.greenTick} {user} has been unbanned')

    @commands.command(name='kick')
    @commands.has_any_role(config.moderator, config.eh)
    @commands.max_concurrency(1, commands.BucketType.guild, wait=True)
    async def _kicking(self, ctx, users: commands.Greedy[tools.ResolveUser], *, reason='-No reason specified-'):
        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Kick reason is too long, reduce it by at least {len(reason) - 990} characters'
            )
        if not users:
            return await ctx.send(f'{config.redTick} An invalid user was provided')

        kickCount = 0
        failedKicks = 0
        couldNotDM = False

        for user in users:
            userid = user if (type(user) is int) else user.id
            username = userid if (type(user) is int) else f'{str(user)}'

            user = (
                discord.Object(id=userid) if (type(user) is int) else user
            )  # If not a user, manually contruct a user object

            try:
                member = await ctx.guild.fetch_member(userid)
            except discord.HTTPException:  # Member not in guild
                if len(users) == 1:
                    return await ctx.send(f'{config.redTick} {username} is not the server!')

                else:
                    # If a many-user kick, don't exit if a user is already gone
                    failedKicks += 1
                    continue

            usr_role_pos = member.top_role.position

            if (usr_role_pos >= ctx.guild.me.top_role.position) or (usr_role_pos >= ctx.author.top_role.position):
                if len(users) == 1:
                    return await ctx.send(f'{config.redTick} Insufficent permissions to kick {username}')
                else:
                    failedKicks += 1
                    continue

            try:
                await user.send(tools.format_pundm('kick', reason, ctx.author))
            except (discord.Forbidden, AttributeError):
                couldNotDM = True
                pass

            try:
                await member.kick(reason='Kick action performed by moderator')
            except discord.Forbidden:
                failedKicks += 1
                continue

            docID = await tools.issue_pun(member.id, ctx.author.id, 'kick', reason, active=False)
            await tools.send_modlog(
                self.bot, self.modLogs, 'kick', docID, reason, user=member, moderator=ctx.author, public=True
            )
            kickCount += 1

        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        if ctx.author.id != self.bot.user.id:  # Non-command invoke, such as automod
            if len(users) == 1:
                resp = f'{config.greenTick} {users[0]} has been successfully kicked'
                if couldNotDM:
                    resp += '. I was not able to DM them about this action'

            else:
                resp = f'{config.greenTick} **{kickCount}** users have been successfully kicked'
                if failedKicks:
                    resp += f'. Failed to kick **{failedKicks}** from the provided list'

            return await ctx.send(resp)

    @commands.command(name='mute')
    @commands.has_any_role(config.moderator, config.eh)
    @commands.max_concurrency(1, commands.BucketType.guild, wait=True)
    async def _muting(self, ctx, member: discord.Member, duration, *, reason='-No reason specified-'):
        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Mute reason is too long, reduce it by at least {len(reason) - 990} characters'
            )
        db = mclient.bowser.puns
        if db.find_one({'user': member.id, 'type': 'mute', 'active': True}):
            return await ctx.send(f'{config.redTick} {member} ({member.id}) is already muted')

        try:
            _duration = tools.resolve_duration(duration)
            try:
                if int(duration):
                    raise TypeError

            except ValueError:
                pass

        except (KeyError, TypeError):
            return await ctx.send(f'{config.redTick} Invalid duration passed')

        durDiff = (_duration - datetime.now(tz=timezone.utc)).total_seconds()
        if durDiff - 1 > 60 * 60 * 24 * 28:
            # Discord Timeouts cannot exceed 28 days, so we must check this
            return await ctx.send(f'{config.redTick} Mutes cannot be longer than 28 days')

        try:
            member = await ctx.guild.fetch_member(member.id)
            usr_role_pos = member.top_role.position
        except:
            usr_role_pos = -1

        if (usr_role_pos >= ctx.guild.me.top_role.position) or (usr_role_pos >= ctx.author.top_role.position):
            return await ctx.send(f'{config.redTick} Insufficent permissions to mute {member.name}')

        await member.edit(timed_out_until=_duration, reason='Mute action performed by moderator')

        error = ""
        public_notify = False
        try:
            await member.send(tools.format_pundm('mute', reason, ctx.author, f'<t:{int(_duration.timestamp())}:R>'))

        except (discord.Forbidden, AttributeError):
            error = '. I was not able to DM them about this action'
            public_notify = True

        if not tools.mod_cmd_invoke_delete(ctx.channel):
            await ctx.send(f'{config.greenTick} {member} ({member.id}) has been successfully muted{error}')

        docID = await tools.issue_pun(
            member.id, ctx.author.id, 'mute', reason, int(_duration.timestamp()), public_notify=public_notify
        )
        await tools.send_modlog(
            self.bot,
            self.modLogs,
            'mute',
            docID,
            reason,
            user=member,
            moderator=ctx.author,
            expires=f'<t:{int(_duration.timestamp())}:f> (<t:{int(_duration.timestamp())}:R>)',
            public=True,
        )

        twelveHr = 60 * 60 * 12
        expireTime = time.mktime(_duration.timetuple())
        tryTime = twelveHr if expireTime - time.time() > twelveHr else expireTime - time.time()
        self.schedule_task(tryTime, docID, ctx.guild.id)

        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

    @commands.command(name='unmute')
    @commands.has_any_role(config.moderator, config.eh)
    @commands.max_concurrency(1, commands.BucketType.guild, wait=True)
    async def _unmuting(
        self, ctx, member: discord.Member, *, reason='-No reason specified-'
    ):  # TODO: Allow IDs to be unmuted (in the case of not being in the guild)
        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Unmute reason is too long, reduce it by at least {len(reason) - 990} characters'
            )
        db = mclient.bowser.puns
        action = db.find_one_and_update(
            {'user': member.id, 'type': 'mute', 'active': True}, {'$set': {'active': False}}
        )
        if not action:
            return await ctx.send(
                f'{config.redTick} Cannot unmute {member} ({member.id}), they are not currently muted'
            )

        await member.edit(timed_out_until=None, reason='Unmute action performed by moderator')

        error = ""
        public_notify = False
        try:
            await member.send(tools.format_pundm('unmute', reason, ctx.author))

        except (discord.Forbidden, AttributeError):
            error = '. I was not able to DM them about this action'
            public_notify = True

        if not tools.mod_cmd_invoke_delete(ctx.channel):
            await ctx.send(f'{config.greenTick} {member} ({member.id}) has been successfully unmuted{error}')

        docID = await tools.issue_pun(
            member.id, ctx.author.id, 'unmute', reason, context=action['_id'], active=False, public_notify=public_notify
        )
        await tools.send_modlog(
            self.bot,
            self.modLogs,
            'unmute',
            docID,
            reason,
            user=member,
            moderator=ctx.author,
            public=True,
        )

        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

    @commands.has_any_role(config.moderator, config.eh)
    @commands.command(name='note')
    async def _note(self, ctx, user: tools.ResolveUser, *, content):
        userid = user if (type(user) is int) else user.id

        if len(content) > 900:
            return await ctx.send(
                f'{config.redTick} Note is too long, reduce it by at least {len(content) - 990} characters'
            )

        await tools.issue_pun(userid, ctx.author.id, 'note', content, active=False, public=False)
        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

        return await ctx.send(f'{config.greenTick} Note successfully added to {user} ({user.id})')

    @commands.has_any_role(config.moderator, config.eh)
    @commands.group(name='strike', invoke_without_command=True)
    async def _strike(self, ctx, user: tools.ResolveUser, count: typing.Optional[StrikeRange] = 1, *, reason):
        if count == 0:
            return await ctx.send(
                f'{config.redTick} You cannot issue less than one strike. If you need to reset this user\'s strikes to zero instead use `{ctx.prefix}strike set`'
            )

        if len(reason) > 990:
            return await ctx.send(
                f'{config.redTick} Strike reason is too long, reduce it by at least {len(reason) - 990} characters'
            )
        punDB = mclient.bowser.puns
        userDB = mclient.bowser.users
        userDoc = userDB.find_one({'_id': user.id})
        if not userDoc:
            return await ctx.send(f'{config.redTick} Unable strike user who has never joined the server')

        activeStrikes = 0
        for pun in punDB.find({'user': user.id, 'type': 'strike', 'active': True}):
            activeStrikes += pun['active_strike_count']

        activeStrikes += count
        if activeStrikes > 16:  # Max of 16 active strikes
            return await ctx.send(
                f'{config.redTick} Striking {count} time{"s" if count > 1 else ""} would exceed the maximum of 16 strikes. The amount being issued must be lowered by at least {activeStrikes - 16} or consider banning the user instead'
            )

        error = ""
        public_notify = False
        try:
            await user.send(tools.format_pundm('strike', reason, ctx.author, details=count))

        except discord.Forbidden:
            error = '. I was not able to DM them about this action'
            public_notify = True

        if activeStrikes == 16:
            error += '.\n:exclamation: You may want to consider a ban'

        if not tools.mod_cmd_invoke_delete(ctx.channel):
            await ctx.send(
                f'{config.greenTick} {user} ({user.id}) has been successfully struck, they now have '
                f'{activeStrikes} strike{"s" if activeStrikes > 1 else ""} ({activeStrikes-count} + {count}){error}'
            )

        docID = await tools.issue_pun(
            user.id, ctx.author.id, 'strike', reason, strike_count=count, public=True, public_notify=public_notify
        )

        await tools.send_modlog(
            self.bot,
            self.modLogs,
            'strike',
            docID,
            reason,
            user=user,
            moderator=ctx.author,
            extra_author=count,
            public=True,
        )
        content = (
            f'{config.greenTick} {user} ({user.id}) has been successfully struck, '
            f'they now have {activeStrikes} strike{"s" if activeStrikes > 1 else ""} ({activeStrikes-count} + {count})'
        )

        userDB.update_one({'_id': user.id}, {'$set': {'strike_check': time.time() + (60 * 60 * 24 * 7)}})  # 7 days
        self.schedule_task(60 * 60 * 12, docID, ctx.guild.id)

        if tools.mod_cmd_invoke_delete(ctx.channel):
            return await ctx.message.delete()

    @commands.has_any_role(config.moderator, config.eh)
    @_strike.command(name='set')
    async def _strike_set(self, ctx, user: tools.ResolveUser, count: StrikeRange, *, reason):
        punDB = mclient.bowser.puns
        activeStrikes = 0
        puns = punDB.find({'user': user.id, 'type': 'strike', 'active': True})
        for pun in puns:
            activeStrikes += pun['active_strike_count']

        if activeStrikes == count:
            return await ctx.send(f'{config.redTick} That user already has {activeStrikes} active strikes')

        elif (
            count > activeStrikes
        ):  # This is going to be a positive diff, lets just do the math and defer work to _strike()
            return await self._strike(ctx, user, count - activeStrikes, reason=reason)

        else:  # Negative diff, we will need to reduce our strikes
            removedStrikes = activeStrikes - count
            diff = removedStrikes  # accumlator

            puns = punDB.find({'user': user.id, 'type': 'strike', 'active': True}).sort('timestamp', 1)
            for pun in puns:
                if pun['active_strike_count'] - diff >= 0:
                    userDB = mclient.bowser.users
                    userDoc = userDB.find_one({'_id': user.id})
                    if not userDoc:
                        return await ctx.send(f'{config.redTick} Unable strike user who has never joined the server')

                    punDB.update_one(
                        {'_id': pun['_id']},
                        {
                            '$set': {
                                'active_strike_count': pun['active_strike_count'] - diff,
                                'active': pun['active_strike_count'] - diff > 0,
                            }
                        },
                    )
                    userDB.update_one({'_id': user.id}, {'$set': {'strike_check': time.time() + (60 * 60 * 24 * 7)}})
                    self.schedule_task(60 * 60 * 12, pun['_id'], ctx.guild.id)

                    # Logic to calculate the remaining (diff) strikes will simplify to 0
                    # new_diff = diff - removed_strikes
                    #          = diff - (old_strike_amount - new_strike_amount)
                    #          = diff - (old_strike_amount - (old_strike_amount - diff))
                    #          = diff - old_strike_amount + old_strike_amount - diff
                    #          = 0
                    diff = 0
                    break

                elif pun['active_strike_count'] - diff < 0:
                    punDB.update_one({'_id': pun['_id']}, {'$set': {'active_strike_count': 0, 'active': False}})
                    diff -= pun['active_strike_count']

            if diff != 0:  # Something has gone horribly wrong
                raise ValueError('Diff != 0 after full iteration')

            error = ""
            public_notify = False
            try:
                await user.send(tools.format_pundm('destrike', reason, ctx.author, details=removedStrikes))
            except discord.Forbidden:
                error = 'I was not able to DM them about this action'
                public_notify = True

            docID = await tools.issue_pun(
                user.id,
                ctx.author.id,
                'destrike',
                reason=reason,
                active=False,
                strike_count=removedStrikes,
                public_notify=public_notify,
            )
            await tools.send_modlog(
                self.bot,
                self.modLogs,
                'destrike',
                docID,
                reason,
                user=user,
                moderator=ctx.author,
                extra_author=(removedStrikes),
                public=True,
            )

            if not tools.mod_cmd_invoke_delete(ctx.channel):
                await ctx.send(
                    f'{config.greenTick} {user} ({user.id}) has had {removedStrikes} strikes removed, '
                    f'they now have {count} strike{"s" if count > 1 else ""} '
                    f'({activeStrikes} - {removedStrikes}) {error}'
                )

            else:
                return await ctx.message.delete()

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if not ctx.command:
            return

        cmd_str = ctx.command.full_parent_name + ' ' + ctx.command.name if ctx.command.parent else ctx.command.name
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(
                f'{config.redTick} Missing one or more required arguments. See `{ctx.prefix}help {cmd_str}`',
                delete_after=15,
            )

        elif isinstance(error, commands.BadArgument):
            return await ctx.send(
                f'{config.redTick} One or more provided arguments are invalid. See `{ctx.prefix}help {cmd_str}`',
                delete_after=15,
            )

        elif isinstance(error, commands.CheckFailure):
            return await ctx.send(f'{config.redTick} You do not have permission to run this command', delete_after=15)

        else:
            await ctx.send(
                f'{config.redTick} An unknown exception has occured, if this continues to happen contact the developer.',
                delete_after=15,
            )
            raise error

    def schedule_task(self, tryTime: int, _id: str, guild_id: int):
        if _id in self.taskHandles.keys():
            self.taskHandles[_id].cancel()

        self.taskHandles[_id] = self.bot.loop.call_later(
            tryTime, asyncio.create_task, self.expire_actions(_id, guild_id)
        )

    async def expire_actions(self, _id, guild):
        await asyncio.sleep(0.5)
        db = mclient.bowser.puns
        doc = db.find_one({'_id': _id})
        if not doc:
            logging.error(f'[Moderation] Expiry failed. Doc {_id} does not exist!')
            return

        # Lets do a sanity check.
        if not doc['active']:
            logging.debug(f'[Moderation] Expiry failed. Doc {_id} is not active but was scheduled to expire!')
            return

        twelveHr = 60 * 60 * 12
        if doc['type'] == 'strike':
            userDB = mclient.bowser.users
            user = userDB.find_one({'_id': doc['user']})
            try:
                if user['strike_check'] > time.time():
                    # To prevent drift we recall every 12 hours. Schedule for 12hr or expiry time, whichever is sooner
                    retryTime = (
                        twelveHr
                        if user['strike_check'] - time.time() > twelveHr
                        else user['strike_check'] - time.time()
                    )
                    self.schedule_task(retryTime, _id, guild)
                    return

            except (
                KeyError
            ):  # This is a rare edge case, but if a pun is manually created the user may not have the flag yet. More a dev handler than not
                logging.error(
                    f'[Moderation] Expiry failed. Could not get strike_check from db.users resolving for pun {_id}, was it manually added?'
                )

            # Start logic
            if doc['active_strike_count'] - 1 == 0:
                db.update_one({'_id': doc['_id']}, {'$set': {'active': False}, '$inc': {'active_strike_count': -1}})
                strikes = [
                    x for x in db.find({'user': doc['user'], 'type': 'strike', 'active': True}).sort('timestamp', 1)
                ]
                if not strikes:  # Last active strike expired, no additional
                    del self.taskHandles[_id]
                    return

                self.schedule_task(60 * 60 * 12, strikes[0]['_id'], guild)

            elif doc['active_strike_count'] > 0:
                db.update_one({'_id': doc['_id']}, {'$inc': {'active_strike_count': -1}})
                self.schedule_task(60 * 60 * 12, doc['_id'], guild)

            else:
                logging.warning(
                    f'[Moderation] Expiry failed. Doc {_id} had a negative active strike count and was skipped'
                )
                del self.taskHandles[_id]
                return

            userDB.update_one({'_id': doc['user']}, {'$set': {'strike_check': time.time() + 60 * 60 * 24 * 7}})

        elif doc['type'] == 'mute' and doc['expiry']:  # A mute that has an expiry
            # To prevent drift we recall every 12 hours. Schedule for 12hr or expiry time, whichever is sooner
            # This could also fail if the expiry time is changed by a mod
            if doc['expiry'] > time.time():
                retryTime = twelveHr if doc['expiry'] - time.time() > twelveHr else doc['expiry'] - time.time()
                self.schedule_task(retryTime, _id, guild)
                return

            punGuild = self.bot.get_guild(guild)
            member = punGuild.get_member(doc['user'])
            if not member:
                logging.debug(f'[Moderation] {doc["user"]} not in guild and has mute to be expired, ignoring')
                return

            public_notify = False
            try:
                await member.send(tools.format_pundm('unmute', 'Mute expired', None, auto=True))

            except discord.Forbidden:  # User has DMs off
                public_notify = True

            newPun = db.find_one_and_update({'_id': doc['_id']}, {'$set': {'active': False}})
            docID = await tools.issue_pun(
                doc['user'],
                self.bot.user.id,
                'unmute',
                'Mute expired',
                active=False,
                context=doc['_id'],
                public_notify=public_notify,
            )

            if not newPun:  # There is near zero reason this would ever hit, but in case...
                logging.error(
                    f'[Moderation] Expiry failed. Database failed to update user on pun expiration of {doc["_id"]}'
                )

            await member.edit(timed_out_until=None, reason='Automatic: Mute has expired')

            del self.taskHandles[_id]
            await tools.send_modlog(
                self.bot,
                self.modLogs,
                'unmute',
                docID,
                'Mute expired',
                user=member,
                moderator=self.bot.user,
                public=True,
            )


async def setup(bot):
    await bot.add_cog(Moderation(bot))
    logging.info('[Extension] Moderation module loaded')


async def teardown(bot):
    await bot.remove_cog('Moderation')
    logging.info('[Extension] Moderation module unloaded')

import discord
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core import bank, commands, Config, checks, errors
from redbot.core.utils.chat_formatting import (
    bold,
    box,
    humanize_number,
    humanize_timedelta,
    pagify,
)
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

import dbl
import time
import math
import aiohttp
import logging
import asyncio
import calendar
from uuid import uuid4
from typing import Mapping
from tabulate import tabulate
from collections import Counter
from datetime import datetime, timedelta

from .utils import check_weekend, download_widget, error_message, guild_only_check, intro_msg


log = logging.getLogger("red.predacogs.DblTools")
_ = Translator("DblTools", __file__)


@cog_i18n(_)
class DblTools(commands.Cog):
    """Tools for Top.gg API."""

    __author__ = "Predä"
    __version__ = "2.1_brandjuh"

    def __init__(self, bot: Red):
        self.bot = bot
        self.dbl = None

        self.config = Config.get_conf(
            self, identifier=51222797489301095423, force_registration=True
        )
        self.config.register_global(
            post_guild_count=False,
            webhook_auth=None,
            webhook_port=None,
            votes_channel=None,
            support_server_role={"guild_id": None, "role_id": None},
            daily_rewards={
                "toggled": False,
                "amount": 100,
                "weekend_bonus_toggled": False,
                "weekend_bonus_amount": 500,
            },
        )
        self.config.register_user(voted=False, next_daily=0)

        self.economy_cog = None
        self.session = aiohttp.ClientSession()
        self._init_task = bot.loop.create_task(self.initialize())
        self._post_stats_task = self.bot.loop.create_task(self.update_stats())

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Thanks Sinbad!"""
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nAuthor: {self.__author__}\nCog Version: {self.__version__}"

    async def initialize(self):
        await self.bot.wait_until_ready()
        key = (await self.bot.get_shared_api_tokens("dbl")).get("api_key")
        config = await self.config.all()
        self.dbl = dbl.DBLClient(
            bot=self.bot,
            token=key,
            session=self.session,
            webhook_port=config["webhook_port"],
            webhook_auth=config["webhook_auth"],
        )

    def cog_unload(self):
        self.bot.loop.create_task(self.session.close())
        if self._init_task:
            self._init_task.cancel()
        if self._post_stats_task:
            self._post_stats_task.cancel()
        payday_command = self.bot.get_command("payday")
        if payday_command:
            self.bot.remove_command(payday_command.name)

    async def cog_before_invoke(self, ctx: commands.Context):
        if ctx.command.name == "payday":
            cog = self.bot.get_cog("Economy")
            if not cog:
                return
            self.economy_cog = cog

    async def update_stats(self):
        await self.bot.wait_until_ready()
        while True:
            if await self.config.post_guild_count():
                try:
                    await self.dbl.post_guild_count()
                    log.info(
                        "Posted server count to Top.gg {} servers.".format(self.dbl.guild_count())
                    )
                except Exception as error:
                    log.exception(
                        "Failed to post server count\n{}: {}".format(type(error).__name__, error)
                    )
            await asyncio.sleep(1800)

    @commands.Cog.listener()
    async def on_red_api_tokens_update(self, service_name: str, api_tokens: Mapping[str, str]):
        if service_name != "dbl":
            return
        try:
            if self.dbl:
                self.dbl.close()
            config = await self.config.all()
            client = dbl.DBLClient(
                bot=self.bot,
                token=api_tokens.get("api_key"),
                session=self.session,
                webhook_port=config["webhook_port"],
                webhook_auth=config["webhook_auth"],
            )
            await client.get_guild_count()
        except (dbl.Unauthorized, dbl.UnauthorizedDetected):
            await client.close()
            return await self.bot.send_to_owners(
                "[DblTools cog]\n"
                + error_message.format(_("A wrong token has been set for dbltools cog.\n\n"))
            )
        except dbl.NotFound:
            await client.close()
            return await self.bot.send_to_owners(
                _(
                    "[DblTools cog]\nThis bot seems doesn't seems be validated on Top.gg. Please try again with a validated bot."
                )
            )
        self.dbl = client

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.bot.wait_until_ready()
        config = await self.config.all()
        if not member.guild.id == config["support_server_role"]["guild_id"]:
            return
        if not config["support_server_role"]["role_id"]:
            return
        try:
            check_vote = await self.dbl.get_user_vote(member.id)
        except (dbl.Unauthorized, dbl.UnauthorizedDetected, dbl.errors.HTTPException) as error:
            log.error("Failed to fetch Top.gg API.", exc_info=error)
            return
        if check_vote:
            try:
                await member.add_roles(
                    member.guild.get_role(config["support_server_role"]["role_id"]),
                    reason=f"Top.gg {self.bot.user.name} upvoter.",
                )
            except discord.Forbidden:
                await self.bot.send_to_owners(
                    _(
                        "It seems that I no longer have permissions to add roles for Top.gg upvoters "
                        "in {} `{}`. Role rewards has been disabled."
                    ).format(member.guild, member.guild.id)
                )
                async with self.config.all() as config:
                    config["support_server_role"]["guild_id"] = None
                    config["support_server_role"]["role_id"] = None

    @commands.Cog.listener()
    async def on_dbl_vote(self, data: dict):
        global_config = await self.config.all()
        if not global_config["daily_rewards"]["toggled"]:
            return
        async with self.config.user_from_id(int(data["user"])).all() as config:
            config["voted"] = True
            config["next_daily"] = int(datetime.timestamp(datetime.now() + timedelta(hours=12)))
        user = self.bot.get_user(int(data["user"]))
        if not user:
            log.error(
                "Received a vote for ID %s, but cannot get this user from bot cache.", data["user"]
            )
            return

        regular_amount = global_config["daily_rewards"]["amount"]
        weekend_amount = global_config["daily_rewards"]["weekend_bonus_amount"]
        weekend = check_weekend() and global_config["daily_rewards"]["weekend_bonus_toggled"]
        credits_name = await bank.get_currency_name()
        try:
            await bank.deposit_credits(
                user, amount=regular_amount + weekend_amount if weekend else regular_amount
            )
        except errors.BalanceTooHigh as exc:
            await bank.set_balance(user, exc.max_balance)
            await user.send(
                embed=discord.Embed(
                    title="Thanks for your upvote!",
                    description=_(
                        "However, you've reached the maximum amount of {currency}! (**{new_balance}**) "
                        "Please spend some more \N{GRIMACING FACE}\n\n"
                        "You currently have {new_balance} {currency}."
                    ).format(currency=credits_name, new_balance=humanize_number(exc.max_balance)),
                )
            )
            return

        pos = await bank.get_leaderboard_position(user)
        maybe_weekend_bonus = (
            _("\nAnd your week-end bonus, +{}!").format(humanize_number(weekend_amount))
            if weekend
            else ""
        )
        em = discord.Embed(
            color=await self.bot.get_embed_color(user),
            title=_("Thanks for your upvote! Here is your daily bonus."),
            description=_(
                " Take some {currency}. Enjoy! (+{amount} {currency}!){weekend}\n\n"
                "You currently have {new_balance} {currency}.\n\n"
            ).format(
                currency=credits_name,
                amount=humanize_number(regular_amount),
                weekend=maybe_weekend_bonus,
                new_balance=humanize_number(await bank.get_balance(user)),
            ),
        )
        em.set_footer(
            text=_("You are currently #{} on the global leaderboard!").format(humanize_number(pos))
        )
        try:
            await user.send(embed=em)
        except discord.Forbidden:
            log.error("Failed to send vote notification to %s.", user.name)

        if global_config["votes_channel"]:
            channel = self.bot.get_channel(global_config["votes_channel"])
            if not channel:
                await self.config.votes_channel.set(None)
                return
            msg = _("{user.mention} `{user.id}` just voted for {bot.mention} on Top.gg!").format(
                user=user, bot=self.bot.user
            )
            await channel.send(msg)

    @commands.Cog.listener()
    async def on_dbl_test(self, data: dict):
        global_config = await self.config.all()
        if global_config["votes_channel"]:
            channel = self.bot.get_channel(global_config["votes_channel"])
            if not channel:
                await self.config.votes_channel.set(None)
                return
            msg = _("Top.gg test vote.")
            await channel.send(msg)

    @commands.group()
    async def dblset(self, ctx: commands.Context):
        """Group commands for settings of DblTools cog."""

    @dblset.command()
    @checks.is_owner()
    async def poststats(self, ctx: commands.Context):
        """Set if you want to send your bot stats (Guilds and shards count) to Top.gg API."""
        toggled = await self.config.post_guild_count()
        await self.config.post_guild_count.set(not toggled)
        msg = (
            _("Stats will now be sent to Top.gg.")
            if not toggled
            else _("Stats will no longer be sent to Top.gg.")
        )
        await ctx.send(msg)

    @dblset.group()
    async def webhook(self, ctx: commands.Context):
        """Webhook server settings."""

    @webhook.command()
    async def token(self, ctx: commands.Context):
        """Generate a token and send it to owner DMs."""
        token = str(uuid4())
        await self.config.webhook_auth.set(token)
        await self.initialize()
        await self.bot.send_to_owners(
            _(
                "Here is the token for your webhook server that you will need to specify on your Top.gg bot page:\n`{}`"
            ).format(token)
        )
        await ctx.tick()

    @webhook.command()
    async def port(self, ctx: commands.Context, port: int = None):
        """
        Set webhook server port. `[p]dblset webhook` token needs to be ran before.

        Use this command without specifying a port to reset it, which will stop the webhook server.
        """
        if await self.config.webhook_auth() is None:
            return await ctx.send(
                _("You need to run `{}dblset webhook token` before.").format(ctx.prefix)
            )
        if (port < 1) or (port > 65535):
            return await ctx.send("Invalid port number. The port must be between 1 and 65535.")
        await self.config.webhook_port.set(port)
        await self.initialize()
        await ctx.send(
            _(
                "Webhook server set to {} port.\nThe server is now running and ready to receive votes."
            ).format(port)
        )

    @webhook.command()
    async def voteschannel(self, ctx: commands.Context, *, channel: discord.TextChannel):
        """Set the channel where you will receive notifications of votes."""
        await self.config.votes_channel.set(channel.id)
        await ctx.send(_("Votes notifications will be sent to `{}`.").format(channel))

    @webhook.command()
    async def setup(self, ctx: commands.Context):
        """Explanantions on how to setup the webhook server."""
        msg = _(
            "Optional: Use `{prefix}dblset webhook voteschannel channel_id_or_mention` command, to set a channel where you will receive notifications of votes "
            "(This can be useful for step 4 of this guide).\n\n"
            "**1.** Use `{prefix}dblset webhook token` command, which will generate a token, that you will need to "
            "provide at this page: <https://top.gg/bot/{botid}/edit>. At the bottom of the page on `API Options` category, then `Webhook` section, and `Authorization` field.\n"
            "**2.** Use `{prefix}dblset webhook port port_here` command, followed by the port of your choice. Don't forget to open it on your host, "
            "on Ubuntu you can use `sudo ufw allow your_port/tcp` command.\n"
            "**3.** On this page again, <https://top.gg/bot/{botid}/edit>, at `URL` field, put the following: `http://host_ip:port/dblwebhook`, and press on `Save` button. "
            "`host_ip` is your vps/host IP address, and `port` is the port you have set before, on `{prefix}dblset webhook port port_here` command.\n"
            "**4.** If you have set a votes notification channel, use the `Test` button, if you receive a `Top.gg test vote.` message, it means that the setup is done."
        ).format(prefix=ctx.prefix, botid=ctx.bot.user.id)
        await ctx.send(msg)

    @dblset.group(aliases=["rolereward"])
    @commands.guild_only()
    async def rolerewards(self, ctx: commands.Context):
        """Settings for role rewards."""

    @rolerewards.command()
    @commands.bot_has_permissions(manage_roles=True)
    async def role(self, ctx: commands.Context, *, role: discord.Role):
        """Set the role that will be added to new users if they have upvoted for your bot."""
        async with self.config.all() as config:
            config["support_server_role"]["guild_id"] = ctx.guild.id
            config["support_server_role"]["role_id"] = role.id
        await ctx.send(_("Role reward has been enabled and set to: `{}`").format(role.name))

    @rolerewards.command()
    async def reset(self, ctx: commands.Context):
        """Reset current role rewards setup."""
        async with self.config.all() as config:
            config["support_server_role"]["guild_id"] = None
            config["support_server_role"]["role_id"] = None
        await ctx.tick()

    @dblset.group(aliases=["dailyreward"])
    async def dailyrewards(self, ctx: commands.Context):
        """Settings for daily rewards."""

    @dailyrewards.command()
    async def toggle(self, ctx: commands.Context):
        """Set wether you want [p]daily command usable or not."""
        toggled = await self.config.daily_rewards.get_raw("toggled")
        await self.config.daily_rewards.set_raw("toggled", value=not toggled)
        msg = _("Daily command enabled.") if not toggled else _("Daily command disabled.")
        await ctx.send(msg)

    @dailyrewards.command()
    async def amount(self, ctx: commands.Context, amount: int = None):
        """Set the amount of currency that users will receive on daily rewards."""
        if not amount:
            return await ctx.send_help()
        if amount >= await bank.get_max_balance():
            return await ctx.send(_("The amount needs to be lower than bank maximum balance."))
        await self.config.daily_rewards.set_raw("amount", value=amount)
        await ctx.send(_("Daily rewards amount set to {}").format(amount))

    @dailyrewards.command()
    async def weekend(self, ctx: commands.Context):
        """Set weekend bonus."""
        toggled = await self.config.daily_rewards.get_raw("weekend_bonus_toggled")
        await self.config.daily_rewards.set_raw("weekend_bonus_toggled", value=not toggled)
        msg = _("Weekend bonus enabled.") if not toggled else _("Weekend bonus disabled.")
        await ctx.send(msg)

    @dailyrewards.command()
    async def weekendamount(self, ctx: commands.Context, amount: int = None):
        """Set the amount of currency that users will receive on week-end bonus."""
        if not amount:
            return await ctx.send_help()
        if amount >= await bank.get_max_balance():
            return await ctx.send(_("The amount needs to be lower than bank maximum balance."))
        await self.config.daily_rewards.set_raw("weekend_bonus_amount", value=amount)
        await ctx.send(_("Weekend bonus amount set to {}").format(amount))

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(1, 2, commands.BucketType.user)
    async def dblinfo(self, ctx: commands.Context, *, bot: discord.User):
        """
        Show information of a chosen bot on Top.gg.

        `bot`: Can be a mention or ID of a bot.
        """
        if bot is None:
            return await ctx.send(_("This is not a valid Discord user."))
        if not bot.bot:
            return await ctx.send(_("This is not a bot user, please try again with a bot."))

        async with ctx.typing():
            try:
                data = await self.dbl.get_bot_info(bot.id)
            except (dbl.Unauthorized, dbl.UnauthorizedDetected):
                return await ctx.send(
                    _("Failed to contact Top.gg API. A wrong token has been set by the bot owner.")
                )
            except dbl.NotFound:
                return await ctx.send(_("That bot isn't validated on Top.gg."))
            except dbl.HTTPException as error:
                log.error("Failed to fetch Top.gg API.", exc_info=error)
                return await ctx.send(_("Failed to contact Top.gg API. Please try again later."))

            cert_emoji = (
                "<:dblCertified:392249976639455232>"
                if self.bot.get_guild(264445053596991498)
                else "\N{WHITE HEAVY CHECK MARK}"
            )
            fields = {
                "description": (
                    bold(_("Description:")) + box("\n{}\n").format(data["shortdesc"])
                    if data["shortdesc"]
                    else ""
                ),
                "tags": (
                    bold(_("Tags:")) + box("\n{}\n\n").format(", ".join(data["tags"]))
                    if data["tags"]
                    else ""
                ),
                "certified": (
                    bold(_("\nCertified!")) + f" {cert_emoji}\n" if data["certifiedBot"] else "\n"
                ),
                "prefixes": (
                    bold(_("Prefix:")) + " {}\n".format(data["prefix"])
                    if data.get("prefix")
                    else ""
                ),
                "library": (
                    bold(_("Library:")) + " {}\n".format(data["lib"]) if data.get("lib") else ""
                ),
                "servers": (
                    bold(_("Server count:"))
                    + " {}\n".format(humanize_number(data["server_count"]))
                    if data.get("server_count")
                    else ""
                ),
                "shards": (
                    bold(_("Shard count:")) + " {}\n".format(humanize_number(data["shard_count"]))
                    if data.get("shard_count")
                    else ""
                ),
                "votes_month": (
                    bold(_("Monthly votes:"))
                    + (" {}\n".format(humanize_number(data.get("monthlyPoints", 0))))
                ),
                "votes_total": (
                    bold(_("Total votes:"))
                    + (" {}\n".format(humanize_number(data.get("points", 0))))
                ),
                "owners": (
                    bold("{}: ").format(_("Owners") if len(data["owners"]) > 1 else _("Owner"))
                    + ", ".join([str((self.bot.get_user(int(u)))) for u in data["owners"]])
                    + "\n"  # Thanks Slime :ablobcatsipsweats:
                ),
                "approval_date": (
                    bold(_("Approval date:")) + " {}\n\n".format(str(data["date"]).split(".")[0])
                ),
                "dbl_page": _("[Top.gg Page]({})").format(f"https://top.gg/bot/{bot.id}"),
                "invitation": (
                    _(" • [Invitation link]({})").format(data["invite"])
                    if data.get("invite")
                    else ""
                ),
                "support_server": (
                    _(" • [Support](https://discord.gg/{})").format(data["support"])
                    if data.get("support")
                    else ""
                ),
                "github": (
                    _(" • [GitHub]({})").format(data["github"]) if data.get("github") else ""
                ),
                "website": (
                    _(" • [Website]({})").format(data["website"]) if data.get("website") else ""
                ),
            }
            description = [field for field in list(fields.values())]
            em = discord.Embed(color=(await ctx.embed_colour()), description="".join(description))
            em.set_author(
                name=_("Top.gg info about {}:").format(data["username"]),
                icon_url="https://cdn.discordapp.com/emojis/393548388664082444.gif",
            )
            em.set_thumbnail(url=bot.avatar_url_as(static_format="png"))
            return await ctx.send(embed=em)

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(1, 1, commands.BucketType.user)
    async def dblwidget(self, ctx: commands.Context, *, bot: discord.User):
        """
        Send the widget of a chosen bot on Top.gg.

        `bot`: Can be a mention or ID of a bot.
        """
        if bot is None:
            return await ctx.send(_("This is not a valid Discord user."))
        if not bot.bot:
            return await ctx.send(_("This is not a bot user, please try again with a bot."))

        async with ctx.typing():
            try:
                await self.dbl.get_guild_count(bot.id)
                url = await self.dbl.get_widget_large(bot.id)
            except (dbl.Unauthorized, dbl.UnauthorizedDetected):
                return await ctx.send(
                    _("Failed to contact Top.gg API. A wrong token has been set by the bot owner.")
                )
            except dbl.NotFound:
                return await ctx.send(_("That bot isn't validated on Top.gg."))
            except dbl.HTTPException as error:
                log.error("Failed to fetch Top.gg API.", exc_info=error)
                return await ctx.send(_("Failed to contact Top.gg API. Please try again later."))
            file = await download_widget(self.session, url)
            em = discord.Embed(
                color=discord.Color.blurple(),
                description=bold(_("[Top.gg Page]({})")).format(f"https://top.gg/bot/{bot.id}"),
            )
            if file:
                filename = f"{bot.id}_topggwidget_{int(time.time())}.png"
                em.set_image(url=f"attachment://{filename}")
                return await ctx.send(file=discord.File(file, filename=filename), embed=em)
            em.set_image(url=url)
            return await ctx.send(embed=em)

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    @commands.cooldown(1, 1, commands.BucketType.user)
    async def listdblvotes(self, ctx: commands.Context):
        """Sends a list of the persons who voted for the bot this month."""
        try:
            data = await self.dbl.get_bot_upvotes()
        except (dbl.Unauthorized, dbl.UnauthorizedDetected):
            return await ctx.send(
                _("Failed to contact Top.gg API. A wrong token has been set by the bot owner.")
            )
        except (dbl.NotFound, dbl.HTTPException) as error:
            log.error("Failed to fetch Top.gg API.", exc_info=error)
            return await ctx.send(_("Failed to contact Top.gg API. Please try again later."))
        if not data:
            return await ctx.send(_("Your bot hasn't received any votes yet."))

        votes_count = Counter()
        for user_data in data:
            votes_count[user_data["id"]] += 1
        votes = []
        for user_id, value in votes_count.most_common():
            user = self.bot.get_user(int(user_id))
            votes.append((user if user else user_id, humanize_number(value)))
        msg = tabulate(votes, tablefmt="orgtbl")
        embeds = []
        pages = 1
        for page in pagify(msg, delims=["\n"], page_length=1300):
            em = discord.Embed(
                color=await ctx.embed_color(),
                title=_("Monthly votes of {}:").format(self.bot.user),
                description=box(page),
            )
            em.set_footer(
                text=_("Page {}/{}").format(
                    humanize_number(pages), humanize_number((math.ceil(len(msg) / 1300)))
                )
            )
            pages += 1
            embeds.append(em)
        if len(embeds) > 1:
            await menu(ctx, embeds, DEFAULT_CONTROLS)
        else:
            await ctx.send(embed=em)

    @commands.command()
    @commands.cooldown(1, 1, commands.BucketType.user)
    async def daily(self, ctx: commands.Context):
        """Claim your daily reward."""
        config = await self.config.all()
        if not config["daily_rewards"]["toggled"]:
            return
        author = ctx.author
        cur_time = int(time.time())
        next_daily = await self.config.user(author).next_daily()
        if cur_time <= next_daily:
            delta = humanize_timedelta(seconds=next_daily - cur_time) or "1 second"
            msg = author.mention + _(
                " You are speeding! Slow down!\nYou have already claim your daily reward!\n"
                "Wait **{}** for the next one."
            ).format(delta)
            if not await ctx.embed_requested():
                await ctx.send(msg)
            else:
                em = discord.Embed(description=msg, color=discord.Color.red())
                await ctx.send(embed=em)
            return
        credits_name = await bank.get_currency_name(ctx.guild)
        weekend = check_weekend() and config["daily_rewards"]["weekend_bonus_toggled"]
        voted = await self.config.user(author).voted()
        maybe_weekend_bonus = ""
        if weekend:
            maybe_weekend_bonus = _(" and the week-end bonus of {} {}").format(
                humanize_number(config["daily_rewards"]["weekend_bonus_amount"]), credits_name
            )
        title = _(
            "**You can upvote {bot_name} every 12 hours to earn {amount} {currency}\n"
            "Click here to vote. Then do {prefix}daily again{weekend}!**"
        ).format(
            bot_name=self.bot.user.name,
            amount=humanize_number(config["daily_rewards"]["amount"]),
            currency=credits_name,
            prefix=ctx.clean_prefix,
            weekend=maybe_weekend_bonus,
        )
        vote_url = f"https://top.gg/bot/{self.bot.user.id}/vote"
        if not await ctx.embed_requested():
            await ctx.send(f"{title}\n\n{vote_url}")
        else:
            em = discord.Embed(color=discord.Color.red(), title=title, url=vote_url)
            await ctx.send(embed=em)

    @guild_only_check()
    @commands.command()
    async def payday(self, ctx: commands.Context):
        """Get some free currency."""
        # From https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/develop/redbot/cogs/economy/economy.py#L347
        author = ctx.author
        guild = ctx.guild

        cur_time = calendar.timegm(ctx.message.created_at.utctimetuple())
        credits_name = await bank.get_currency_name(ctx.guild)
        daily_config = await self.config.all()
        daily_message = "\n"
        if daily_config["daily_rewards"]["toggled"]:
            last_vote = await self.config.user(author).next_daily()
            if last_vote > int(time.time()):
                delta = humanize_timedelta(seconds=last_vote - cur_time) or "1 second"
                daily_message = _("Your daily bonus will be ready in {}.\n\n").format(delta)
            else:
                async with self.config.user(author).all() as config:
                    config["voted"] = False
                    config["next_daily"] = 0
                weekend = (
                    check_weekend() and daily_config["daily_rewards"]["weekend_bonus_toggled"]
                )
                maybe_weekend_bonus = ""
                if weekend:
                    maybe_weekend_bonus = _(" and the week-end bonus of {} {}").format(
                        humanize_number(daily_config["daily_rewards"]["weekend_bonus_amount"]),
                        credits_name,
                    )
                daily_message = _(
                    "Your daily bonus is ready! Type `{prefix}daily` to claim {daily_amount} {currency}{weekend}\n\n"
                ).format(
                    prefix=ctx.clean_prefix,
                    daily_amount=daily_config["daily_rewards"]["amount"],
                    currency=credits_name,
                    weekend=maybe_weekend_bonus,
                )

        if await bank.is_global():  # Role payouts will not be used

            # Gets the latest time the user used the command successfully and adds the global payday time
            next_payday = (
                await self.economy_cog.config.user(author).next_payday()
                + await self.economy_cog.config.PAYDAY_TIME()
            )
            if cur_time >= next_payday:
                try:
                    await bank.deposit_credits(
                        author, await self.economy_cog.config.PAYDAY_CREDITS()
                    )
                except errors.BalanceTooHigh as exc:
                    await bank.set_balance(author, exc.max_balance)
                    await ctx.maybe_send_embed(
                        _(
                            "You've reached the maximum amount of {currency}!"
                            "Please spend some more \N{GRIMACING FACE}\n\n"
                            "You currently have {new_balance} {currency}."
                        ).format(
                            currency=credits_name, new_balance=humanize_number(exc.max_balance)
                        )
                    )
                    return
                # Sets the current time as the latest payday
                await self.economy_cog.config.user(author).next_payday.set(cur_time)

                pos = await bank.get_leaderboard_position(author)
                await ctx.maybe_send_embed(
                    _(
                        "{author.mention} Here, take some {currency}. "
                        "Enjoy! (+{amount} {currency}!)\n\n"
                        "You currently have {new_balance} {currency}.\n{daily_message}"
                        "You are currently #{pos} on the global leaderboard!"
                    ).format(
                        author=author,
                        currency=credits_name,
                        amount=humanize_number(await self.economy_cog.config.PAYDAY_CREDITS()),
                        new_balance=humanize_number(await bank.get_balance(author)),
                        daily_message=daily_message,
                        pos=humanize_number(pos) if pos else pos,
                    )
                )

            else:
                dtime = self.economy_cog.display_time(next_payday - cur_time)
                await ctx.maybe_send_embed(
                    _(
                        "{author.mention} You are speeding! Slow down!\nYour next payday will be ready in **{time}**.\n\n{daily_message}"
                    ).format(author=author, time=dtime, daily_message=daily_message)
                )
        else:

            # Gets the users latest successfully payday and adds the guilds payday time
            next_payday = (
                await self.economy_cog.config.member(author).next_payday()
                + await self.economy_cog.config.guild(guild).PAYDAY_TIME()
            )
            if cur_time >= next_payday:
                credit_amount = await self.economy_cog.config.guild(guild).PAYDAY_CREDITS()
                for role in author.roles:
                    role_credits = await self.economy_cog.config.role(
                        role
                    ).PAYDAY_CREDITS()  # Nice variable name
                    if role_credits > credit_amount:
                        credit_amount = role_credits
                try:
                    await bank.deposit_credits(author, credit_amount)
                except errors.BalanceTooHigh as exc:
                    await bank.set_balance(author, exc.max_balance)
                    await ctx.maybe_send_embed(
                        _(
                            "You've reached the maximum amount of {currency}! "
                            "Please spend some more \N{GRIMACING FACE}\n\n"
                            "You currently have {new_balance} {currency}."
                        ).format(
                            currency=credits_name, new_balance=humanize_number(exc.max_balance)
                        )
                    )
                    return

                # Sets the latest payday time to the current time
                next_payday = cur_time

                await self.economy_cog.config.member(author).next_payday.set(next_payday)
                pos = await bank.get_leaderboard_position(author)
                await ctx.maybe_send_embed(
                    _(
                        "{author.mention} Here, take some {currency}. "
                        "Enjoy! (+{amount} {currency}!)\n\n"
                        "You currently have {new_balance} {currency}.\n{daily_message}"
                        "You are currently #{pos} on the global leaderboard!"
                    ).format(
                        author=author,
                        currency=credits_name,
                        amount=humanize_number(credit_amount),
                        new_balance=humanize_number(await bank.get_balance(author)),
                        daily_message=daily_message,
                        pos=humanize_number(pos) if pos else pos,
                    )
                )
            else:
                dtime = self.economy_cog.display_time(next_payday - cur_time)
                await ctx.maybe_send_embed(
                    _(
                        "{author.mention} You are speeding! Slow down!\nYour next payday will be ready in **{time}**.\n\n{daily_message}"
                    ).format(author=author, time=dtime, daily_message=daily_message)
                )

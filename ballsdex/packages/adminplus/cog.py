import datetime
import logging
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button
from discord.utils import format_dt
from tortoise.exceptions import BaseORMException, DoesNotExist, IntegrityError
from tortoise.expressions import Q
from ballsdex.core.models import PrivacyPolicy
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.models import Player as PlayerModel

from ballsdex.core.models import (
    Ball,
    BallInstance,
    BlacklistedGuild,
    BlacklistedID,
    GuildConfig,
    Player,
    Trade,
    TradeObject,
    balls,
    specials,
)
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.logging import log_action
from ballsdex.core.utils.paginator import FieldPageSource, Pages, TextPageSource
from ballsdex.core.utils.transformers import (
    BallTransform,
    EconomyTransform,
    RegimeTransform,
    SpecialTransform,
)
from ballsdex.packages.countryballs.countryball import CountryBall
from ballsdex.packages.trade.display import TradeViewFormat, fill_trade_embed_fields
from ballsdex.packages.trade.trade_user import TradingUser
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from ballsdex.packages.countryballs.cog import CountryBallsSpawner

log = logging.getLogger("ballsdex.packages.adminplus.cog")
FILENAME_RE = re.compile(r"^(.+)(\.\S+)$")


async def save_file(attachment: discord.Attachment) -> Path:
    path = Path(f"./static/uploads/{attachment.filename}")
    match = FILENAME_RE.match(attachment.filename)
    if not match:
        raise TypeError("The file you uploaded lacks an extension.")
    i = 1
    while path.exists():
        path = Path(f"./static/uploads/{match.group(1)}-{i}{match.group(2)}")
        i = i + 1
    await attachment.save(path)
    return path


@app_commands.guilds(*settings.admin_guild_ids)
@app_commands.default_permissions(administrator=True)
class Adminplus(commands.GroupCog):
    """
    Bot admin (plus) commands.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        if not self.bot.intents.members:
            self.__cog_app_commands_group__.get_command("privacy").parameters[  # type: ignore
                0
            ]._Parameter__parent.choices.pop()  # type: ignore
        self.blacklist.parent = self.__cog_app_commands_group__
        self.balls.parent = self.__cog_app_commands_group__

    blacklist = app_commands.Group(name="blacklist", description="Bot blacklist management")
    blacklist_guild = app_commands.Group(
        name="blacklistguild", description="Guild blacklist management"
    )
    balls = app_commands.Group(
        name=settings.players_group_cog_name, description="Balls management"
    )
    logs = app_commands.Group(name="logs", description="Bot logs management")
    history = app_commands.Group(name="history", description="Trade history management")

    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    @app_commands.choices(
        policy=[
            app_commands.Choice(name="Open Inventory", value=PrivacyPolicy.ALLOW),
            app_commands.Choice(name="Private Inventory", value=PrivacyPolicy.DENY),
            app_commands.Choice(name="Same Server", value=PrivacyPolicy.SAME_SERVER),
        ]
    )
    async def privacy(self, interaction: discord.Interaction, policy: PrivacyPolicy):
        """
        Set the bot's privacy policy.
        """
        if policy == PrivacyPolicy.SAME_SERVER and not self.bot.intents.members:
            await interaction.response.send_message(
                "I need the `members` intent to use this policy.", ephemeral=True
            )
            return
        player, _ = await PlayerModel.get_or_create(discord_id=1237889057330303057)
        player.privacy_policy = policy
        await player.save()
        await interaction.response.send_message(
            f"The bot's privacy policy has been set to **{policy.name}**.", ephemeral=True
        )

    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def special_rarity(self, interaction: discord.Interaction):
        # DO NOT CHANGE THE CREDITS TO THE AUTHOR HERE!
        """
        Show the rarity list of the dex - made by GamingadlerHD
        """
        # Filter enabled collectibles
        events = [x for x in specials.values()]

        if not events:
            await interaction.response.send_message(
                f"There are no events registered in {settings.bot_name} yet.",
                ephemeral=True,
            )
            return

        # Sort collectibles by rarity in ascending order

        entries = []

        for special in events:
            name = f"{special.name}"
            emoji = special.emoji

            if emoji:
                emote = str(emoji)
            else:
                emote = "N/A"

            filters = {}
            filters["special"] = special

            count = await BallInstance.filter(**filters)
            countNum = len(count)
            # sorted_collectibles = sorted(enabled_collectibles.values(), key=lambda x: x.rarity)
            # if you want the Rarity to only show full numbers like 1 or 12 use the code part here:
            # rarity = int(collectible.rarity)
            # otherwise you want to display numbers like 1.5, 5.3, 76.9 use the normal part.

            entry = (name, f"{emote} Count: {countNum}")
            entries.append(entry)
        # This is the number of countryballs who are displayed at one page,
        # you can change this, but keep in mind: discord has an embed size limit.
        per_page = 5

        source = FieldPageSource(entries, per_page=per_page, inline=False, clear_description=False)
        source.embed.description = (
            f"__**{settings.bot_name} events rarity**__"
        )
        source.embed.colour = discord.Colour.blurple()
        source.embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start(
            ephemeral=True,
        )

    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def rarity(self, interaction: discord.Interaction["BallsDexBot"], chunked: bool = True):
        # DO NOT CHANGE THE CREDITS TO THE AUTHOR HERE!
        """
        Show the ACTUAL rarities of the dex - made by GamingadlerHD
        """
        # Filter enabled collectibles
        enabled_collectibles = [x for x in balls.values() if x.enabled]

        if not enabled_collectibles:
            await interaction.response.send_message(
                f"There are no collectibles registered in {settings.bot_name} yet.",
                ephemeral=True,
            )
            return

        # Sort collectibles by rarity in ascending order
        sorted_collectibles = sorted(enabled_collectibles, key=lambda x: x.rarity)

        entries = []

        for collectible in sorted_collectibles:
            name = f"{collectible.country}"
            emoji = self.bot.get_emoji(collectible.emoji_id)

            if emoji:
                emote = str(emoji)
            else:
                emote = "N/A"
            # if you want the Rarity to only show full numbers like 1 or 12 use the code part here:
            # rarity = int(collectible.rarity)
            # otherwise you want to display numbers like 1.5, 5.3, 76.9 use the normal part.
            rarity = collectible.rarity

            entry = (name, f"{emote} Rarity: {rarity}")
            entries.append(entry)
        # This is the number of countryballs who are displayed at one page,
        # you can change this, but keep in mind: discord has an embed size limit.
        per_page = 5

        source = FieldPageSource(entries, per_page=per_page, inline=False, clear_description=False)
        source.embed.description = (
            f"__**{settings.bot_name} rarity**__"
        )
        source.embed.colour = discord.Colour.blurple()
        source.embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start(
            ephemeral=True,
        )

    @balls.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def spawn(
        self,
        interaction: discord.Interaction,
        ball: BallTransform | None = None,
        channel: discord.TextChannel | None = None,
    ):
        """
        Force spawn a random or specified ball.

        Parameters
        ----------
        ball: Ball | None
            The countryball you want to spawn. Random according to rarities if not specified.
        channel: discord.TextChannel | None
            The channel you want to spawn the countryball in. Current channel if not specified.
        """
        # the transformer triggered a response, meaning user tried an incorrect input
        if interaction.response.is_done():
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not ball:
            countryball = await CountryBall.get_random()
        else:
            countryball = CountryBall(ball)
        await countryball.spawn(channel or interaction.channel)  # type: ignore
        await interaction.followup.send(
            f"{settings.collectible_name.title()} spawned.", ephemeral=True
        )
        await log_action(
            f"{interaction.user} spawned {settings.collectible_name} {countryball.name} "
            f"in {channel or interaction.channel}.",
            self.bot,
        )

    @balls.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def give(
        self,
        interaction: discord.Interaction,
        countryball: BallTransform,
        user: discord.User,
        special: SpecialTransform | None = None,
        shiny: bool | None = None,
        health_bonus: int | None = None,
        attack_bonus: int | None = None,
    ):
        """
        Give the specified countryball to a player.

        Parameters
        ----------
        countryball: Ball
        user: discord.User
        special: Special | None
        shiny: bool
            Omit this to make it random.
        health_bonus: int | None
            Omit this to make it random.
        attack_bonus: int | None
            Omit this to make it random.
        """
        # the transformers triggered a response, meaning user tried an incorrect input
        if interaction.response.is_done():
            return
        paintarray = ["Mythical","Gold","Titanium White","Black","Cobalt","Crimson","Forest Green","Saffron","Sky Blue","Pink","Purple","Lime","Orange","Grey","Burnt Sienna"]
        if special != None:
            if str(special) not in paintarray:
                return await interaction.response.send_message("You do not have permission to give this special")
        await interaction.response.defer(ephemeral=True, thinking=True)

        player, created = await Player.get_or_create(discord_id=user.id)
        instance = await BallInstance.create(
            ball=countryball,
            player=player,
            shiny=(shiny if shiny is not None else random.randint(1, 2048) == 1),
            attack_bonus=(
                attack_bonus
                if attack_bonus is not None
                else random.randint(-20, 20)
            ),
            health_bonus=(
                health_bonus
                if health_bonus is not None
                else random.randint(-20, 20)
            ),
            special=special,
        )
        await interaction.followup.send(
            f"`{countryball.country}` {settings.collectible_name} was successfully given to "
            f"`{user}`.\nSpecial: `{special.name if special else None}` • ATK: "
            f"`{instance.attack_bonus:+d}` • HP:`{instance.health_bonus:+d}` "
            f"• Shiny: `{instance.shiny}`"
        )
        await log_action(
            f"{interaction.user} gave {settings.collectible_name} "
            f"{countryball.country} to {user}. (Special={special.name if special else None} "
            f"ATK={instance.attack_bonus:+d} HP={instance.health_bonus:+d} "
            f"shiny={instance.shiny}).",
            self.bot,
        )

    @balls.command(name="count")
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def balls_count(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        ball: BallTransform | None = None,
        shiny: bool | None = None,
        special: SpecialTransform | None = None,
    ):
        """
        Count the number of balls that a player has or how many exist in total.

        Parameters
        ----------
        user: discord.User
            The user you want to count the balls of.
        ball: Ball
        shiny: bool
        special: Special
        """
        if interaction.response.is_done():
            return
        filters = {}
        if ball:
            filters["ball"] = ball
        if shiny is not None:
            filters["shiny"] = shiny
        if special:
            filters["special"] = special
        if user:
            filters["player__discord_id"] = user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        balls = await BallInstance.filter(**filters).count()
        country = f"{ball.country} " if ball else ""
        plural = "s" if balls > 1 or balls == 0 else ""
        special_str = f"{special.name} " if special else ""
        shiny_str = "shiny " if shiny else ""
        if user:
            await interaction.followup.send(
                f"{user} has {balls} {special_str}{shiny_str}"
                f"{country}{settings.collectible_name}{plural}."
            )
        else:
            await interaction.followup.send(
                f"There are {balls} {special_str}{shiny_str}"
                f"{country}{settings.collectible_name}{plural}."
            )

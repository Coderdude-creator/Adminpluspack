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
from ballsdex.core.bot import BallsDexBot
from ballsdex.packages.admin.balls import Balls
adminballs = Balls

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
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from ballsdex.packages.trade.display import TradeViewFormat, fill_trade_embed_fields
from ballsdex.packages.trade.trade_user import TradingUser
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from ballsdex.packages.countryballs.cog import CountryBallsSpawner

log = logging.getLogger("ballsdex.packages.adminplus.cog")
FILENAME_RE = re.compile(r"^(.+)(\.\S+)$")

@app_commands.guilds(*settings.admin_guild_ids)
@app_commands.default_permissions(administrator=True)
class Adminplus(commands.GroupCog):
    """
    Bot admin (plus) commands.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
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
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def completion(
            self,
            interaction: discord.Interaction["BallsDexBot"],
            special: SpecialEnabledTransform | None = None,
    ):
        """
        Show completion of the BallsDex.

        Parameters
        ----------
        special: Special
            The special you want to see the completion of
        """
        user = None
        await interaction.response.defer(thinking=True)
        extra_text = f"{special.name} " if special else ""
        if user is not None:
            try:
                player = await Player.get(discord_id=user_obj.id)
            except DoesNotExist:
                await interaction.followup.send(
                    f"There are no "
                    f"{extra_text}{settings.plural_collectible_name} yet."
                )
                return

            if await inventory_privacy(self.bot, interaction, player) is False:
                return
        # Filter disabled balls, they do not count towards progression
        # Only ID and emoji is interesting for us
        bot_countryballs = {x: y.emoji_id for x, y in balls.items() if y.enabled}

        # Set of ball IDs owned by the player
        filters = {"ball__enabled": True}
        if special:
            filters["special"] = special
            bot_countryballs = {
                x: y.emoji_id
                for x, y in balls.items()
                if y.enabled and y.created_at < special.end_date
            }
        if not bot_countryballs:
            await interaction.followup.send(
                f"There are no {extra_text}{settings.plural_collectible_name}"
                " registered on this bot yet.",
                ephemeral=True,
            )
            return

        owned_countryballs = set(
            x[0]
            for x in await BallInstance.filter(**filters)
            .distinct()  # Do not query everything
            .values_list("ball_id")
        )

        entries: list[tuple[str, str]] = []

        def fill_fields(title: str, emoji_ids: set[int]):
            # check if we need to add "(continued)" to the field name
            first_field_added = False
            buffer = ""

            for emoji_id in emoji_ids:
                emoji = self.bot.get_emoji(emoji_id)
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    # hitting embed limits, adding an intermediate field
                    if first_field_added:
                        entries.append(("\u200B", buffer))
                    else:
                        entries.append((f"__**{title}**__", buffer))
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:  # add what's remaining
                if first_field_added:
                    entries.append(("\u200B", buffer))
                else:
                    entries.append((f"__**{title}**__", buffer))

        if owned_countryballs:
            # Getting the list of emoji IDs from the IDs of the owned countryballs
            fill_fields(
                f"Caught {settings.plural_collectible_name}",
                set(bot_countryballs[x] for x in owned_countryballs),
            )
        else:
            entries.append((f"__**Caught {settings.plural_collectible_name}**__", "Nothing yet."))

        if missing := set(y for x, y in bot_countryballs.items() if x not in owned_countryballs):
            fill_fields(f"Uncaught {settings.plural_collectible_name}", missing)
        else:
            entries.append(
                (
                    f"__**:tada: No missing {settings.plural_collectible_name}! "
                    ":tada:**__",
                    "\u200B",
                )
            )  # force empty field value

        source = FieldPageSource(entries, per_page=5, inline=False, clear_description=False)
        special_str = f" ({special.name})" if special else ""
        source.embed.description = (
            f"Global {settings.bot_name}{special_str} progression: "
            f"**{round(len(owned_countryballs) / len(bot_countryballs) * 100, 1)}%**"
        )
        source.embed.colour = discord.Colour.blurple()
        source.embed.set_author(name=(settings.bot_name), icon_url=self.bot.user.avatar.url)

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start()


    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def special_rarity(self, interaction: discord.Interaction, countryball: BallEnabledTransform | None = None,):
        # DO NOT CHANGE THE CREDITS TO THE AUTHOR HERE!
        """
        Show the special count list of the dex - made by GamingadlerHD
        
        Parameters
        ----------
        countryball: Ball | None
            Choose to filter by a specific countryball.
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
            if countryball:
                filters["ball"] = countryball

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

    @balls.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def spawn(
            self,
            interaction: discord.Interaction[BallsDexBot],
            countryball: BallEnabledTransform | None = None,
            n: app_commands.Range[int, 1, 100] = 1,
            special: SpecialTransform | None = None,
            
    ):
        """
        Force spawn a random or specified countryball.

        Parameters
        ----------
        countryball: Ball | None
            The countryball you want to spawn. Random according to rarities if not specified.
        n: int
            The number of countryballs to spawn. If no countryball was specified, it's random
            every time.
        special: Special | None
            The special you want to spawn. You can only spawn RLDex paints.
        """
        if countryball:
            if countryball.enabled == False:
                return await interaction.response.send_message(f"You do not have permission to spawn this {settings.collectible_name}", ephemeral=True)
        paintarray = ["Gold","Titanium White","Black","Cobalt","Crimson","Forest Green","Saffron","Sky Blue","Pink","Purple","Lime","Orange","Grey","Burnt Sienna"]
        if special:
            if str(special) not in paintarray:
                return await interaction.response.send_message("You do not have permission to spawn this special",ephemeral=True)
        await adminballs().get_command('spawn').callback(
            adminballs(),
            interaction,
            countryball,
            None,
            n,
            special,
            None,
            None,
        )
            

    @balls.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    @app_commands.choices(
        stats=[
            app_commands.Choice(name="Regular stats", value="ONES"),
            app_commands.Choice(name="Tens stats (10,20,30etc)", value="TENS"),
            app_commands.Choice(name="No stats", value="NOSTATS")
        ]
    )
    async def wheel(
        self,
        interaction: discord.Interaction,
        shiny_percentage: app_commands.Range[float, 0, 100] = 0,
        mythical_percentage: app_commands.Range[float, 0, 100] = 0,
        stats: str | None = None,
    ):
        """
        Spin the wheel!.
        """
        await interaction.response.defer(thinking=True)
        cog = cast("CountryBallsSpawner | None", interaction.client.get_cog("CountryBallsSpawner"))
        ball = await cog.countryball_cls.get_random(interaction.client)
        shinyresult = ""
        mythicalresult = ""
        if shiny_percentage != 100:
            mythical_percentage = mythical_percentage * 100/(100-shiny_percentage) # this is required since mythical relies on NOTSHINY to work
        plusatk = ""
        plushp = ""
        shinyrng = random.uniform(0,100)
        mythicalrng = random.uniform(0,100)
        tenthatk = int(settings.max_attack_bonus/10)
        tenthhp = int(settings.max_health_bonus/10)
        if stats == "TENS":
            atkrng = random.randint(-1*tenthatk, tenthatk)*10
            hprng = random.randint(-1*tenthhp, tenthhp)*10
        else:
            atkrng = random.randint(-1*settings.max_attack_bonus, settings.max_attack_bonus)
            hprng = random.randint(-1*settings.max_health_bonus, settings.max_health_bonus)
        if atkrng >= 0:
            plusatk = "+"
        if hprng >= 0:
            plushp = "+"
        if shinyrng < (shiny_percentage):
            shinyresult = f"\n***✨ It's a shiny {settings.collectible_name}! ✨***"
        elif mythicalrng < (mythical_percentage):
            mythicalresult = f"\n*🔮 This {settings.collectible_name} exudes a mythical aura.🔮*"
        if stats != "NOSTATS":
            statsresults = f"\n`{plusatk}{atkrng}ATK/{plushp}{hprng}HP`"
        else:
            statsresults = ""
        await interaction.followup.send(f"# {ball.name}{statsresults}{shinyresult}{mythicalresult}")


    @balls.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def give(
        self,
        interaction: discord.Interaction,
        countryball: BallEnabledTransform,
        user: discord.User,
        special: SpecialTransform | None = None,
        health_bonus: app_commands.Range[int, -1*settings.max_health_bonus, settings.max_health_bonus] = None,
        attack_bonus: app_commands.Range[int, -1*settings.max_attack_bonus, settings.max_attack_bonus] = None,
    ):
        """
        Give the specified countryball to a player.

        Parameters
        ----------
        countryball: Ball
        user: discord.User
        special: Special | None
        health_bonus: int | None
            Omit this to make it random.
        attack_bonus: int | None
            Omit this to make it random.
        """
        # the transformers triggered a response, meaning user tried an incorrect input
        if countryball.enabled == False:
            return await interaction.response.send_message(f"You do not have permission to give this {settings.collectible_name}", ephemeral=True)
        paintarray = ["Gold","Titanium White","Black","Cobalt","Crimson","Forest Green","Saffron","Sky Blue","Pink","Purple","Lime","Orange","Grey","Burnt Sienna"]
        if special:
            if str(special) not in paintarray:
                return await interaction.response.send_message("You do not have permission to give this special",ephemeral=True)
        await adminballs().get_command('give').callback(
            adminballs(),
            interaction,
            countryball,
            user,
            special,
            health_bonus,
            attack_bonus,
        )

    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def count_list(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        special: SpecialTransform | None = None,):
        # DO NOT CHANGE THE CREDITS TO THE AUTHOR HERE!
        """
        Counts every character - made by GamingadlerHD and Mo Official

        Parameters
        ----------
        user: discord.User
            The user you want to count the balls of.
        special: Special
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

        # Sort collectibles by rarity in ascending order

        entries = []
        nothingcheck = ""

        for collectible in sorted_collectibles:
            name = f"{collectible.country}"
            emoji = self.bot.get_emoji(collectible.emoji_id)

            if emoji:
                emote = str(emoji)
            else:
                emote = "N/A"

            filters = {}
            filters["ball"] = collectible
            if special:
                filters["special"] = special
            if user:
                filters["player__discord_id"] = user.id

            count = await BallInstance.filter(**filters)
            countNum = len(count)
            # sorted_collectibles = sorted(enabled_collectibles.values(), key=lambda x: x.rarity)
            # if you want the Rarity to only show full numbers like 1 or 12 use the code part here:
            # rarity = int(collectible.rarity)
            # otherwise you want to display numbers like 1.5, 5.3, 76.9 use the normal part.
            if countNum != 0:
                entry = (name, f"{emote} Count: {countNum}")
                entries.append(entry)
                nothingcheck = "something lol"

        # This is the number of countryballs who are displayed at one page,
        # you can change this, but keep in mind: discord has an embed size limit.
        per_page = 5
        special_str = f" ({special.name})" if special else ""
        if nothingcheck == "":
            if user:
                return await interaction.response.send_message(
                    f"{user} has no {special_str} {settings.plural_collectible_name} yet.",
                    ephemeral=True,
                )
            else:
                return await interaction.response.send_message(
                    f"There are no {special_str} {settings.plural_collectible_name} yet.",
                    ephemeral=True,
                )
        else:
            source = FieldPageSource(entries, per_page=per_page, inline=False, clear_description=False)
            source.embed.description = (
                f"__**{settings.bot_name}{special_str} count**__"
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
    async def bulk_transfer(
        self,
        interaction: discord.Interaction[BallsDexBot],
        donor: discord.User,
        receiver: discord.User,
    ):
        """
        Transfer all countryballs from one user to another.

        Parameters
        ----------
        donor: discord.User
            The user from whom the countryballs will be transferred.
        receiver: discord.User
            The user who will receive the countryballs.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Get the player objects for both the donor and the receiver
        donor_player, _ = await Player.get_or_create(discord_id=donor.id)
        receiver_player, _ = await Player.get_or_create(discord_id=receiver.id)

        # Get all countryballs owned by the donor
        balls_to_transfer = await BallInstance.filter(player=donor_player).prefetch_related("player")

        if not balls_to_transfer:
            await interaction.followup.send(
                f"{donor} does not own any {settings.plural_collectible_name} to transfer.", ephemeral=True
            )
            return
        view = ConfirmChoiceView(
            interaction,
            accept_message=f"Confirmed, transferring all {settings.plural_collectible_name}...",
            cancel_message="Request cancelled.",
        )
        await interaction.followup.send(
            f"Are you sure you want to transfer all {settings.plural_collectible_name} from {donor} to {receiver}?\nThis cannot be reversed.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.value:
            return
        # Start the bulk transfer
        trade = await Trade.create(player1=donor_player, player2=receiver_player)
        for ball in balls_to_transfer:
            ball.player = receiver_player  
            await ball.save()

            # Log the transfer in the trade table
            await TradeObject.create(trade=trade, ballinstance=ball, player=donor_player)

        await interaction.followup.send(
            f"Transferred all {settings.plural_collectible_name} from {donor} to {receiver}.",
            ephemeral=True,
        )

        # Log the action
        await log_action(
            f"{interaction.user} bulk-transferred all {settings.plural_collectible_name} from {donor} to {receiver}.",
            interaction.client,
        )

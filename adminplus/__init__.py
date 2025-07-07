import logging
from typing import TYPE_CHECKING

from discord import app_commands

from ballsdex.packages.adminplus.cog import Adminplus

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.adminplus")


def command_count(cog: Adminplus) -> int:
    total = 0
    for command in cog.walk_app_commands():
        total += len(command.name) + len(command.description)
        if isinstance(command, app_commands.Group):
            continue
        for param in command.parameters:
            total += len(param.name) + len(param.description)
            for choice in param.choices:
                total += len(choice.name) + (
                    int(choice.value)
                    if isinstance(choice.value, int | float)
                    else len(choice.value)
                )
    return total


def strip_descriptions(cog: Adminplus):
    for command in cog.walk_app_commands():
        command.description = "."
        if isinstance(command, app_commands.Group):
            continue
        for param in command.parameters:
            param._Parameter__parent.description = "."  # type: ignore


async def setup(bot: "BallsDexBot"):
    n = Adminplus(bot)
    if command_count(n) > 3900:
        strip_descriptions(n)
        log.warn("/adminplus command too long, stripping descriptions")
    await bot.add_cog(n)

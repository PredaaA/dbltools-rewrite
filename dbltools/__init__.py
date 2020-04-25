from redbot.core.bot import Red
from .dbltools import DblTools


def setup(bot: Red):
    old_payday = bot.get_command("payday")
    if old_payday:
        bot.remove_command(old_payday.name)
    cog = DblTools(bot)
    bot.add_cog(cog)

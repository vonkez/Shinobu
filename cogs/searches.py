from discord.ext import commands


class Searches(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # TODO: nsfw, google, urban dictionary


def setup(bot):
    bot.add_cog(Searches(bot))

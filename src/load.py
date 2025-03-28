from discord.ext import commands

class LoadModules:
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def register_cogs(self):
        self.bot.load_extension("src.system.status")
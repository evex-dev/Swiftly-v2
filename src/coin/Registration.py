# コインに登録する実装
import discord
from discord.ext import commands

class coin_Registation(commands.Cog):
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        # コインの登録に関する初期化処理をここに追加できます。
        # 例: データベース接続、API設定など

async def setup(bot: commands.Bot):
    await bot.add_cog(coin_Registation(bot))
import discord
from discord.ext import commands

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="help", description="Swiftlyヘルプを表示します。")
    async def help_command(self, interaction: discord.Interaction):
        """主要なコマンドの一覧を表示する"""
        embed = discord.Embed(
            title="Swiftlyヘルプ",
            description=(
                "Swiftlyを導入していただきありがとうございます！\n"
            ),
            color=discord.Color.yellow()
        )

        # embed.add_field(
        #     name="Swiftly",
        #     value=(
        #         "あああああああああああああああ"
        #     ),
        #     inline=False
        # )

        embed.set_footer(
            text="詳細を知りたい場合は、各コマンドを直接実行するか、全コマンド詳細をご確認ください！"
        )

        await interaction.response.send_message(embed=embed)

# Cogのセットアップ
async def setup(bot):
    await bot.add_cog(Help(bot))
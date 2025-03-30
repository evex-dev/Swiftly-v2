import discord
from discord.ext import commands
import os

class CommandListGen(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # 認可されたユーザーかどうかを確認する関数
    def is_authorized_user(ctx):
        return ctx.author.id == 1241397634095120438

    @commands.command(
        name="generate_command_list", 
        help="すべてのコマンドのリストを生成します。(bot管理者専用)", 
        usage="", 
        brief="コマンドリストを生成", 
        permissions="管理者"
    )
    @commands.has_permissions(administrator=True)  # 管理者権限が必要
    @commands.check(is_authorized_user)  # 認可されたユーザーのみ実行可能
    async def generate_command_list(self, ctx):
        try:
            # data ディレクトリが存在することを確認
            os.makedirs("data", exist_ok=True)

            # ファイルを開いて書き込み
            with open("data/commands.txt", "w", encoding="utf-8") as file:
                for command in self.bot.commands:
                    # コマンドの詳細を書き込む
                    file.write(f"名前: {command.name}\n")
                    file.write(f"説明: {command.help or '説明がありません。'}\n")
                    file.write(f"使用法: {command.usage or '使用法がありません。'}\n")
                    file.write(f"必要な権限: {command.extras.get('permissions', 'なし')}\n")
                    file.write(f"オプション: {command.brief or 'オプションがありません。'}\n")
                    file.write("-" * 40 + "\n")

            await ctx.send("コマンドリストが生成され、`data/commands.txt` に保存されました。")
        except Exception as e:
            await ctx.send(f"エラーが発生しました: {e}")

# Cog をセットアップする非同期関数
async def setup(bot):
    await bot.add_cog(CommandListGen(bot))
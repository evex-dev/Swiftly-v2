import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio

# .envファイルから環境変数をロード
load_dotenv()

# トークンを環境変数から取得
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="sw!", intents=intents)  # `commands.Bot`を使用

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

    tasks = []
    for root, _, files in os.walk('./src'):
        for file in files:
            if file.endswith('.py'):
                relative_path = os.path.relpath(root, './src').replace(os.sep, '.')
                module_name = f'src.{relative_path}.{file[:-3]}' if relative_path != '.' else f'src.{file[:-3]}'
                tasks.append(bot.load_extension(module_name))

    await asyncio.gather(*tasks)

    await bot.tree.sync()

    print("All cogs loaded and commands synced!")

# Botの起動
bot.run(TOKEN)
import discord
from discord.ext import commands
import os
import sys
from dotenv import load_dotenv
import asyncio
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import aiomysql

# 現在のディレクトリをPythonパスに追加
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# .envファイルから環境変数をロード
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
DB_HOST = os.getenv('DB_HOST')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_NAME = os.getenv('DB_NAME', 'swiftly')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="swd!", intents=intents)

# DBプールをグローバルで保持する
async def init_db_pool():
    try:
        # 初回接続でデータベースの存在確認と作成
        temp_pool = await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            autocommit=True
        )
        async with temp_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
        temp_pool.close()
        await temp_pool.wait_closed()
        # 本来のプールを作成
        pool = await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            autocommit=True
        )
        return pool
    except Exception as e:
        print(f"データベース接続に失敗しました: {e}")
        return None

# ユーザー数を保存（既存のDBプールを再利用）
async def save_user_count():
    all_users = {member.id for guild in bot.guilds for member in guild.members}
    user_count = len(all_users)
    try:
        pool = bot.db_pool
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "CREATE TABLE IF NOT EXISTS user_count (name VARCHAR(50), count INT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
                )
                await cur.execute(
                    "INSERT INTO user_count (name, count) VALUES (%s, %s)",
                    ("user_count", user_count)
                )
        print(f"ユーザー数を保存しました: {user_count}人")
    except Exception as e:
        print(f"ユーザー数の保存に失敗しました: {e}")

class CogReloader(FileSystemEventHandler):
    def __init__(self, bot):
        self.bot = bot
        self.loop = asyncio.get_event_loop()
        self.pending_reloads = set()

    def on_modified(self, event):
        if event.src_path.endswith('.py'):
            rel_path = os.path.relpath(event.src_path, './src')
            rel_path = os.path.splitext(rel_path)[0]
            module_name = f'src.{rel_path.replace(os.sep, ".")}'
            if module_name in self.pending_reloads:
                return
            self.pending_reloads.add(module_name)
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._reload_and_clear(module_name))
            )

    async def _reload_and_clear(self, module_name):
        try:
            await self.reload_cog(module_name)
        finally:
            self.pending_reloads.discard(module_name)

    async def reload_cog(self, module_name):
        try:
            await self.bot.reload_extension(module_name)
            print(f"リロード完了: {module_name}")
            await self.bot.tree.sync()
            print("コマンド再同期完了")
        except Exception as e:
            print(f"リロード失敗 {module_name}: {e}")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    
    # 起動時１度のみDBプールを作成（各関数で再作成しない）
    bot.db_pool = await init_db_pool()
    if bot.db_pool is None:
        print("DBプールの作成に失敗しました。")

    # コグ（拡張機能）を並列にロード
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
    
    # watchdogの設定と起動（非同期タスクとして実行）
    event_handler = CogReloader(bot)
    observer = Observer()
    observer.schedule(event_handler, path='./src', recursive=True)
    observer.start()
    print("Watchdogを起動しました。srcディレクトリを監視中...")
    
    # ステータス自動更新タスクとユーザー数保存タスクを並行して開始
    async def update_status():
        while True:
            await bot.change_presence(
                activity=discord.Game(
                    name=f"{len(bot.guilds)}のサーバー数 || {round(bot.latency * 1000)}ms"
                )
            )
            await asyncio.sleep(30)
    
    async def periodic_save_user_count():
        while True:
            await save_user_count()
            await asyncio.sleep(3600)
    
    asyncio.create_task(update_status())
    asyncio.create_task(periodic_save_user_count())
    print("定期タスクを開始しました")

bot.run(TOKEN)

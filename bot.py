import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import aiomysql

# .envファイルから環境変数をロード
load_dotenv()

# トークンと環境変数から取得
TOKEN = os.getenv('DISCORD_TOKEN')
DB_HOST = os.getenv('DB_HOST')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_NAME = os.getenv('DB_NAME', 'swiftly')  # デフォルト値としてswiftlyを設定

# Enable the message_content intent
intents = discord.Intents.default()
intents.message_content = True  # Explicitly enable message_content intent
intents.members = True  # Enable members intent if needed

bot = commands.Bot(command_prefix="sw!", intents=intents)  # `commands.Bot`を使用
# データベース接続プールを作成する関数
async def create_pool():
    try:
        # まずデータベースなしで接続
        temp_pool = await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            autocommit=True
        )
        
        async with temp_pool.acquire() as conn:
            async with conn.cursor() as cur:
                # データベースが存在するか確認し、存在しない場合は作成
                await cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
        
        # 一時的な接続を閉じる
        temp_pool.close()
        await temp_pool.wait_closed()
        
        # 改めて指定されたデータベースに接続
        pool = await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,  # ここでデータベースを明示的に指定
            autocommit=True
        )
        return pool
    except Exception as e:
        print(f"データベース接続に失敗しました: {e}")
        return None

# ユーザー数をデータベースに保存する関数
async def save_user_count():
    # すべてのサーバーの全ユーザーを集める
    all_users = set()
    for guild in bot.guilds:
        for member in guild.members:
            all_users.add(member.id)
    
    user_count = len(all_users)
    
    try:
        pool = await create_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # テーブルが存在しない場合は作成
                await cur.execute(
                    "CREATE TABLE IF NOT EXISTS user_count (name VARCHAR(50), count INT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
                )
                
                # ユーザー数を保存
                await cur.execute(
                    "INSERT INTO user_count (name, count) VALUES (%s, %s)",
                    ("user_count", user_count)
                )
        pool.close()
        await pool.wait_closed()
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
            # ファイルパスをモジュール名に変換
            rel_path = os.path.relpath(event.src_path, './src')
            rel_path = os.path.splitext(rel_path)[0]  # 拡張子を削除
            module_name = f'src.{rel_path.replace(os.sep, ".")}'

            # 同じモジュールの連続リロードを防止
            if module_name in self.pending_reloads:
                return

            self.pending_reloads.add(module_name)

            # メインイベントループに処理を送る
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
    
    # watchdogの設定と起動
    event_handler = CogReloader(bot)
    observer = Observer()
    observer.schedule(event_handler, path='./src', recursive=True)
    observer.start()
    print("Watchdogを起動しました。srcディレクトリを監視中...")
    
    # ステータス自動更新タスクを開始
    async def update_status():
        while True:
            await bot.change_presence(
                activity=discord.Game(
                    name=f"{len(bot.guilds)}のサーバー数 || {round(bot.latency * 1000)}ms"
                )
            )
            await asyncio.sleep(30)  # 30秒ごとに更新

    asyncio.create_task(update_status())
    
    # ユーザー数を定期的に保存するタスク
    async def periodic_save_user_count():
        while True:
            await save_user_count()
            await asyncio.sleep(3600)  # 1時間ごとに保存
    
    asyncio.create_task(periodic_save_user_count())
    print("ユーザー数の定期保存タスクを開始しました")

# Botの起動
bot.run(TOKEN)
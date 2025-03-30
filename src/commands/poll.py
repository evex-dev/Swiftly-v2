import asyncio
import base64
from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands
import hashlib
import os
import pytz
import aiomysql
from contextlib import asynccontextmanager

# データベース接続関数
@asynccontextmanager
async def get_db_connection():
    """非同期コンテキストマネージャとしてデータベース接続を取得"""
    conn = await aiomysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
        autocommit=True
    )
    try:
        yield conn
    finally:
        conn.close()

from cryptography.fernet import Fernet
from typing import Optional
from dotenv import load_dotenv

# .envファイルの読み込み
load_dotenv()

RATE_LIMIT_SECONDS = 5  # コマンドのレート制限
VOTE_RATE_LIMIT_SECONDS = 2  # 投票アクションのレート制限
CLEANUP_DAYS = 1  # 終了した投票を保持する日数
MAX_OPTIONS = 5  # 最大選択肢数（Discordの制限に合わせる）
RECOVER = False  # BOT再起動時にアクティブな投票を復元するかどうか(レートリミット注意)

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = "poll"

# 暗号化キーの管理
async def get_or_create_key():
    """暗号化キーをデータベースから取得または新規作成"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # encryption_keysテーブルが存在しない場合は作成
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS encryption_keys (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        key_value TEXT NOT NULL
                    )
                """)

                # 既存のキーを取得
                await cursor.execute("SELECT key_value FROM encryption_keys LIMIT 1")
                result = await cursor.fetchone()
                if result:
                    return base64.b64decode(result[0])

                # キーが存在しない場合は新規作成
                key = Fernet.generate_key()
                await cursor.execute("INSERT INTO encryption_keys (key_value) VALUES (%s)", (base64.b64encode(key).decode(),))
                await conn.commit()
                return key

    except Exception as e:
        print(f"暗号化キーの取得または作成中にエラーが発生: {e}")
        raise

# 暗号化キーの初期化
ENCRYPTION_KEY = None
cipher_suite = None

async def initialize_encryption_key():
    global ENCRYPTION_KEY, cipher_suite
    ENCRYPTION_KEY = await get_or_create_key()
    cipher_suite = Fernet(ENCRYPTION_KEY)

DURATION_CHOICES = [
    app_commands.Choice(name="30分", value=30),
    app_commands.Choice(name="1時間", value=60),
    app_commands.Choice(name="12時間", value=720),
    app_commands.Choice(name="1日", value=1440),
    app_commands.Choice(name="3日", value=4320),
    app_commands.Choice(name="1週間", value=10080)
]


def encrypt_user_id(user_id: int) -> str:
    """ユーザーIDを暗号化"""
    data = str(user_id).encode()
    return base64.b64encode(cipher_suite.encrypt(data)).decode()


def get_vote_hash(poll_id: int, user_id: int) -> str:
    """投票確認用のハッシュを生成"""
    data = f"{poll_id}:{user_id}".encode()
    return hashlib.sha256(data).hexdigest()


class PollView(discord.ui.View):
    def __init__(self, options: list, poll_id: int):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        for i, option in enumerate(options):
            self.add_item(PollButton(option, i, poll_id))


class PollButton(discord.ui.Button):
    def __init__(self, label: str, option_id: int, poll_id: int):
        super().__init__(style=discord.ButtonStyle.primary, label=label, custom_id=f"poll_{poll_id}_{option_id}")
        self.option_id = option_id
        self.poll_id = poll_id
        self._last_uses = {}

    def _check_rate_limit(self, user_id: int) -> tuple[bool, Optional[int]]:
        now = datetime.now()
        if user_id in self._last_uses:
            time_diff = now - self._last_uses[user_id]
            if time_diff < timedelta(seconds=VOTE_RATE_LIMIT_SECONDS):
                remaining = VOTE_RATE_LIMIT_SECONDS - \
                    int(time_diff.total_seconds())
                return True, remaining
        return False, None

    async def callback(self, interaction: discord.Interaction):
        # レート制限
        is_limited, remaining = self._check_rate_limit(interaction.user.id)
        if is_limited:
            await interaction.response.send_message(
                f"投票が早すぎます。{remaining}秒後に試してね",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("START TRANSACTION")
                    try:
                        # 投票が有効かチェック
                        await cursor.execute("SELECT is_active FROM polls WHERE id = %s", (self.poll_id,))
                        poll = await cursor.fetchone()
                        if not poll or not poll[0]:
                            await interaction.followup.send("この投票はもう終了しているよ", ephemeral=True)
                            await cursor.execute("ROLLBACK")
                            return

                        # ユーザーが既に投票しているかチェック
                        vote_hash = get_vote_hash(
                            self.poll_id, interaction.user.id)
                        await cursor.execute("SELECT 1 FROM vote_checks WHERE vote_hash = %s", (vote_hash,))
                        if await cursor.fetchone():
                            await interaction.followup.send("既に投票済みだよ", ephemeral=True)
                            await cursor.execute("ROLLBACK")
                            return

                        # 暗号化されたユーザーIDと投票データを保存
                        encrypted_user_id = encrypt_user_id(interaction.user.id)
                        await cursor.execute("""\
                            INSERT INTO votes (poll_id, encrypted_user_id, choice)
                            VALUES (%s, %s, %s)
                        """, (self.poll_id, encrypted_user_id, self.option_id))

                        # 投票チェック用のハッシュを保存
                        await cursor.execute("INSERT INTO vote_checks (vote_hash) VALUES (%s)", (vote_hash,))

                        # 投票数を更新
                        await cursor.execute("""\
                            UPDATE polls
                            SET total_votes = (
                                SELECT COUNT(*)
                                FROM votes
                                WHERE poll_id = %s
                            )
                            WHERE id = %s
                        """, (self.poll_id, self.poll_id))

                        await cursor.execute("COMMIT")

                    except Exception as e:
                        await cursor.execute("ROLLBACK")
                        print(f"投票処理中にエラーが発生: {e}")
                        await interaction.followup.send("投票の処理中にエラーが発生したよ。もう一度試してね", ephemeral=True)
                        return

                    # 現在の投票数を取得
                    await cursor.execute("SELECT total_votes FROM polls WHERE id = %s", (self.poll_id,))
                    result = await cursor.fetchone()
                    total_votes = result[0] if result else 0

        except Exception as e:
            print(f"データベース接続エラー: {e}")
            await interaction.followup.send("システムエラーが発生したよ。もう一度試してね", ephemeral=True)
            return

        # レート制限を更新
        self._last_uses[interaction.user.id] = datetime.now()

        # 投票メッセージを更新
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT channel_id, message_id FROM polls WHERE id = %s", (self.poll_id,))
                    poll_location = await cursor.fetchone()

            if poll_location and poll_location[0] and poll_location[1]:
                channel_id, message_id = poll_location
                channel = interaction.guild.get_channel(channel_id)

                if channel:
                    try:
                        message = await channel.fetch_message(message_id)
                        if message.embeds and len(message.embeds) > 0:
                            embed = message.embeds[0]
                            for i, field in enumerate(embed.fields):
                                if field.name == "🗳️ 投票数":
                                    embed.set_field_at(
                                        i,
                                        name="🗳️ 投票数",
                                        value=str(total_votes),
                                        inline=False
                                    )
                                    await message.edit(embed=embed)
                                    break
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                        print(f"投票メッセージの更新中にエラーが発生しました: {e}")
        except Exception as e:
            print(f"投票数の更新中にエラーが発生しました: {e}")

        await interaction.followup.send(f"投票を受け付けたよ（現在の投票数: {total_votes}票）", ephemeral=True)


class Poll(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_uses = {}

    def _check_rate_limit(self, user_id: int) -> tuple[bool, Optional[int]]:
        """ユーザーのレート制限を確認"""
        now = datetime.now()
        if user_id in self._last_uses:
            time_diff = now - self._last_uses[user_id]
            if time_diff < timedelta(seconds=RATE_LIMIT_SECONDS):
                remaining = RATE_LIMIT_SECONDS - int(time_diff.total_seconds())
                return True, remaining
        return False, None

    async def init_db(self):
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # pollsテーブル
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS polls (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        creator_id BIGINT NOT NULL,
                        end_time TIMESTAMP NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        options TEXT NOT NULL,
                        channel_id BIGINT,
                        message_id BIGINT,
                        total_votes INT DEFAULT 0
                    )
                """)

                # votesテーブル
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS votes (
                        poll_id INT NOT NULL,
                        encrypted_user_id TEXT NOT NULL,
                        choice INT NOT NULL,
                        FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE
                    )
                """)

                # vote_checksテーブル
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS vote_checks (
                        vote_hash VARCHAR(255) PRIMARY KEY
                    )
                """)

                # encryption_keysテーブル
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS encryption_keys (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        key_value TEXT NOT NULL
                    )
                """)

    async def recover_active_polls(self):
        """アクティブな投票の状態を復元"""
        await self.bot.wait_until_ready()
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("""\
                        SELECT id, title, options, channel_id, message_id, total_votes
                        FROM polls
                        WHERE is_active = 1
                    """)
                    active_polls = await cursor.fetchall()

                for poll in active_polls:
                    poll_id, title, options_str, channel_id, message_id, total_votes = poll
                    options = options_str.split(",")
                    # チャンネルとメッセージを取得
                    for guild in self.bot.guilds:
                        channel = guild.get_channel(channel_id)
                        if channel:
                            try:
                                # 古いメッセージを削除
                                if message_id:
                                    try:
                                        message = await channel.fetch_message(message_id)
                                        await message.delete()
                                    except:
                                        pass

                                # 新しい投票メッセージを作成
                                embed = discord.Embed(
                                    title=f"📊 {title}",
                                    description="🔒 **匿名投票**\n\n(BOTの再起動により再作成されました)",
                                    color=discord.Color.blue()
                                )
                                embed.add_field(
                                    name="🗳️ 投票数",
                                    value=str(total_votes),
                                    inline=False
                                )

                                view = PollView(options, poll_id)
                                message = await channel.send(embed=embed, view=view)

                                # 新しいメッセージIDを保存
                                await conn.cursor().execute(
                                    "UPDATE polls SET message_id = %s WHERE id = %s",
                                    (message.id, poll_id)
                                )
                                await conn.commit()
                                break
                            except Exception as e:
                                print(f"投票の復元中にエラーが発生: {e}")
        except Exception as e:
            print(f"アクティブな投票の復元中にエラーが発生: {e}")

    async def cleanup_old_polls(self):
        """終了した古い投票を定期的に削除"""
        while True:
            try:
                async with get_db_connection() as conn:
                    async with conn.cursor() as cursor:
                        cleanup_time = (datetime.now() - timedelta(days=CLEANUP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
                        # 古い投票の票を削除
                        await cursor.execute("""
                            DELETE FROM votes WHERE poll_id IN (
                                SELECT id FROM polls
                                WHERE is_active = 0
                                AND end_time < %s
                            )
                        """, (cleanup_time,))

                        # 古い投票チェックを削除 (timestamp()を使わないように修正)
                        timestamp_prefix = f"poll_{int(datetime.now().timestamp()) - CLEANUP_DAYS * 86400}%"
                        await cursor.execute("""
                            DELETE FROM vote_checks 
                            WHERE vote_hash LIKE %s
                        """, (timestamp_prefix,))

                        # 古い投票を削除
                        await cursor.execute("""
                            DELETE FROM polls
                            WHERE is_active = 0
                            AND end_time < %s
                        """, (cleanup_time,))
            except Exception as e:
                print(f"Error in cleanup_old_polls: {e}")
            await asyncio.sleep(86400)  # 24時間ごとに実行

    async def check_ended_polls(self):
        """終了時間を過ぎた投票を自動的に終了する"""
        while True:
            try:
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                async with get_db_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("""
                            SELECT id, title, options, end_time, channel_id, message_id
                            FROM polls
                            WHERE is_active = 1
                            AND end_time < %s
                        """, (current_time,))
                        ended_polls = await cursor.fetchall()

                        for poll in ended_polls:
                            poll_id, title, options_str, _, channel_id, message_id = poll

                            # 投票を終了状態に更新
                            await cursor.execute("UPDATE polls SET is_active = 0 WHERE id = %s", (poll_id,))

                            # 投票結果を集計
                            options = options_str.split(",")
                            vote_counts = {i: 0 for i in range(len(options))}
                            total_votes = 0

                            await cursor.execute("""
                                SELECT choice, COUNT(*) as votes
                                FROM votes
                                WHERE poll_id = %s
                                GROUP BY choice
                            """, (poll_id,))
                            results = await cursor.fetchall()

                            for choice, votes in results:
                                vote_counts[choice] = votes
                                total_votes += votes

                            # 結果表示用のEmbed作成
                            embed = discord.Embed(
                                title=f"📊 投票結果: {title} (自動終了)",
                                description="🔒 この投票は匿名で実施されました",
                                color=discord.Color.green()
                            )

                            max_votes = max(vote_counts.values()) if vote_counts else 0
                            for i, option in enumerate(options):
                                votes = vote_counts.get(i, 0)
                                percentage = (votes / total_votes * 100) if total_votes > 0 else 0
                                bar_length = int(percentage / 5 * total_votes / max_votes) if max_votes > 0 else 0
                                progress_bar = "█" * bar_length + "▁" * (20 - bar_length)
                                embed.add_field(
                                    name=option,
                                    value=f"{progress_bar} {votes}票 ({percentage:.1f}%)",
                                    inline=False
                                )

                            embed.set_footer(text=f"総投票数: {total_votes}票")

                            # チャンネルを取得して結果を送信
                            if channel_id:
                                for guild in self.bot.guilds:
                                    channel = guild.get_channel(channel_id)
                                    if channel:
                                        try:
                                            await channel.send("投票の終了時間になったよ", embed=embed)

                                            if message_id:
                                                try:
                                                    original_message = await channel.fetch_message(message_id)
                                                    await original_message.delete()
                                                except:
                                                    pass

                                            break
                                        except Exception as e:
                                            print(f"投票結果の送信中にエラーが発生しました: {e}")

            except Exception as e:
                print(f"Error in check_ended_polls: {e}")

            await asyncio.sleep(10)

    @app_commands.command(name="poll", description="匿名投票の作成・管理")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="投票を作成", value="create"),
            app_commands.Choice(name="投票を終了", value="end")
        ],
        duration=DURATION_CHOICES
    )
    @app_commands.describe(
        action="実行するアクション",
        title="投票のタイトル",
        description="投票の説明",
        duration="投票の期間",
        options="投票の選択肢（カンマ区切り）"
    )
    async def poll(
        self,
        interaction: discord.Interaction,
        action: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        duration: Optional[app_commands.Choice[int]] = None,
        options: Optional[str] = None
    ):
        # レート制限チェック
        is_limited, remaining = self._check_rate_limit(interaction.user.id)
        if is_limited:
            await interaction.response.send_message(
                f"コマンドの実行が早すぎます。{remaining}秒後に試してね",
                ephemeral=True
            )
            return

        if action == "create":
            if not all([title, options]):
                await interaction.response.send_message(
                    "タイトルと選択肢は必須だよ", ephemeral=True)
                return

            option_list = [opt.strip() for opt in options.split(",")]
            if len(option_list) < 2:
                await interaction.response.send_message(
                    "選択肢は2つ以上必要だよ", ephemeral=True)
                return

            if len(option_list) > MAX_OPTIONS:
                await interaction.response.send_message(
                    f"選択肢は最大{MAX_OPTIONS}個までだよ", ephemeral=True)
                return

            # 先に応答を遅延させる
            await interaction.response.defer()

            try:
                jst = pytz.timezone("Asia/Tokyo")
                duration_minutes = duration.value if duration else 1440  # デフォルト24時間
                end_time = datetime.now(
                    jst) + timedelta(minutes=duration_minutes)

                async with get_db_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "INSERT INTO polls (title, description, creator_id, end_time, options, channel_id) VALUES (%s, %s, %s, %s, %s, %s)",
                            (title, description or "", interaction.user.id, end_time.strftime('%Y-%m-%d %H:%M:%S'), options, interaction.channel_id)
                        )
                        poll_id = cursor.lastrowid
                        await conn.commit()
            except Exception as e:
                print(f"投票作成中にエラーが発生: {e}")
                await interaction.followup.send("投票の作成中にエラーが発生したよ", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"📊 {title}",
                description=f"🔒 **匿名投票**\n\n{description or '投票を開始するよ'}",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="⏰ 終了時刻",
                value=f"{end_time.strftime('%Y/%m/%d %H:%M')} (JST)\n<t:{int(end_time.timestamp())}:R>",
                inline=False
            )
            embed.add_field(
                name="🗳️ 投票数",
                value="0",
                inline=False
            )

            view = PollView(option_list, poll_id)
            message = await interaction.followup.send(embed=embed, view=view)

            try:
                async with get_db_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "UPDATE polls SET message_id = %s WHERE id = %s",
                            (message.id, poll_id)
                        )
                        await conn.commit()
            except Exception as e:
                print(f"メッセージID保存中にエラーが発生しました: {e}")

            self._last_uses[interaction.user.id] = datetime.now()

        elif action == "end":
            try:
                async with get_db_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "SELECT id, title FROM polls WHERE creator_id = %s AND is_active = 1",
                            (interaction.user.id,)
                        )
                        polls = await cursor.fetchall()

                if not polls:
                    await interaction.response.send_message(
                        "終了可能な投票が見つからないよ", ephemeral=True)
                    return

                options = [
                    discord.SelectOption(
                        label=f"ID: {poll[0]} - {poll[1]}",
                        value=str(poll[0])
                    ) for poll in polls
                ]

                select_menu = discord.ui.Select(
                    placeholder="終了する投票を選択してね",
                    options=options
                )

                async def select_callback(interaction: discord.Interaction):
                    poll_id = int(select_menu.values[0])
                    try:
                        async with get_db_connection() as conn:
                            async with conn.cursor() as cursor:
                                await cursor.execute("START TRANSACTION")
                                try:
                                    # 投票結果を取得する前に投票情報を取得
                                    await cursor.execute("""
                                        SELECT title, options, channel_id, message_id 
                                        FROM polls 
                                        WHERE id = %s
                                    """, (poll_id,))
                                    poll_data = await cursor.fetchone()
                                    
                                    if not poll_data:
                                        await cursor.execute("ROLLBACK")
                                        await interaction.response.send_message("投票が見つかりませんでした", ephemeral=True)
                                        return
                                        
                                    title, options_str, channel_id, message_id = poll_data
                                    options = options_str.split(",")
                                    
                                    # 投票結果を集計
                                    await cursor.execute("""
                                        SELECT choice, COUNT(*) as votes
                                        FROM votes
                                        WHERE poll_id = %s
                                        GROUP BY choice
                                    """, (poll_id,))
                                    vote_results = await cursor.fetchall()
                                    
                                    # 投票数を集計
                                    vote_counts = {i: 0 for i in range(len(options))}
                                    total_votes = 0
                                    
                                    for choice, votes in vote_results:
                                        if choice is not None:
                                            vote_counts[choice] = votes
                                            total_votes += votes
                                    
                                    # 関連するメッセージを削除
                                    if channel_id and message_id:
                                        try:
                                            channel = interaction.guild.get_channel(channel_id)
                                            if channel:
                                                message = await channel.fetch_message(message_id)
                                                await message.delete()
                                        except Exception as e:
                                            print(f"投票メッセージの削除中にエラーが発生: {e}")
                                    
                                    # データベースからすべての関連レコードを削除
                                    await cursor.execute("DELETE FROM votes WHERE poll_id = %s", (poll_id,))
                                    await cursor.execute("DELETE FROM polls WHERE id = %s", (poll_id,))
                                    
                                    # vote_checksからも関連するハッシュを削除
                                    vote_hash_prefix = f"%{poll_id}:%"
                                    await cursor.execute("DELETE FROM vote_checks WHERE vote_hash LIKE %s", (vote_hash_prefix,))
                                    
                                    await cursor.execute("COMMIT")
                                except Exception as e:
                                    await cursor.execute("ROLLBACK")
                                    print(f"投票終了処理中にエラーが発生: {e}")
                                    await interaction.response.send_message("投票の終了処理中にエラーが発生したよ", ephemeral=True)
                                    return
                                    
                        # 結果表示用のEmbed作成
                        embed = discord.Embed(
                            title=f"📊 投票結果: {title}",
                            description="🔒 この投票は匿名で実施されたよ",
                            color=discord.Color.green()
                        )

                        max_votes = max(vote_counts.values()) if vote_counts else 0
                        for i, option in enumerate(options):
                            votes = vote_counts.get(i, 0)
                            percentage = (votes / total_votes * 100) if total_votes > 0 else 0
                            bar_length = int(
                                percentage / 5 * total_votes / max_votes) if max_votes > 0 else 0
                            progress_bar = "█" * bar_length + \
                                "▁" * (20 - bar_length)
                            embed.add_field(
                                name=option,
                                value=f"{progress_bar} {votes}票 ({percentage:.1f}%)",
                                inline=False
                            )

                        embed.set_footer(text=f"総投票数: {total_votes}票")

                        await interaction.response.send_message("投票を終了して削除したよ", ephemeral=True)
                        await interaction.channel.send(embed=embed)

                    except Exception as e:
                        print(f"投票終了中にエラーが発生: {e}")
                        await interaction.response.send_message("システムエラーが発生したよ", ephemeral=True)

                select_menu.callback = select_callback
                view = discord.ui.View()
                view.add_item(select_menu)
                await interaction.response.send_message("終了する投票を選択してね: ", view=view, ephemeral=True)

                self._last_uses[interaction.user.id] = datetime.now()

            except Exception as e:
                print(f"投票終了選択中にエラーが発生: {e}")
                await interaction.response.send_message("システムエラーが発生したよ", ephemeral=True)

        else:
            await interaction.response.send_message(
                "無効なアクションが指定されました。もう一度試してください。",
                ephemeral=True
            )
async def setup(bot: commands.Bot):
    await initialize_encryption_key()
    await bot.add_cog(Poll(bot))


async def setup(bot: commands.Bot):
    await initialize_encryption_key()
    cog = Poll(bot)
    await cog.init_db()
    await bot.add_cog(cog)
    bot.loop.create_task(cog.cleanup_old_polls())
    bot.loop.create_task(cog.check_ended_polls())
    if RECOVER:
        bot.loop.create_task(cog.recover_active_polls())

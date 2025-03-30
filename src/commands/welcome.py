import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Optional, Tuple
import logging
import aiomysql
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

# 環境変数の読み込み
load_dotenv()

# MySQLの接続情報
DB_NAME: Final[str] = "welcome"
DB_HOST: Final[str] = os.getenv("DB_HOST", "localhost")
DB_USER: Final[str] = os.getenv("DB_USER", "root")
DB_PASSWORD: Final[str] = os.getenv("DB_PASSWORD", "")

DEFAULT_INCREMENT: Final[int] = 100
MIN_INCREMENT: Final[int] = 5
MAX_INCREMENT: Final[int] = 1000
JOIN_COOLDOWN: Final[int] = 3  # seconds

ERROR_MESSAGES: Final[dict] = {
    "no_permission": "コマンドを使用するにはサーバーの管理権限が必要です。",
    "invalid_action": "enableまたはdisableを指定してください。",
    "invalid_increment": f"{MIN_INCREMENT}～{MAX_INCREMENT}人の間で指定してください。",
    "no_channel": "ONにする場合はチャンネルを指定してください。"
}

SUCCESS_MESSAGES: Final[dict] = {
    "enabled": "参加メッセージをONにしました!\n{increment}人ごとに{channel}でお祝いメッセージを送信します",
    "disabled": "参加メッセージを無効にしました!"
}

WELCOME_MESSAGES: Final[dict] = {
    "milestone": (
        "🎉🎉🎉 お祝い 🎉🎉🎉\n"
        "{mention} さん、ようこそ！\n"
        "{member_count}人達成！\n"
        "{guild_name}のメンバーが{member_count}人になりました！皆さんありがとうございます！"
    ),
    "normal": (
        "{mention} さん、ようこそ！\n"
        "現在のメンバー数: {member_count}人\n"
        "あと {remaining} 人で {next_milestone}人達成です！"
    )
}

CREATE_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS welcome_settings (
    guild_id BIGINT PRIMARY KEY,
    is_enabled BOOLEAN DEFAULT FALSE,
    member_increment INT DEFAULT 100,
    channel_id BIGINT DEFAULT NULL
)
"""

logger = logging.getLogger(__name__)

class WelcomeDatabase:
    """ウェルカムメッセージの設定を管理するDB"""

    @staticmethod
    async def get_pool():
        """MySQLコネクションプールを取得"""
        return await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            autocommit=True
        )

    @staticmethod
    async def init_database() -> None:
        """DBを初期化"""
        try:
            # DBが存在するか確認して、なければ作成
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
            
            # welcomeテーブルの作成
            pool = await WelcomeDatabase.get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(CREATE_TABLE_SQL)
            pool.close()
            await pool.wait_closed()
        except Exception as e:
            logger.error("Database initialization error: %s", e, exc_info=True)
            raise

    @staticmethod
    async def get_settings(
        guild_id: int
    ) -> Tuple[bool, int, Optional[int]]:
        pool = await WelcomeDatabase.get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT is_enabled, member_increment, channel_id
                        FROM welcome_settings WHERE guild_id = %s
                        """,
                        (guild_id,)
                    )
                    result = await cur.fetchone()
                    return (
                        bool(result[0]),
                        result[1],
                        result[2]
                    ) if result else (False, DEFAULT_INCREMENT, None)
        finally:
            pool.close()
            await pool.wait_closed()

    @staticmethod
    async def update_settings(
        guild_id: int,
        is_enabled: bool,
        member_increment: Optional[int] = None,
        channel_id: Optional[int] = None
    ) -> None:
        """サーバーの設定を更新"""
        pool = await WelcomeDatabase.get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 既存の設定を確認
                    await cur.execute(
                        "SELECT COUNT(*) FROM welcome_settings WHERE guild_id = %s",
                        (guild_id,)
                    )
                    exists = (await cur.fetchone())[0] > 0
                    
                    if exists:
                        # 更新クエリを構築
                        query = "UPDATE welcome_settings SET is_enabled = %s"
                        params = [is_enabled]
                        
                        if member_increment is not None:
                            query += ", member_increment = %s"
                            params.append(member_increment)
                        
                        if channel_id is not None:
                            query += ", channel_id = %s"
                            params.append(channel_id)
                        
                        query += " WHERE guild_id = %s"
                        params.append(guild_id)
                        
                        await cur.execute(query, params)
                    else:
                        # 新規レコードを作成
                        await cur.execute(
                            """
                            INSERT INTO welcome_settings
                            (guild_id, is_enabled, member_increment, channel_id)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (
                                guild_id,
                                is_enabled,
                                member_increment or DEFAULT_INCREMENT,
                                channel_id
                            )
                        )
        finally:
            pool.close()
            await pool.wait_closed()

class MemberWelcomeCog(commands.Cog):
    """メンバー参加時のウェルカムメッセージを管理"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.last_welcome_time = {}

    async def cog_load(self) -> None:
        """Cogのロード時にDBを初期化"""
        await WelcomeDatabase.init_database()

    @app_commands.command(
        name="welcome",
        description="参加メッセージの設定"
    )
    @app_commands.describe(
        action="参加メッセージをON/OFFにします",
        increment="何人ごとにお祝いメッセージを送信するか設定 (デフォルト: 100)",
        channel="メッセージを送信するチャンネル"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="enable", value="enable"),
        app_commands.Choice(name="disable", value="disable")
    ])
    async def welcome_command(
        self,
        interaction: discord.Interaction,
        action: Literal["enable", "disable"],
        increment: Optional[int] = None,
        channel: Optional[discord.TextChannel] = None
    ) -> None:
        """ウェルカムメッセージの設定を行うコマンド"""
        try:
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    ERROR_MESSAGES["no_permission"],
                    ephemeral=True
                )
                return

            is_enabled = action == "enable"
            increment = increment or DEFAULT_INCREMENT

            if increment < MIN_INCREMENT or increment > MAX_INCREMENT:
                await interaction.response.send_message(
                    ERROR_MESSAGES["invalid_increment"],
                    ephemeral=True
                )
                return

            if is_enabled and not channel:
                await interaction.response.send_message(
                    ERROR_MESSAGES["no_channel"],
                    ephemeral=True
                )
                return

            channel_id = channel.id if channel else None
            await WelcomeDatabase.update_settings(
                interaction.guild_id,
                is_enabled,
                increment,
                channel_id
            )

            if is_enabled:
                await interaction.response.send_message(
                    SUCCESS_MESSAGES["enabled"].format(
                        increment=increment,
                        channel=channel.mention
                    ),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    SUCCESS_MESSAGES["disabled"],
                    ephemeral=True
                )

        except Exception as e:
            logger.error("Error in welcome command: %s", e, exc_info=True)
            await interaction.response.send_message(
                f"エラーが発生しました: {e}",
                ephemeral=True
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """メンバー参加時のイベントハンドラ"""
        if member.bot:
            return

        try:
            is_enabled, increment, channel_id = await WelcomeDatabase.get_settings(
                member.guild.id
            )
            if not is_enabled:
                return

            # 参加マクロ対策
            now = datetime.now()
            last_time = self.last_welcome_time.get(member.guild.id)
            if last_time and now - last_time < timedelta(seconds=JOIN_COOLDOWN):
                return
            self.last_welcome_time[member.guild.id] = now

            channel = member.guild.get_channel(channel_id)
            if not channel:
                await WelcomeDatabase.update_settings(
                    member.guild.id,
                    False
                )
                return

            member_count = len(member.guild.members)
            remainder = member_count % increment

            if remainder == 0:
                message = WELCOME_MESSAGES["milestone"].format(
                    mention=member.mention,
                    member_count=member_count,
                    guild_name=member.guild.name
                )
            else:
                message = WELCOME_MESSAGES["normal"].format(
                    mention=member.mention,
                    member_count=member_count,
                    remaining=increment - remainder,
                    next_milestone=member_count + (increment - remainder)
                )

            await channel.send(message)

        except Exception as e:
            logger.error(
                "Error processing member join: %s", e,
                exc_info=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberWelcomeCog(bot))

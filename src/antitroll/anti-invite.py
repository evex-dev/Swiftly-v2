import discord
from discord.ext import commands
import aiomysql
from dotenv import load_dotenv
import os
import re
import asyncio
import aiohttp
from typing import Final, Optional, Set, Deque
from urllib.parse import urlparse
from collections import deque
from pathlib import Path

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = "antiinvite"

INVITE_PATTERNS: Final[Set[str]] = {
    "discord.gg/",
    "discordapp.com/invite/",
    "discord.com/invite/"
}

URL_SHORTENERS: Final[Set[str]] = {
    "x.gd", "bit.ly", "tinyurl.com",
    "goo.gl", "is.gd", "ow.ly",
    "buff.ly", "00m.in"
}

ADMIN_ONLY_MESSAGE: Final[str] = "このコマンドはサーバー管理者のみ実行可能です。"
GUILD_ONLY_MESSAGE: Final[str] = "このコマンドはサーバー内でのみ使用可能です。"
INVITE_WARNING: Final[str] = "Discord招待リンクは禁止です。メッセージは削除されました。"

class AntiInvite(commands.Cog):
    """招待リンク自動削除機能"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data_dir = Path(os.getcwd()) / "data"
        self.data_dir.mkdir(exist_ok=True)

        self._session: Optional[aiohttp.ClientSession] = None
        self._url_cache: deque[str] = deque(maxlen=1000)  # キャッシュの最大サイズを1000に設定

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

        # メインDB
        async with aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        guild_id BIGINT PRIMARY KEY,
                        anti_invite_enabled TINYINT NOT NULL DEFAULT 0
                    )
                """)
                await conn.commit()

        # 除外リストDB
        async with aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS whitelist (
                        guild_id BIGINT,
                        channel_id BIGINT,
                        PRIMARY KEY (guild_id, channel_id)
                    )
                """)
                await conn.commit()

    async def set_setting(self, guild_id: int, enabled: bool) -> None:
        """サーバーごとの設定を保存"""
        async with aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT INTO settings (guild_id, anti_invite_enabled) VALUES (%s, %s) ON DUPLICATE KEY UPDATE anti_invite_enabled = VALUES(anti_invite_enabled)",
                    (guild_id, int(enabled))
                )
                await conn.commit()

    async def get_setting(self, guild_id: int) -> bool:
        """サーバーごとの設定を取得"""
        async with aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT anti_invite_enabled FROM settings WHERE guild_id = %s",
                    (guild_id,)
                )
                row = await cursor.fetchone()
                return bool(row[0]) if row else False

    async def contains_invite(self, content: str) -> bool:
        # 直接の招待リンクチェック
        if any(pattern in content.lower() for pattern in INVITE_PATTERNS):
            return True

        # URLの抽出
        urls = re.findall(r"(https?://\S+)", content)
        if not urls:
            return False

        if not self._session:
            self._session = aiohttp.ClientSession()

        for url in urls:
            try:
                parsed = urlparse(url)
                if not parsed.hostname:
                    continue

                hostname = parsed.hostname.lower()
                if hostname not in URL_SHORTENERS:
                    continue

                # キャッシュチェック
                if url in self._url_cache:
                    return True

                # 短縮URLの展開
                try:
                    async with self._session.head(
                        url,
                        allow_redirects=True,
                        timeout=5
                    ) as response:
                        final_url = str(response.url)
                except Exception:
                    async with self._session.get(
                        url,
                        allow_redirects=True,
                        timeout=5
                    ) as response:
                        final_url = str(response.url)

                if any(pattern in final_url.lower() for pattern in INVITE_PATTERNS):
                    self._url_cache.append(url)  # キャッシュに追加
                    return True

            except Exception:
                continue

        return False

    @discord.app_commands.command(
        name="anti-invite",
        description="Discord招待リンクの自動削除を設定します。（デフォルトはdisable）"
    )
    @discord.app_commands.describe(action="設定する値（enable または disable）")
    @discord.app_commands.choices(action=[
        discord.app_commands.Choice(name="enable", value="enable"),
        discord.app_commands.Choice(name="disable", value="disable")
    ])
    async def anti_invite(self, interaction: discord.Interaction, action: str) -> None:
        """招待リンク自動削除の有効/無効を設定"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
            return

        if not interaction.guild:
            await interaction.response.send_message(GUILD_ONLY_MESSAGE, ephemeral=True)
            return

        enabled = action.lower() == "enable"
        await self.set_setting(interaction.guild.id, enabled)

        embed = discord.Embed(
            title="Anti-Invite設定",
            description=f"このサーバーでの招待リンク自動削除は **{'有効' if enabled else '無効'}** になりました。",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.app_commands.command(
        name="anti-invite-setting",
        description="禁止対象としないチャンネル（ホワリス）を設定します（複数指定可、最大10件）。"
    )
    async def anti_invite_setting(
        self,
        interaction: discord.Interaction,
        channel_1: Optional[discord.TextChannel] = None,
        channel_2: Optional[discord.TextChannel] = None,
        channel_3: Optional[discord.TextChannel] = None,
        channel_4: Optional[discord.TextChannel] = None,
        channel_5: Optional[discord.TextChannel] = None,
        channel_6: Optional[discord.TextChannel] = None,
        channel_7: Optional[discord.TextChannel] = None,
        channel_8: Optional[discord.TextChannel] = None,
        channel_9: Optional[discord.TextChannel] = None,
        channel_10: Optional[discord.TextChannel] = None
    ) -> None:
        """ホワリスチャンネルの設定"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
            return

        if not interaction.guild:
            await interaction.response.send_message(GUILD_ONLY_MESSAGE, ephemeral=True)
            return

        channels = [
            ch.id for ch in [
                channel_1, channel_2, channel_3, channel_4, channel_5,
                channel_6, channel_7, channel_8, channel_9, channel_10
            ]
            if ch and ch.guild.id == interaction.guild.id
        ]

        async with aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM whitelist WHERE guild_id = %s",
                    (interaction.guild.id,)
                )
                if channels:
                    await cursor.executemany(
                        "INSERT INTO whitelist (guild_id, channel_id) VALUES (%s, %s)",
                        [(interaction.guild.id, ch_id) for ch_id in channels]
                    )
                await conn.commit()

        if channels:
            desc = "以下のチャンネルで招待リンクの自動削除が無効化されました。\n" + \
                "\n".join([f"<#{ch_id}>" for ch_id in channels])
            title = "ホワリス設定完了"
        else:
            desc = "全てのチャンネルの無効化設定を解除しました。"
            title = "ホワリス解除完了"

        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return

        if not await self.get_setting(message.guild.id):
            return

        async with aiomysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT channel_id FROM whitelist WHERE guild_id = %s",
                    (message.guild.id,)
                )
                whitelist_channels = [row[0] async for row in cursor]

        if message.channel.id in whitelist_channels:
            return

        if await self.contains_invite(message.content):
            try:
                await message.delete()
                warning = await message.channel.send(INVITE_WARNING)
                await asyncio.sleep(5)
                await warning.delete()
            except (discord.errors.Forbidden, Exception):
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiInvite(bot))

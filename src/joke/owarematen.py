import discord
from discord.ext import commands
import aiomysql
import uuid
import asyncio
from pathlib import Path
from typing import Final, Optional, List, Tuple, Annotated
import logging
from dotenv import load_dotenv
import os
from discord.ui import Button, View, Modal, TextInput

load_dotenv(override=True)

VERSION: Final[str] = "V1.0 by K-Nana"
SESSION_TIMEOUT: Final[int] = 3600  # 1時間（秒）
DB_HOST: Final[str] = os.getenv("DB_HOST")
DB_USER: Final[str] = os.getenv("DB_USER")
DB_PASSWORD: Final[str] = os.getenv("DB_PASSWORD")
DB_NAME: Final[str] = "owarematen"

CREATE_SESSIONS_TABLE: Final[str] = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    channel_id BIGINT, 
    guild_id BIGINT,
    theme TEXT
)
"""

CREATE_ANSWERS_TABLE: Final[str] = """
CREATE TABLE IF NOT EXISTS answers (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(255),
    user_id BIGINT,
    user_name VARCHAR(255),
    answer TEXT,
    UNIQUE(session_id, user_id)
)
"""

EMBED_COLORS: Final[dict] = {
    "start": discord.Color.blurple(),
    "success": discord.Color.green(),
    "error": discord.Color.red(),
    "notify": discord.Color.orange()
}

ERROR_MESSAGES: Final[dict] = {
    "game_in_progress": "他のゲームが進行中です。先に/owarematen-open-answersでゲームを終了してください。",
    "no_game": "ゲームが開始されていません。/owarematen-start-customで開始してください。",
    "already_answered": "既に回答済みです。一人一回のみ回答できます。",
    "db_error": "DBエラーが発生しました: {}"
}

logger = logging.getLogger(__name__)

class GameSession:
    """ゲームセッションを管理するクラス"""

    def __init__(
        self,
        session_id: str,
        channel_id: int,
        guild_id: int,
        theme: str
    ) -> None:
        self.session_id = session_id
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.theme = theme

class DiscowaremaTen(commands.Cog):

    @discord.app_commands.command(
        name="owarematen-start-custom",
        description="終われまテンをカスタムお題で開始します。"
    )
    async def owarematenstart(self, interaction: Annotated[discord.Interaction, None], theme: str) -> None:
        """カスタムお題でゲームを開始"""
        try:
            if session := await self._get_session(
                interaction.channel_id,
                interaction.guild_id
            ):
                embed = self._create_game_embed(
                    "終われまテン",
                    session,
                    color_key="error",
                    error_message=ERROR_MESSAGES["game_in_progress"]
                )
                await interaction.response.send_message(
                    embed=embed
                )
                return

            session_id = uuid.uuid4().hex
            async with aiomysql.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME
            ) as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO sessions
                        (session_id, channel_id, guild_id, theme)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            interaction.channel_id,
                            interaction.guild_id,
                            theme
                        )
                    )
                    await conn.commit()

            session = GameSession(
                session_id,
                interaction.channel_id,
                interaction.guild_id,
                theme
            )
            embed = self._create_game_embed(
                "終われまテン",
                session,
                color_key="start"
            )
            embed.add_field(
                name="回答方法",
                value="以下のボタンをクリックして回答を入力してください。",
                inline=False
            )
            embed.add_field(
                name="注意",
                value="このセッションは1時間後に自動で終了し回答が公開されます。",
                inline=False
            )
            await interaction.response.send_message(embed=embed, view=StartCustomView(session_id))

            self.bot.loop.create_task(
                self.auto_open(
                    session_id,
                    interaction.channel_id,
                    interaction.guild_id
                )
            )

        except Exception as e:
            logger.error("Error in start_custom: %s", e, exc_info=True)
            await interaction.response.send_message(
                ERROR_MESSAGES["db_error"].format(str(e)),
                ephemeral=True
            )
    @discord.app_commands.command(
        name="owarematen-open-answers",
        description="全員の回答を開きます。終われまテンの終了コマンドも兼ねています。"
    )
    async def open_answers(
        self,
        interaction: discord.Interaction
    ) -> None:
        """回答を公開してゲームを終了"""
        try:
            if session := await self._get_session(
                interaction.channel_id,
                interaction.guild_id
            ):
                answers = await self._get_answers(session.session_id)
                await self._clear_session(session.session_id)

                embed = self._create_game_embed(
                    "終われまテン",
                    session,
                    answers
                )
                await interaction.response.send_message(embed=embed)
            else:
                embed = self._create_game_embed(
                    "終われまテン",
                    color_key="error",
                    error_message=ERROR_MESSAGES["no_game"]
                )
                await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error("Error in open_answers: %s", e, exc_info=True)
            await interaction.response.send_message(
                ERROR_MESSAGES["db_error"].format(str(e)),
                ephemeral=True
            )


    """終われまテン機能を提供"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Cogのロード時にDBを初期化"""
        await self._init_db()

    async def _init_db(self) -> None:
        """DBを初期化"""
        async with aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME
        ) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(CREATE_SESSIONS_TABLE)
                await cursor.execute(CREATE_ANSWERS_TABLE)
                await conn.commit()

    async def _get_session(
        self,
        channel_id: int,
        guild_id: int
    ) -> Optional[GameSession]:
        async with aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME
        ) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT session_id, theme
                    FROM sessions
                    WHERE channel_id = %s AND guild_id = %s
                    """,
                    (channel_id, guild_id)
                )
                if row := await cursor.fetchone():
                    return GameSession(
                        row[0], channel_id, guild_id, row[1]
                    )
        return None

    async def _get_answers(
        self,
        session_id: str
    ) -> List[Tuple[str, str]]:
        """セッションの回答を取得"""
        async with aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME
        ) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT user_name, answer
                    FROM answers
                    WHERE session_id = %s
                    """,
                    (session_id,)
                )
                return await cursor.fetchall()

    async def _clear_session(self, session_id: str) -> None:
        """セッションをクリア"""
        async with aiomysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME
        ) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,)
                )
                await cursor.execute(
                    "DELETE FROM answers WHERE session_id = %s",
                    (session_id,)
                )
                await conn.commit()

    def _create_game_embed(
        self,
        title: str,
        session: Optional[GameSession] = None,
        answers: Optional[List[Tuple[str, str]]] = None,
        color_key: str = "success",
        error_message: Optional[str] = None
    ) -> discord.Embed:
        """ゲーム情報表示用のEmbedを作成"""
        embed = discord.Embed(
            title=title,
            description=VERSION,
            color=EMBED_COLORS[color_key]
        )

        if error_message:
            embed.add_field(
                name="エラー",
                value=error_message,
                inline=False
            )
            return embed

        if session:
            embed.add_field(
                name="お題",
                value=session.theme,
                inline=False
            )
            if answers:
                if not answers:
                    embed.add_field(
                        name="おっと。",
                        value="誰も答えていないようです...",
                        inline=False
                    )
                else:
                    for user_name, answer in answers:
                        embed.add_field(
                            name=f"{user_name}の回答",
                            value=answer,
                            inline=False
                        )
            embed.set_footer(text=f"セッションID: {session.session_id}")

        return embed

    async def auto_open(
        self,
        session_id: str,
        channel_id: int,
        guild_id: int
    ) -> None:
        await asyncio.sleep(SESSION_TIMEOUT)

        try:
            session = GameSession(
                session_id, channel_id, guild_id,
                "Unknown Theme"
            )
            async with aiomysql.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME
            ) as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT theme FROM sessions WHERE session_id = %s",
                        (session_id,)
                    )
                    if row := await cursor.fetchone():
                        session.theme = row[0]
                    else:
                        return

            answers = await self._get_answers(session_id)
            await self._clear_session(session_id)

            if channel := self.bot.get_channel(channel_id):
                embed = self._create_game_embed(
                    "自動終了: 終われまテン",
                    session,
                    answers
                )
                await channel.send(embed=embed)

        except Exception as e:
            logger.error("Error in auto_open: %s", e, exc_info=True)

class AnswerModal(Modal):
    def __init__(self, session_id: str):
        super().__init__(title="回答を入力してください")
        self.session_id = session_id
        self.answer_input = TextInput(
            label="回答",
            placeholder="ここに回答を入力してください",
            required=True
        )
        self.add_item(self.answer_input)

    async def on_submit(self, interaction: discord.Interaction):
        answer = self.answer_input.value
        try:
            async with aiomysql.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME
            ) as conn:
                async with conn.cursor() as cursor:
                    # 回答済みチェック
                    await cursor.execute(
                        """
                        SELECT 1 FROM answers
                        WHERE session_id = %s AND user_id = %s
                        """,
                        (self.session_id, interaction.user.id)
                    )
                    if await cursor.fetchone():
                        await interaction.response.send_message(
                            ERROR_MESSAGES["already_answered"],
                            ephemeral=True
                        )
                        return

                    # 回答を保存
                    await cursor.execute(
                        """
                        INSERT INTO answers
                        (session_id, user_id, user_name, answer)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            self.session_id,
                            interaction.user.id,
                            interaction.user.name,
                            answer
                        )
                    )
                    await conn.commit()

                    # 回答数を取得
                    await cursor.execute(
                        """
                        SELECT COUNT(*) FROM answers
                        WHERE session_id = %s
                        """,
                        (self.session_id,)
                    )
                    count = (await cursor.fetchone())[0]

            # 回答完了通知
            await interaction.response.send_message(
                f"{answer}で回答しました",
                ephemeral=True
            )

            # 全体通知
            notify_embed = discord.Embed(
                title="回答受付",
                description=f"{interaction.user.name}が回答しました。",
                color=EMBED_COLORS["notify"]
            )
            notify_embed.add_field(
                name="現在の回答数",
                value=str(count),
                inline=False
            )
            notify_embed.set_footer(
                text=f"セッションID: {self.session_id}"
            )
            await interaction.channel.send(embed=notify_embed)

        except aiomysql.IntegrityError:
            await interaction.response.send_message(
                ERROR_MESSAGES["already_answered"],
                ephemeral=True
            )
        except Exception as e:
            logger.error("Error in AnswerModal: %s", e, exc_info=True)
            await interaction.response.send_message(
                ERROR_MESSAGES["db_error"].format(str(e)),
                ephemeral=True
            )

class StartCustomView(View):
    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id
        self.add_item(Button(label="回答する", style=discord.ButtonStyle.primary, custom_id="answer_button"))

    @discord.ui.button(label="回答する", style=discord.ButtonStyle.primary)
    async def answer_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(AnswerModal(self.session_id))




async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DiscowaremaTen(bot))

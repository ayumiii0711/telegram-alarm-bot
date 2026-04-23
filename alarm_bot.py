"""
Telegram Alarm Bot (python-telegram-bot + asyncio + SQLite)

使い方の概要:
1) /start でメニューを開く
2) 「⏰ アラームを追加」ボタンを押す
3) 例の形式で日時とメモを入力する
   - 明日7時 | 朝会
   - 2026-04-23 08:00 | 病院
   - 2026-04-23 00:00 UTC | 海外会議
   - 2026-04-22 20:00 EST | NYとの打合せ

ポイント:
- すべてJST(日本時間)に変換して保存・通知
- 通知時にスヌーズボタンを表示
- SQLiteでユーザーごとに管理
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------------
# 基本設定
# -----------------------------
DB_PATH = os.getenv("DB_PATH", "alarms.db")
JST = ZoneInfo("Asia/Tokyo")
INPUT_WAIT_FLAG = "awaiting_alarm_input"
DATE_SEPARATOR = "|"

# タイムゾーン略称の簡易マップ
# 注意: ESTは夏時間を考慮しない固定UTC-5として扱う
TZ_MAP = {
    "JST": JST,
    "UTC": UTC,
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
}


@dataclass
class ParsedAlarmInput:
    due_jst: datetime
    memo: str


def menu_keyboard() -> InlineKeyboardMarkup:
    """メインメニューのInlineKeyboardを返す。"""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏰ アラームを追加", callback_data="menu:new")],
            [InlineKeyboardButton("📋 アラーム一覧", callback_data="menu:list")],
            [InlineKeyboardButton("ℹ️ 使い方", callback_data="menu:help")],
        ]
    )


def snooze_keyboard(alarm_id: int) -> InlineKeyboardMarkup:
    """通知時に表示するスヌーズ用ボタン。"""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("5分後", callback_data=f"snooze:{alarm_id}:5"),
                InlineKeyboardButton("10分後", callback_data=f"snooze:{alarm_id}:10"),
                InlineKeyboardButton("30分後", callback_data=f"snooze:{alarm_id}:30"),
            ],
            [InlineKeyboardButton("✅ 完了", callback_data=f"done:{alarm_id}")],
        ]
    )


def format_jst(dt: datetime) -> str:
    """JST日時をわかりやすい文字列に変換。"""
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def now_jst() -> datetime:
    """現在時刻(JST)を返す。"""
    return datetime.now(JST)


def parse_user_datetime(raw: str, now: datetime) -> datetime:
    """
    ユーザー入力から日時を解析して、JSTのaware datetimeを返す。

    許可形式:
    - 日本語相対: 今日7時 / 明日7時30分 / 明後日21時
    - 絶対日時: 2026-04-23 08:00
    - タイムゾーン付き: 2026-04-23 00:00 UTC / 2026-04-22 20:00 EST
    """
    text = raw.strip()

    # 1) 日本語の相対日時
    # 例: 明日7時 / 明後日 21時30分
    m_rel = re.fullmatch(
        r"(今日|明日|明後日)\s*([0-2]?\d)時(?:\s*([0-5]?\d)分?)?",
        text,
    )
    if m_rel:
        day_word = m_rel.group(1)
        hour = int(m_rel.group(2))
        minute = int(m_rel.group(3)) if m_rel.group(3) else 0

        if hour > 23:
            raise ValueError("時間は0〜23時で入力してください。")

        offset_days = {"今日": 0, "明日": 1, "明後日": 2}[day_word]
        target_date = (now + timedelta(days=offset_days)).date()
        return datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            hour,
            minute,
            tzinfo=JST,
        )

    # 2) 絶対日時 + 任意のTZ
    # 例: 2026-04-23 08:00
    # 例: 2026-04-23 00:00 UTC
    # 例: 2026-04-22 20:00 EST
    m_abs = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})\s+([0-2]\d):([0-5]\d)(?:\s+(UTC|JST|EST|EDT))?",
        text,
        flags=re.IGNORECASE,
    )
    if m_abs:
        year, month, day = map(int, m_abs.group(1, 2, 3))
        hour, minute = map(int, m_abs.group(4, 5))
        tz_name = m_abs.group(6).upper() if m_abs.group(6) else "JST"

        if tz_name not in TZ_MAP:
            raise ValueError("タイムゾーンは JST / UTC / EST / EDT のみ対応です。")

        src_tz = TZ_MAP[tz_name]
        dt_src = datetime(year, month, day, hour, minute, tzinfo=src_tz)
        return dt_src.astimezone(JST)

    raise ValueError(
        "日時の形式が正しくありません。例: 明日7時 | 2026-04-23 08:00 | 2026-04-23 00:00 UTC"
    )


def parse_alarm_input(text: str, now: datetime) -> ParsedAlarmInput:
    """
    入力文字列を「日時」と「メモ」に分解する。

    形式:
    - <日時>
    - <日時> | <メモ>
    """
    if DATE_SEPARATOR in text:
        dt_part, memo_part = text.split(DATE_SEPARATOR, 1)
        memo = memo_part.strip()
    else:
        dt_part = text
        memo = ""

    due = parse_user_datetime(dt_part.strip(), now)
    if due <= now:
        raise ValueError("過去時刻は設定できません。未来の時刻を入力してください。")

    return ParsedAlarmInput(due_jst=due, memo=memo)


async def init_db(db_path: str) -> None:
    """SQLite初期化。"""
    # 例: DB_PATH=/data/alarms.db のような場合、親ディレクトリを先に作る
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                due_at_jst TEXT NOT NULL,
                memo TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alarms_user_status_due
            ON alarms (user_id, status, due_at_jst)
            """
        )
        await db.commit()


async def create_alarm(
    db_path: str, user_id: int, chat_id: int, due_jst: datetime, memo: str
) -> int:
    """アラームをDBに保存してIDを返す。"""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO alarms (user_id, chat_id, due_at_jst, memo, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                user_id,
                chat_id,
                due_jst.isoformat(),
                memo,
                now_jst().isoformat(),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_alarm(db_path: str, alarm_id: int) -> Optional[dict]:
    """alarm_idでアラーム1件を取得。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_pending_alarms(db_path: str, user_id: int) -> list[dict]:
    """ユーザーの未実行アラーム一覧を取得。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM alarms
            WHERE user_id = ? AND status = 'pending'
            ORDER BY due_at_jst ASC
            LIMIT 30
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def list_all_pending_for_restore(db_path: str) -> list[dict]:
    """起動時復元用: pendingアラームを全件取得。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM alarms
            WHERE status = 'pending'
            ORDER BY due_at_jst ASC
            """
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def update_status(db_path: str, alarm_id: int, status: str) -> None:
    """アラームの状態を更新。"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE alarms SET status = ? WHERE id = ?", (status, alarm_id))
        await db.commit()


def schedule_alarm_job(app: Application, alarm_id: int, due_jst: datetime) -> None:
    """
    JobQueueに通知ジョブを登録する。
    同じアラームIDのジョブがあれば再登録前に削除する。
    """
    job_name = f"alarm:{alarm_id}"
    for old_job in app.job_queue.get_jobs_by_name(job_name):
        old_job.schedule_removal()

    now_utc = datetime.now(UTC)
    due_utc = due_jst.astimezone(UTC)
    delay = (due_utc - now_utc).total_seconds()
    when = max(delay, 1)  # 過去分は1秒後に即通知扱い

    app.job_queue.run_once(
        alarm_notify_job,
        when=when,
        data={"alarm_id": alarm_id},
        name=job_name,
    )


async def restore_jobs(app: Application) -> None:
    """再起動時にDBからpendingアラームを復元してスケジュール。"""
    alarms = await list_all_pending_for_restore(DB_PATH)
    restored = 0
    for alarm in alarms:
        due = datetime.fromisoformat(alarm["due_at_jst"]).astimezone(JST)
        schedule_alarm_job(app, alarm["id"], due)
        restored += 1
    logging.info("Restored %s alarm jobs from DB.", restored)


async def alarm_notify_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """アラーム発火時にユーザーへ通知を送るジョブ。"""
    alarm_id = context.job.data["alarm_id"]
    alarm = await get_alarm(DB_PATH, alarm_id)
    if not alarm:
        return
    if alarm["status"] != "pending":
        return

    due_jst = datetime.fromisoformat(alarm["due_at_jst"]).astimezone(JST)
    memo = alarm["memo"].strip()
    memo_text = f"\nメモ: {memo}" if memo else "\nメモ: (なし)"
    text = (
        "⏰ アラームです！\n"
        f"設定時刻: {format_jst(due_jst)}"
        f"{memo_text}\n\n"
        "スヌーズしますか？"
    )

    await context.bot.send_message(
        chat_id=alarm["chat_id"],
        text=text,
        reply_markup=snooze_keyboard(alarm_id),
    )
    await update_status(DB_PATH, alarm_id, "triggered")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """開始メッセージとメニュー表示。"""
    context.user_data[INPUT_WAIT_FLAG] = False
    await update.effective_message.reply_text(
        "こんにちは。直感的に使えるアラームBotです。\n"
        "下のボタンから操作してください。",
        reply_markup=menu_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """使い方の説明。"""
    await update.effective_message.reply_text(
        "使い方:\n"
        "1) 「⏰ アラームを追加」を押す\n"
        "2) 次の形式で送信\n"
        "   - 明日7時 | 朝会\n"
        "   - 2026-04-23 08:00 | 病院\n"
        "   - 2026-04-23 00:00 UTC | 海外会議\n"
        "   - 2026-04-22 20:00 EST | NYとの打合せ\n"
        "3) 通知時にスヌーズボタンを押せます",
        reply_markup=menu_keyboard(),
    )


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """アラーム入力待ち状態にする。"""
    context.user_data[INPUT_WAIT_FLAG] = True
    await update.effective_message.reply_text(
        "アラームを設定します。日時とメモを入力してください。\n"
        "形式: <日時> | <メモ>\n"
        "例: 明日7時 | 朝の運動"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """入力待ちをキャンセル。"""
    context.user_data[INPUT_WAIT_FLAG] = False
    await update.effective_message.reply_text(
        "入力待ちをキャンセルしました。", reply_markup=menu_keyboard()
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ユーザーのpendingアラーム一覧を表示。"""
    user_id = update.effective_user.id
    alarms = await list_pending_alarms(DB_PATH, user_id)
    if not alarms:
        await update.effective_message.reply_text(
            "未実行のアラームはありません。", reply_markup=menu_keyboard()
        )
        return

    lines = ["📋 未実行アラーム一覧:"]
    keyboard_rows = []
    for a in alarms:
        due = datetime.fromisoformat(a["due_at_jst"]).astimezone(JST)
        memo = a["memo"].strip() or "(メモなし)"
        lines.append(f"#{a['id']} {format_jst(due)} / {memo}")
        keyboard_rows.append(
            [InlineKeyboardButton(f"削除 #{a['id']}", callback_data=f"del:{a['id']}")]
        )
    keyboard_rows.append([InlineKeyboardButton("メニューへ", callback_data="menu:home")])

    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard_rows)
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """通常テキスト受信時の処理。入力待ち中ならアラーム登録する。"""
    if not context.user_data.get(INPUT_WAIT_FLAG, False):
        await update.effective_message.reply_text(
            "メニューから操作してください。", reply_markup=menu_keyboard()
        )
        return

    text = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        parsed = parse_alarm_input(text, now_jst())
    except ValueError as e:
        await update.effective_message.reply_text(
            f"入力エラー: {e}\n"
            "もう一度入力してください。\n"
            "例: 明日7時 | 朝会"
        )
        return

    alarm_id = await create_alarm(
        DB_PATH, user_id=user_id, chat_id=chat_id, due_jst=parsed.due_jst, memo=parsed.memo
    )
    schedule_alarm_job(context.application, alarm_id, parsed.due_jst)
    context.user_data[INPUT_WAIT_FLAG] = False

    memo_disp = parsed.memo if parsed.memo else "(メモなし)"
    await update.effective_message.reply_text(
        "✅ アラームを登録しました\n"
        f"ID: #{alarm_id}\n"
        f"時刻: {format_jst(parsed.due_jst)}\n"
        f"メモ: {memo_disp}",
        reply_markup=menu_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ボタン押下時の処理。"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu:new":
        context.user_data[INPUT_WAIT_FLAG] = True
        await query.message.reply_text(
            "日時を入力してください。\n"
            "形式: <日時> | <メモ>\n"
            "例: 2026-04-23 08:00 | 病院"
        )
        return

    if data == "menu:list":
        # /list と同じ処理を使いたいので、query.message経由で直接表示
        alarms = await list_pending_alarms(DB_PATH, user_id)
        if not alarms:
            await query.message.reply_text("未実行のアラームはありません。", reply_markup=menu_keyboard())
            return

        lines = ["📋 未実行アラーム一覧:"]
        keyboard_rows = []
        for a in alarms:
            due = datetime.fromisoformat(a["due_at_jst"]).astimezone(JST)
            memo = a["memo"].strip() or "(メモなし)"
            lines.append(f"#{a['id']} {format_jst(due)} / {memo}")
            keyboard_rows.append(
                [InlineKeyboardButton(f"削除 #{a['id']}", callback_data=f"del:{a['id']}")]
            )
        keyboard_rows.append([InlineKeyboardButton("メニューへ", callback_data="menu:home")])
        await query.message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )
        return

    if data in {"menu:help", "menu:home"}:
        context.user_data[INPUT_WAIT_FLAG] = False
        await query.message.reply_text(
            "操作メニューです。必要なボタンを選んでください。", reply_markup=menu_keyboard()
        )
        return

    if data.startswith("snooze:"):
        # 形式: snooze:<alarm_id>:<minutes>
        _, alarm_id_str, minutes_str = data.split(":")
        alarm_id = int(alarm_id_str)
        minutes = int(minutes_str)

        src_alarm = await get_alarm(DB_PATH, alarm_id)
        if not src_alarm:
            await query.message.reply_text("元のアラームが見つかりません。")
            return
        if src_alarm["user_id"] != user_id:
            await query.message.reply_text("このアラームは操作できません。")
            return

        due = now_jst() + timedelta(minutes=minutes)
        memo = src_alarm["memo"].strip()
        new_alarm_id = await create_alarm(
            DB_PATH,
            user_id=src_alarm["user_id"],
            chat_id=src_alarm["chat_id"],
            due_jst=due,
            memo=memo,
        )
        schedule_alarm_job(context.application, new_alarm_id, due)
        await update_status(DB_PATH, alarm_id, "snoozed")

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"😴 {minutes}分後に再通知します。\n"
            f"新しいアラームID: #{new_alarm_id}\n"
            f"時刻: {format_jst(due)}"
        )
        return

    if data.startswith("done:"):
        _, alarm_id_str = data.split(":")
        alarm_id = int(alarm_id_str)
        alarm = await get_alarm(DB_PATH, alarm_id)
        if not alarm:
            await query.message.reply_text("アラームが見つかりません。")
            return
        if alarm["user_id"] != user_id:
            await query.message.reply_text("このアラームは操作できません。")
            return

        await update_status(DB_PATH, alarm_id, "done")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ アラーム #{alarm_id} を完了にしました。")
        return

    if data.startswith("del:"):
        _, alarm_id_str = data.split(":")
        alarm_id = int(alarm_id_str)
        alarm = await get_alarm(DB_PATH, alarm_id)
        if not alarm:
            await query.message.reply_text("アラームが見つかりません。")
            return
        if alarm["user_id"] != user_id:
            await query.message.reply_text("このアラームは操作できません。")
            return

        # 予約済みジョブを削除
        job_name = f"alarm:{alarm_id}"
        for job in context.application.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

        await update_status(DB_PATH, alarm_id, "cancelled")
        await query.message.reply_text(f"🗑️ アラーム #{alarm_id} を削除しました。")
        return


async def post_init(app: Application) -> None:
    """Application起動後の初期処理。"""
    await init_db(DB_PATH)
    await restore_jobs(app)


def build_application(token: str) -> Application:
    """Telegramアプリケーションを構築。"""
    app = Application.builder().token(token).post_init(post_init).build()

    # コマンド
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # ボタン
    app.add_handler(CallbackQueryHandler(handle_callback))

    # テキスト入力
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


def main() -> None:
    """エントリーポイント。"""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    # Railway設定ミスを吸収するため、よくある誤記 BOI_TOKEN も許容する
    token = (
        os.getenv("BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("BOI_TOKEN")
    )
    if token:
        token = token.strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN (または TELEGRAM_BOT_TOKEN) が未設定です。"
            "環境変数にBotトークンを設定してください。"
        )

    app = build_application(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

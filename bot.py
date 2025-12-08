# bot.py
import logging

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, HOST_PIN
from db import (
    init_db,
    get_user_by_telegram_id,
    create_user,
    update_user_role,
    get_participant_count,
    get_host,
    get_nominations,
    get_nomination_by_id,
    get_participant_videos_count,
    create_video,
    get_participant_videos_for_nomination,
    get_connection,
    ROLE_PARTICIPANT,
    ROLE_HOST,
)
from vote_logic import (
    get_videos_for_nomination_excluding_user,
    save_vote,
    get_vote_stats,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==================== /start ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id

    db_user = get_user_by_telegram_id(tg_id)

    # Новый пользователь — выбор роли
    if not db_user:
        keyboard = [
            [InlineKeyboardButton("Я участник", callback_data="role_participant")],
            [InlineKeyboardButton("Я ведущий", callback_data="role_host")],
        ]
        await update.message.reply_text(
            "Добро пожаловать в бота конкурса «Золотая шишка».\n"
            "Выберите вашу роль:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Уже зарегистрирован: показываем роль и КНОПКИ
    role = db_user["role"]
    role_ru = "Участник" if role == ROLE_PARTICIPANT else "Ведущий"

    text = (
        f"Привет, {db_user['display_name']}!\n"
        f"Вы уже зарегистрированы как: {role_ru}."
    )

    keyboard = []

    if role == ROLE_PARTICIPANT:
        keyboard.append(
            [InlineKeyboardButton("➕ Добавить видео", callback_data="action_add_video")]
        )

    if role == ROLE_HOST:
        keyboard.append(
            [InlineKeyboardButton("🎛 Панель ведущего", callback_data="action_host_panel")]
        )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


# ==================== Выбор роли (callback) ====================

async def role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    tg_id = user.id
    full_name = user.full_name or user.username or str(tg_id)

    data = query.data
    db_user = get_user_by_telegram_id(tg_id)

    # ---- Участник ----
    if data == "role_participant":
        current_count = get_participant_count()
        if (not db_user) and current_count >= 6:
            await query.edit_message_text(
                "Лимит участников (6 человек) уже заполнен.\n"
                "Обратитесь к организатору."
            )
            return

        if db_user:
            update_user_role(tg_id, ROLE_PARTICIPANT)
        else:
            create_user(tg_id, ROLE_PARTICIPANT, full_name)

        keyboard = [
            [InlineKeyboardButton("➕ Добавить видео", callback_data="action_add_video")]
        ]

        await query.edit_message_text(
            "Вы зарегистрированы как Участник.\n"
            "Ваши данные сохранены в БД.\n\n"
            "Используйте кнопку ниже, чтобы прикрепить видео к номинациям.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # ---- Ведущий ----
    if data == "role_host":
        existing_host = get_host()
        if existing_host and (not db_user or existing_host["telegram_id"] != tg_id):
            await query.edit_message_text(
                "Роль ведущего уже занята.\n"
                "Обратитесь к организатору, если это ошибка."
            )
            return

        context.user_data["awaiting_host_pin"] = True
        await query.edit_message_text(
            "Вы выбрали роль ведущего.\n"
            "Пожалуйста, отправьте PIN-код ведущего одним сообщением."
        )


# ==================== Кнопки действий (участник / ведущий) ====================

async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    tg_id = user.id
    db_user = get_user_by_telegram_id(tg_id)

    if not db_user:
        await query.edit_message_text(
            "Сначала зарегистрируйтесь через /start и выберите роль."
        )
        return

    # Кнопка участника: Добавить видео
    if data == "action_add_video":
        if db_user["role"] != ROLE_PARTICIPANT:
            await query.edit_message_text("Эта кнопка доступна только участникам.")
            return
        # Переиспользуем логику выбор номинации для добавления видео
        await send_add_video_nomination_menu(query.message, db_user, context)
        return

    # Кнопка ведущего: Панель ведущего
    if data == "action_host_panel":
        if db_user["role"] != ROLE_HOST:
            await query.edit_message_text("Эта кнопка доступна только ведущему.")
            return
        await send_host_panel(query.message, db_user, context)
        return


# ==================== Добавление видео (участник) ====================

async def send_add_video_nomination_menu(message, db_user, context: ContextTypes.DEFAULT_TYPE):
    nominations = get_nominations()

    keyboard_rows = []
    row = []
    for nom in nominations:
        btn = InlineKeyboardButton(
            f"{nom['id']}. {nom['name']}",
            callback_data=f"add_video_nom_{nom['id']}",
        )
        row.append(btn)
        if len(row) == 3:
            keyboard_rows.append(row)
            row = []
    if row:
        keyboard_rows.append(row)

    await message.reply_text(
        "Выберите номинацию, для которой хотите добавить видео.\n"
        "Для каждой номинации можно добавить от 1 до 3 видео.",
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def add_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остаётся как запасной вариант через слэш, но не обязателен."""
    user = update.effective_user
    tg_id = user.id
    db_user = get_user_by_telegram_id(tg_id)

    if not db_user:
        await update.message.reply_text(
            "Сначала зарегистрируйтесь через /start и выберите роль."
        )
        return

    if db_user["role"] != ROLE_PARTICIPANT:
        await update.message.reply_text(
            "Команда /add_video доступна только участникам."
        )
        return

    await send_add_video_nomination_menu(update.message, db_user, context)


async def add_video_nomination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    tg_id = user.id
    db_user = get_user_by_telegram_id(tg_id)

    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text(
            "Только участники могут добавлять видео.\n"
            "Используйте /start, чтобы выбрать роль."
        )
        return

    data = query.data  # "add_video_nom_X"
    try:
        nomination_id = int(data.replace("add_video_nom_", ""))
    except ValueError:
        await query.edit_message_text("Ошибка выбора номинации.")
        return

    nomination = get_nomination_by_id(nomination_id)
    if not nomination:
        await query.edit_message_text("Номинация не найдена.")
        return

    participant_id = db_user["id"]
    existing_count = get_participant_videos_count(participant_id, nomination_id)

    if existing_count >= 3:
        videos = get_participant_videos_for_nomination(participant_id, nomination_id)
        text_lines = [
            f"У вас уже 3 видео в номинации «{nomination['name']}».",
            "",
            "Ваши видео:",
        ]
        for v in videos:
            text_lines.append(f"- {v['title']} ({v['url']})")
        await query.edit_message_text("\n".join(text_lines))
        return

    context.user_data["awaiting_video_title_nomination_id"] = nomination_id
    context.user_data.pop("awaiting_video_url_nomination_id", None)
    context.user_data.pop("temp_video_title", None)

    await query.edit_message_text(
        f"Номинация: «{nomination['name']}».\n"
        f"Сейчас у вас {existing_count} видео.\n\n"
        "Отправьте сообщение с НАЗВАНИЕМ видео."
    )


# ==================== Панель ведущего ====================

async def send_host_panel(message, db_user, context: ContextTypes.DEFAULT_TYPE):
    nominations = get_nominations()
    keyboard = [
        [InlineKeyboardButton(f"{nom['id']}. {nom['name']}", callback_data=f"host_nom_{nom['id']}")]
        for nom in nominations
    ]

    await message.reply_text(
        "Выберите номинацию для управления голосованием:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def host_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запасная команда через слэш, не обязательна."""
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user or db_user["role"] != ROLE_HOST:
        await update.message.reply_text("Эта команда доступна только ведущему.")
        return

    await send_host_panel(update.message, db_user, context)


async def host_nomination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user or db_user["role"] != ROLE_HOST:
        await query.edit_message_text("Только ведущий может управлять голосованием.")
        return

    data = query.data
    nomination_id = int(data.replace("host_nom_", ""))
    nomination = get_nomination_by_id(nomination_id)
    keyboard = [
        [InlineKeyboardButton("▶️ Запустить голосование", callback_data=f"start_vote_{nomination_id}")],
        [InlineKeyboardButton("🛑 Закрыть голосование", callback_data=f"stop_vote_{nomination_id}")],
    ]
    await query.edit_message_text(
        f"Номинация: {nomination['name']}\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ==================== Запуск голосования (ведущий) ====================

async def start_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    nomination_id = int(query.data.replace("start_vote_", ""))
    nomination = get_nomination_by_id(nomination_id)

    # список участников
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM users WHERE role = 'participant'")
        participants = [dict(r) for r in cur.fetchall()]

    for p in participants:
        videos = get_videos_for_nomination_excluding_user(nomination_id, p["id"])
        if not videos:
            # этому участнику нечего голосовать
            continue

        buttons = [
            [InlineKeyboardButton(v["title"], callback_data=f"vote_{nomination_id}_{v['id']}")]
            for v in videos
        ]
        try:
            await context.bot.send_message(
                chat_id=p["telegram_id"],
                text=f"🎥 Голосование за номинацию *{nomination['name']}*\nВыберите одно видео:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение {p['display_name']}: {e}")

    await query.edit_message_text(
        f"Голосование по номинации «{nomination['name']}» запущено.\n"
        "Участникам отправлены варианты.",
    )


# ==================== Голос участника ====================

async def participant_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")  # ["vote", nominationid, videoid]
    nomination_id, video_id = int(data[1]), int(data[2])
    db_user = get_user_by_telegram_id(query.from_user.id)

    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text("Голосовать могут только участники.")
        return

    save_vote(nomination_id, db_user["id"], video_id)
    await query.edit_message_text("✅ Ваш голос принят! Спасибо.")


# ==================== Закрытие голосования (ведущий, результаты) ====================

async def stop_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    nomination_id = int(query.data.replace("stop_vote_", ""))
    nomination = get_nomination_by_id(nomination_id)

    stats = get_vote_stats(nomination_id)
    if not stats:
        await query.edit_message_text(f"Нет видео в номинации «{nomination['name']}».")
        return

    lines = [f"🏆 Результаты номинации *{nomination['name']}*:"]

    max_votes = 0
    winners = []
    for s in stats:
        lines.append(f"- {s['title']} ({s['author_name']}) — {s['votes_count']} голос(ов)")
        if s["votes_count"] > max_votes:
            max_votes = s["votes_count"]
            winners = [s]
        elif s["votes_count"] == max_votes:
            winners.append(s)

    winner_text = "\n".join(
        [f"🥇 {w['title']} — {w['author_name']}" for w in winners]
    )
    lines.append("\n" + winner_text)

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ==================== Тексты: PIN ведущего + шаги добавления видео ====================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id
    text = (update.message.text or "").strip()

    # ---- Ввод PIN для ведущего ----
    if context.user_data.get("awaiting_host_pin"):
        if text == HOST_PIN:
            context.user_data["awaiting_host_pin"] = False

            db_user = get_user_by_telegram_id(tg_id)
            full_name = user.full_name or user.username or str(tg_id)

            if db_user:
                update_user_role(tg_id, ROLE_HOST)
            else:
                create_user(tg_id, ROLE_HOST, full_name)

            keyboard = [
                [InlineKeyboardButton("🎛 Панель ведущего", callback_data="action_host_panel")]
            ]

            await update.message.reply_text(
                "PIN верный.\n"
                "Вы зарегистрированы как Ведущий.\n"
                "Данные сохранены в БД.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await update.message.reply_text("Неверный PIN. Попробуйте ещё раз.")
        return

    db_user = get_user_by_telegram_id(tg_id)

    # ---- Шаг 1: название видео ----
    nomination_id_for_title = context.user_data.get("awaiting_video_title_nomination_id")
    if nomination_id_for_title is not None and db_user and db_user["role"] == ROLE_PARTICIPANT:
        context.user_data["temp_video_title"] = text
        context.user_data["awaiting_video_title_nomination_id"] = None
        context.user_data["awaiting_video_url_nomination_id"] = nomination_id_for_title

        nomination = get_nomination_by_id(nomination_id_for_title)
        await update.message.reply_text(
            f"Название сохранено: «{text}».\n\n"
            "Теперь отправьте ССЫЛКУ на это видео (TikTok / YouTube / Reels и т.п.)."
        )
        return

    # ---- Шаг 2: ссылка видео ----
    nomination_id_for_url = context.user_data.get("awaiting_video_url_nomination_id")
    if nomination_id_for_url is not None and db_user and db_user["role"] == ROLE_PARTICIPANT:
        title = context.user_data.get("temp_video_title")
        if not title:
            context.user_data["awaiting_video_url_nomination_id"] = None
            await update.message.reply_text(
                "Не удалось найти сохранённое название. Начните заново через кнопку «Добавить видео»."
            )
            return

        participant_id = db_user["id"]
        nomination = get_nomination_by_id(nomination_id_for_url)

        video = create_video(
            nomination_id=nomination_id_for_url,
            participant_id=participant_id,
            title=title,
            url=text,
        )

        context.user_data["awaiting_video_url_nomination_id"] = None
        context.user_data["temp_video_title"] = None

        count_after = get_participant_videos_count(participant_id, nomination_id_for_url)

        await update.message.reply_text(
            f"Видео добавлено в номинацию «{nomination['name']}».\n"
            f"Название: «{video['title']}»\n"
            f"Ссылка: {video['url']}\n\n"
            f"Сейчас у вас {count_after} из 3 возможных видео в этой номинации.\n"
            "Чтобы добавить ещё видео, снова нажмите кнопку «Добавить видео»."
        )
        return

    # ---- Прочие тексты ----
    await update.message.reply_text(
        "Сообщение получено.\n"
        "Используйте /start, чтобы увидеть доступные вам кнопки."
    )


# ==================== main ====================

def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды (запасные)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_video", add_video_command))
    app.add_handler(CommandHandler("host_panel", host_panel))

    # Callback-и
    app.add_handler(CallbackQueryHandler(role_callback, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(action_callback, pattern="^action_"))
    app.add_handler(CallbackQueryHandler(add_video_nomination_callback, pattern="^add_video_nom_"))
    app.add_handler(CallbackQueryHandler(host_nomination_callback, pattern="^host_nom_"))
    app.add_handler(CallbackQueryHandler(start_vote_callback, pattern="^start_vote_"))
    app.add_handler(CallbackQueryHandler(stop_vote_callback, pattern="^stop_vote_"))
    app.add_handler(CallbackQueryHandler(participant_vote_callback, pattern="^vote_"))

    # Тексты
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Бот запущен. Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
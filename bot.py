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
    delete_video_for_participant,
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
    get_all_videos_with_meta,
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

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING) 

logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4000  # запас (лимит Telegram ~4096)

def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """
    Разбивает длинный текст на части <= limit, стараясь резать по строкам.
    """
    lines = text.split("\n")
    parts = []
    buf = ""

    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            if buf:
                parts.append(buf)
                buf = ""
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
        buf = (buf + "\n" + line) if buf else line

    if buf:
        parts.append(buf)
    return parts


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id

    db_user = get_user_by_telegram_id(tg_id)

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

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in handler", exc_info=context.error)

async def show_participant_menu(message):
    keyboard = [
        [InlineKeyboardButton("➕ Добавить видео", callback_data="action_add_video")]
    ]
    await message.reply_text(
        "Меню участника:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_host_menu(message):
    keyboard = [
        [InlineKeyboardButton("🎛 Панель ведущего", callback_data="action_host_panel")]
    ]
    await message.reply_text(
        "Меню ведущего:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_host_panel_menu(message):
    nominations = get_nominations()
    keyboard = [
        [InlineKeyboardButton(f"{n['id']}. {n['name']}", callback_data=f"host_nom_{n['id']}")]
        for n in nominations
    ]
    keyboard.append([InlineKeyboardButton("📄 Все прикреплённые видео", callback_data="host_all_videos")])
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="back_to_main")])

    await message.reply_text(
        "Панель ведущего:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    tg_id = user.id
    full_name = user.full_name or user.username or str(tg_id)

    data = query.data
    db_user = get_user_by_telegram_id(tg_id)

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
    
    if data == "action_add_video":
        if db_user["role"] != ROLE_PARTICIPANT:
            await query.edit_message_text("Эта кнопка доступна только участникам.")
            return
        await send_add_video_nomination_menu(query.message, db_user, context)
        return

    if data == "action_host_panel":
        if db_user["role"] != ROLE_HOST:
            await query.edit_message_text("Эта кнопка доступна только ведущему.")
            return
        await send_host_panel(query.message, db_user, context)
        return

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

    tg_id = query.from_user.id
    db_user = get_user_by_telegram_id(tg_id)

    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text(
            "Только участники могут добавлять видео.\n"
            "Используйте /start, чтобы выбрать роль."
        )
        return

    try:
        nomination_id = int(query.data.replace("add_video_nom_", ""))
    except ValueError:
        await query.edit_message_text("Ошибка выбора номинации.")
        return

    nomination = get_nomination_by_id(nomination_id)
    if not nomination:
        await query.edit_message_text("Номинация не найдена.")
        return

    # Сбрасываем режимы ожидания ввода, чтобы не было путаницы
    context.user_data.pop("awaiting_video_title_nomination_id", None)
    context.user_data.pop("awaiting_video_url_nomination_id", None)
    context.user_data.pop("temp_video_title", None)

    # ВАЖНО: показываем экран управления и выходим.
    await render_nomination_manage_screen(query, context, db_user, nomination_id)
    return


async def add_in_nom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    db_user = get_user_by_telegram_id(query.from_user.id)
    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text("Эта кнопка доступна только участникам.")
        return

    nomination_id = int(query.data.replace("add_in_nom_", ""))

    # проверка лимита 3
    participant_id = db_user["id"]
    if get_participant_videos_count(participant_id, nomination_id) >= 3:
        await render_nomination_manage_screen(query, context, db_user, nomination_id)
        return

    nomination = get_nomination_by_id(nomination_id)

    # ставим режим ожидания названия
    context.user_data["awaiting_video_title_nomination_id"] = nomination_id
    context.user_data.pop("awaiting_video_url_nomination_id", None)
    context.user_data.pop("temp_video_title", None)

    await query.edit_message_text(
        f"Номинация: «{nomination['name']}».\n"
        "Отправьте сообщение с НАЗВАНИЕМ видео.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Назад к номинации", callback_data=f"back_to_nom_manage_{nomination_id}")]
        ])
    )

async def back_to_nom_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    db_user = get_user_by_telegram_id(query.from_user.id)
    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text("Эта кнопка доступна только участникам.")
        return

    nomination_id = int(query.data.replace("back_to_nom_manage_", ""))

    # сбрасываем ожидание ввода
    context.user_data.pop("awaiting_video_title_nomination_id", None)
    context.user_data.pop("awaiting_video_url_nomination_id", None)
    context.user_data.pop("temp_video_title", None)

    await render_nomination_manage_screen(query, context, db_user, nomination_id)

async def delete_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    db_user = get_user_by_telegram_id(query.from_user.id)
    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text("Удалять видео могут только участники.")
        return

    # del_video_<video_id>_<nomination_id>
    parts = query.data.split("_")
    video_id = int(parts[2])
    nomination_id = int(parts[3])

    deleted = delete_video_for_participant(video_id, db_user["id"])

    # После удаления (или если не удалилось) — перерисовываем экран
    await render_nomination_manage_screen(query, context, db_user, nomination_id)


async def send_host_panel(message, db_user, context: ContextTypes.DEFAULT_TYPE):
    nominations = get_nominations()
    keyboard = [
        [InlineKeyboardButton(f"{nom['id']}. {nom['name']}", callback_data=f"host_nom_{nom['id']}")]
        for nom in nominations
    ]

    keyboard.append(
        [InlineKeyboardButton("📄 Все прикреплённые видео", callback_data="host_all_videos")]
    )

    await message.reply_text(
        "Выберите номинацию для управления голосованием "
        "или посмотрите все прикреплённые видео:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def back_to_nomination_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    db_user = get_user_by_telegram_id(query.from_user.id)
    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text("Эта кнопка доступна только участникам.")
        return

    # Важно: отменяем “ожидание названия/ссылки”, чтобы не было путаницы
    context.user_data.pop("awaiting_video_title_nomination_id", None)
    context.user_data.pop("awaiting_video_url_nomination_id", None)
    context.user_data.pop("temp_video_title", None)

    # Показываем меню выбора номинации заново
    await send_add_video_nomination_menu(query.message, db_user, context)


async def host_all_videos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ведущий смотрит все прикреплённые видео по номинациям и участникам."""
    query = update.callback_query
    await query.answer()

    db_user = get_user_by_telegram_id(query.from_user.id)
    if not db_user or db_user["role"] != ROLE_HOST:
        await query.edit_message_text("Эта функция доступна только ведущему.")
        return

    rows = get_all_videos_with_meta()
    back_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Назад", callback_data="back_to_host_panel")]
    ])

    if not rows:
        await query.edit_message_text("Пока ни одно видео не прикреплено.", reply_markup=back_markup)
        return

    # Формируем текст
    lines = []
    current_nomination_id = None
    current_participant_id = None
    per_participant_counter = 0

    for r in rows:
        nom_id = r["nomination_id"]
        nom_name = r["nomination_name"]
        part_id = r["participant_id"]
        part_name = r["participant_name"]
        title = r["title"]
        url = r["url"]

        # Новая номинация
        if nom_id != current_nomination_id:
            if lines:
                lines.append("")  # пустая строка между номинациями
            lines.append(f"Номинация {nom_id}. {nom_name}")
            current_nomination_id = nom_id
            current_participant_id = None

        # Новый участник внутри номинации
        if part_id != current_participant_id:
            lines.append("")  # пустая строка перед участником
            lines.append(part_name)
            current_participant_id = part_id
            per_participant_counter = 1
        else:
            per_participant_counter += 1

        lines.append(f'{per_participant_counter}. "{title}"')
        lines.append(url)

    text = "\n".join(lines).strip()
    parts = split_text(text)

    # 1) Первую часть — редактируем текущее сообщение
    if len(parts) == 1:
        await query.edit_message_text(parts[0], reply_markup=back_markup)
        return

    await query.edit_message_text(parts[0])

    # 2) Остальные части — отдельными сообщениями ведущему
    for part in parts[1:]:
        await context.bot.send_message(chat_id=query.from_user.id, text=part)

    # 3) Финальная кнопка "Назад"
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text="Конец списка.",
        reply_markup=back_markup
    )

    """Ведущий смотрит все прикреплённые видео по номинациям и участникам."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user or db_user["role"] != ROLE_HOST:
        await query.edit_message_text(
    text,
    reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Назад", callback_data="back_to_host_panel")]
    ])
)


    rows = get_all_videos_with_meta()
    if not rows:
        await query.edit_message_text("Пока ни одно видео не прикреплено.")
        return

    lines: list[str] = []

    current_nomination_id = None
    current_participant_id = None
    per_participant_counter = 0

    for r in rows:
        nom_id = r["nomination_id"]
        nom_name = r["nomination_name"]
        part_id = r["participant_id"]
        part_name = r["participant_name"]
        title = r["title"]
        url = r["url"]

        if nom_id != current_nomination_id:
            if lines:  
                lines.append("")
            lines.append(f"Номинация {nom_id}. {nom_name}")
            current_nomination_id = nom_id
            current_participant_id = None  

        if part_id != current_participant_id:
            lines.append("")  
            lines.append(part_name)
            current_participant_id = part_id
            per_participant_counter = 1
        else:
            per_participant_counter += 1

        lines.append(f'{per_participant_counter}. "{title}"')
        lines.append(url)
        text_parts = split_text(text)
        await query.edit_message_text(text_parts[0])
        for part in text_parts[1:]:await context.bot.send_message(chat_id=query.from_user.id, text=part)
        await context.bot.send_message(
    chat_id=query.from_user.id,
    text="Конец списка.",
    reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Назад", callback_data="back_to_host_panel")]
    ])
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
    [InlineKeyboardButton("⬅ Назад", callback_data="back_to_host_panel")]
]

    await query.edit_message_text(
        f"Номинация: {nomination['name']}\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    db_user = get_user_by_telegram_id(user.id)

    if not db_user:
        await query.edit_message_text("Используйте /start")
        return

    if query.data == "back_to_main":
        if db_user["role"] == ROLE_PARTICIPANT:
            await show_participant_menu(query.message)
        else:
            await show_host_menu(query.message)

    elif query.data == "back_to_participant_menu":
        await show_participant_menu(query.message)

    elif query.data == "back_to_host_panel":
        await show_host_panel_menu(query.message)


async def start_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    nomination_id = int(query.data.replace("start_vote_", ""))
    nomination = get_nomination_by_id(nomination_id)

    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM users WHERE role = 'participant'")
        participants = [dict(r) for r in cur.fetchall()]

    for p in participants:
        videos = get_videos_for_nomination_excluding_user(nomination_id, p["id"])
        if not videos:
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

async def participant_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")  
    nomination_id, video_id = int(data[1]), int(data[2])
    db_user = get_user_by_telegram_id(query.from_user.id)

    if not db_user or db_user["role"] != ROLE_PARTICIPANT:
        await query.edit_message_text("Голосовать могут только участники.")
        return

    save_vote(nomination_id, db_user["id"], video_id)
    await query.edit_message_text("✅ Ваш голос принят! Спасибо.")

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

async def render_nomination_manage_screen(query, context: ContextTypes.DEFAULT_TYPE, db_user, nomination_id: int):
    nomination = get_nomination_by_id(nomination_id)
    if not nomination:
        await query.edit_message_text("Номинация не найдена.")
        return

    participant_id = db_user["id"]
    videos = get_participant_videos_for_nomination(participant_id, nomination_id)
    count = len(videos)

    # Текст
    lines = [
        f"Номинация: «{nomination['name']}»",
        f"Ваши видео: {count}/3",
        ""
    ]
    if not videos:
        lines.append("Пока нет добавленных видео.")
    else:
        for i, v in enumerate(videos, start=1):
            lines.append(f'{i}. "{v["title"]}"')
            lines.append(v["url"])

    text = "\n".join(lines)

    # Кнопки
    keyboard = []

    # Кнопки удаления по каждому видео
    for v in videos:
        title_short = v["title"][:30] + ("…" if len(v["title"]) > 30 else "")
        keyboard.append([
            InlineKeyboardButton(f"🗑 Удалить: {title_short}", callback_data=f"del_video_{v['id']}_{nomination_id}")
        ])

    # Кнопка добавить (если ещё не 3)
    if count < 3:
        keyboard.append([
            InlineKeyboardButton("➕ Добавить видео в эту номинацию", callback_data=f"add_in_nom_{nomination_id}")
        ])

    # Назад к выбору номинации
    keyboard.append([
        InlineKeyboardButton("⬅ Назад к выбору номинации", callback_data="back_to_nomination_picker")
    ])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id
    text = (update.message.text or "").strip()

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
            await update.message.reply_text(
    f"✅ Видео добавлено в номинацию «{nomination['name']}».\n"
    f"Название: «{video['title']}»\n"
    f"Ссылка: {video['url']}\n\n"
    "Что дальше?",
    reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Открыть эту номинацию", callback_data=f"add_video_nom_{nomination_id_for_url}")],
        [InlineKeyboardButton("⬅ К номинациям", callback_data="back_to_nomination_picker")],
    ])
)

        return



    db_user = get_user_by_telegram_id(tg_id)

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

    await update.message.reply_text(
        "Сообщение получено.\n"
        "Используйте /start, чтобы увидеть доступные вам кнопки."
    )


def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_video", add_video_command))
    app.add_handler(CommandHandler("host_panel", host_panel))
    app.add_handler(CallbackQueryHandler(role_callback, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(action_callback, pattern="^action_"))
    app.add_handler(CallbackQueryHandler(add_video_nomination_callback, pattern="^add_video_nom_"))
    app.add_handler(CallbackQueryHandler(host_nomination_callback, pattern="^host_nom_"))
    app.add_handler(CallbackQueryHandler(start_vote_callback, pattern="^start_vote_"))
    app.add_handler(CallbackQueryHandler(stop_vote_callback, pattern="^stop_vote_"))
    app.add_handler(CallbackQueryHandler(participant_vote_callback, pattern="^vote_"))
    app.add_handler(CallbackQueryHandler(host_all_videos_callback, pattern="^host_all_videos$"))
    app.add_handler(CallbackQueryHandler(back_to_nomination_picker_callback, pattern="^back_to_nomination_picker$"))
    app.add_handler(CallbackQueryHandler(back_callback, pattern="^back_"))
    app.add_handler(CallbackQueryHandler(delete_video_callback, pattern="^del_video_"))
    app.add_handler(CallbackQueryHandler(add_in_nom_callback, pattern="^add_in_nom_"))
    app.add_handler(CallbackQueryHandler(back_to_nom_manage_callback, pattern="^back_to_nom_manage_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    print("Бот запущен. Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True, timeout=20)


if __name__ == "__main__":
    main()
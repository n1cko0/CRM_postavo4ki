from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

import db
import parsing


def get_delivery_assign_keyboard(delivery_id: int) -> InlineKeyboardMarkup:
    assigned = db.db_get_assigned_workers(delivery_id)
    delivery = db.db_get_delivery(delivery_id)
    needed = delivery.get("workers_needed") if delivery else None
    keyboard = []

    for w in assigned:
        row = []
        if w.get("telegram_id") or w.get("username"):
            chat_url = f"tg://user?id={w['telegram_id']}" if w.get("telegram_id") else f"https://t.me/{w['username']}"
            row.append(InlineKeyboardButton(w["name"], url=chat_url))
        else:
            row.append(InlineKeyboardButton(w["name"], callback_data="noop"))
        row.append(InlineKeyboardButton("❌", callback_data=f"unassign_{delivery_id}_{w['id']}"))
        keyboard.append(row)

    count = len(assigned)
    if needed and count >= needed:
        keyboard.append([
            InlineKeyboardButton(f"✅ Набрано ({count}/{needed})", callback_data="noop"),
            InlineKeyboardButton("➕ Призначити", callback_data=f"assign_{delivery_id}"),
        ])
    else:
        label = f"➕ Призначити ({count}/{needed})" if needed else "➕ Призначити"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"assign_{delivery_id}")])

    return InlineKeyboardMarkup(keyboard)


# ==================== KEYBOARDS ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📦 Поставки"],
            ["🏙 Мої міста", "📊 Звіт"],
            ["🗂 Мої звіти", "👷 Робітники"],
            ["📇 Кандидати", "💰 Борги логістів"],
        ],
        resize_keyboard=True
    )


def get_reports_list_keyboard():
    dates = db.db_list_report_dates()
    keyboard = [[InlineKeyboardButton(d, callback_data=f"rdate_{d}")] for d in dates]
    return keyboard, dates


def get_workers_list_keyboard():
    workers = db.db_get_workers()
    keyboard = [[InlineKeyboardButton(w["name"] or f"#{w['id']}", callback_data=f"wview_{w['id']}")] for w in workers]
    keyboard.append([InlineKeyboardButton("➕ Додати працівника", callback_data="waddnew")])
    return keyboard, workers


def get_candidates_list_keyboard():
    candidates = db.db_list_pending_contacts()
    keyboard = [
        [InlineKeyboardButton(c["full_name_ua"] or f"ID {c['telegram_id']}", callback_data=f"cand_{c['telegram_id']}")]
        for c in candidates
    ]
    return keyboard, candidates


def format_candidate_card(c: dict) -> str:
    lines = [f"📇 *{c['full_name_ua'] or '(без імені)'}*"]
    if c.get("age"):
        lines.append(f"🎂 Вік: {c['age']}")
    if c.get("phone"):
        lines.append(f"📞 {c['phone']}")
    if c.get("username"):
        lines.append(f"💬 @{c['username']}")
    if c.get("city_raw"):
        lines.append(f"📍 Місто (як написав): {c['city_raw']}")
        guess = parsing.guess_city(c["city_raw"], db.load_cities())
        if guess and guess.lower() != c["city_raw"].strip().lower():
            lines.append(f"   ймовірно: {guess}")
    lines.append(f"🆔 {c['telegram_id']}")
    return "\n".join(lines)


def format_worker_card(w: dict) -> str:
    lines = [f"👷 *{w['name']}*"]
    if w["phone"]:
        lines.append(f"📞 {w['phone']}")
    for p in db.db_get_worker_phones(w["id"]):
        lines.append(f"📞 {p['phone']} _(додатковий)_")
    if w.get("city"):
        lines.append(f"📍 {w['city']}")
    if w["username"]:
        lines.append(f"💬 @{w['username']}")
    if w["card"]:
        lines.append(f"💳 {w['card']}")
    if w.get("telegram_id"):
        lines.append(f"🆔 {w['telegram_id']} (справжній Telegram ID, підтверджений)")
    return "\n".join(lines)


def get_worker_card_keyboard(worker_id: int) -> InlineKeyboardMarkup:
    w = db.db_get_worker(worker_id)
    keyboard = []

    if w and (w.get("telegram_id") or w.get("username")):
        chat_url = f"tg://user?id={w['telegram_id']}" if w.get("telegram_id") else f"https://t.me/{w['username']}"
        keyboard.append([InlineKeyboardButton("💬 Написати", url=chat_url)])

    keyboard += [
        [
            InlineKeyboardButton("✏️ Ім'я", callback_data=f"wf_{worker_id}_name"),
            InlineKeyboardButton("📞 Телефони", callback_data=f"wphones_{worker_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Місто", callback_data=f"wf_{worker_id}_city"),
            InlineKeyboardButton("✏️ Username", callback_data=f"wf_{worker_id}_username"),
        ],
        [InlineKeyboardButton("✏️ Картка", callback_data=f"wf_{worker_id}_card")],
        [InlineKeyboardButton("🔗 Об'єднати з іншим", callback_data=f"wmergestart_{worker_id}")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"wdel_{worker_id}")],
        [InlineKeyboardButton("◀️ До списку", callback_data="wback")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_phones_management_keyboard(worker_id: int) -> InlineKeyboardMarkup:
    w = db.db_get_worker(worker_id)
    keyboard = []
    if w and w.get("phone"):
        keyboard.append([InlineKeyboardButton(f"📞 {w['phone']} (основний)", callback_data=f"wphoneview_{worker_id}_main")])
    else:
        keyboard.append([InlineKeyboardButton("➕ Додати основний номер", callback_data=f"wf_{worker_id}_phone")])
    for p in db.db_get_worker_phones(worker_id):
        keyboard.append([InlineKeyboardButton(f"📞 {p['phone']}", callback_data=f"wphoneview_{worker_id}_{p['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Додати ще номер", callback_data=f"wphoneadd_{worker_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"wview_{worker_id}")])
    return InlineKeyboardMarkup(keyboard)


def get_phone_action_keyboard(worker_id: int, token: str) -> InlineKeyboardMarkup:
    if token == "main":
        keyboard = [
            [InlineKeyboardButton("✏️ Змінити", callback_data=f"wf_{worker_id}_phone")],
            [InlineKeyboardButton("🗑 Видалити", callback_data=f"wphoneclearmain_{worker_id}")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("✏️ Змінити", callback_data=f"wphoneeditextra_{token}_{worker_id}")],
            [InlineKeyboardButton("⭐ Зробити основним", callback_data=f"wphonepromote_{token}_{worker_id}")],
            [InlineKeyboardButton("🗑 Видалити", callback_data=f"wphonedel_{token}_{worker_id}")],
        ]
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"wphones_{worker_id}")])
    return InlineKeyboardMarkup(keyboard)


def get_merge_target_keyboard(worker_id: int) -> InlineKeyboardMarkup:
    others = [w for w in db.db_get_workers() if w["id"] != worker_id]
    keyboard = [
        [InlineKeyboardButton(w["name"] or f"#{w['id']}", callback_data=f"wmergepick_{worker_id}_{w['id']}")]
        for w in others
    ]
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data=f"wview_{worker_id}")])
    return InlineKeyboardMarkup(keyboard)
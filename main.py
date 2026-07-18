import asyncio
import logging
import re
import json
import os
from datetime import datetime, date, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8971446928:AAF32e4fMvi9KQkcFKK924K1QbxwMbtNzzs")
SPREADSHEET_ID = "1x-vsC2M1cLtitP2DF04EqkSB4emVwvyh4N3jaauLqZ4"
CREDENTIALS_FILE = "credentials.json"
CITIES_FILE = "cities.json"
ALLOWED_USERS = [7305470549, 506094120]
REPORT_GROUP_ID = -5344273524

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Хранилище сообщений об оплатах
payment_sessions = {}
current_report_date = {}


# ==================== МІСТА ====================
def load_cities() -> dict:
    if os.path.exists(CITIES_FILE):
        with open(CITIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cities(cities: dict):
    with open(CITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cities, f, ensure_ascii=False, indent=2)


# ==================== GOOGLE SHEETS ====================
def get_sheet_data():
    import base64
    import json as json_module

    google_creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64")
    google_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

    if google_creds_b64:
        creds_json_str = base64.b64decode(google_creds_b64).decode("utf-8")
        creds_json_str = creds_json_str.replace('\\n', '\n')
        creds_dict = json_module.loads(creds_json_str)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    elif google_creds_json:
        creds_dict = json_module.loads(google_creds_json, strict=False)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    last_sheet = spreadsheet.worksheets()[-1]
    logger.info(f"Читаємо лист: {last_sheet.title}")

    all_values = last_sheet.get_all_values()
    sheet_id = last_sheet.id
    spreadsheet_meta = spreadsheet.fetch_sheet_metadata()

    merged_cells = []
    for sheet_meta in spreadsheet_meta.get('sheets', []):
        if sheet_meta['properties']['sheetId'] == sheet_id:
            for merge in sheet_meta.get('merges', []):
                merged_cells.append({
                    'start_row': merge['startRowIndex'],
                    'end_row': merge['endRowIndex'],
                    'start_col': merge['startColumnIndex'],
                    'end_col': merge['endColumnIndex'],
                })
            break

    return all_values, merged_cells


def is_merged_with_above(row_idx: int, col_idx: int, merged_cells: list) -> bool:
    for merge in merged_cells:
        if (merge['start_col'] <= col_idx < merge['end_col'] and
                merge['start_row'] < row_idx < merge['end_row']):
            return True
    return False


def extract_phone(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r'\+38\s*\(?\d{3}\)?\s*\d{3}[\s-]?\d{2}[\s-]?\d{2}',
        r'38\s*\(?\d{3}\)?\s*\d{3}[\s-]?\d{2}[\s-]?\d{2}',
        r'\+?\(0\d{2}\)\s*\d{3}[\s-]?\d{2}[\s-]?\d{2}',
        r'\b0\d{9}\b',
        r'\b\d{9}\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            phone = match.group().strip()
            digits = re.sub(r'\D', '', phone)
            if len(digits) == 9:
                digits = '380' + digits
            elif len(digits) == 10 and digits.startswith('0'):
                digits = '38' + digits
            return '+' + digits if not digits.startswith('+') else digits
    return ""


def parse_routes(all_values: list, merged_cells: list) -> list:
    routes = []
    current_route = []

    for row_idx, row in enumerate(all_values):
        if row[0] in ("Місто", "Город", "місто", "город"):
            continue
        if all(cell.strip() == "" for cell in row):
            if current_route:
                routes.append(current_route)
                current_route = []
            continue
        if not row[0].strip():
            continue
        current_route.append((row_idx, row))

    if current_route:
        routes.append(current_route)

    return routes


def build_delivery_messages(routes: list, merged_cells: list, filter_date: date = None) -> list:
    cities = load_cities()
    my_cities = {c.lower() for c in cities.keys()}
    messages = []

    for route in routes:
        driver_phone = ""
        for row_idx, row in route:
            if len(row) > 9 and row[9].strip():
                phone = extract_phone(row[9])
                if phone and not driver_phone:
                    driver_phone = phone

        groups = []
        current_group = []

        for row_idx, row in route:
            city = row[0].strip() if len(row) > 0 else ""
            tc = row[1].strip() if len(row) > 1 else ""
            brand = row[2].strip() if len(row) > 2 else ""
            boxes = row[4].strip() if len(row) > 4 else ""
            workers_cell = row[5].strip() if len(row) > 5 else ""
            delivery_date = row[7].strip() if len(row) > 7 else ""
            delivery_time = row[8].strip() if len(row) > 8 else ""

            if not city or not delivery_date:
                continue

            parsed_date = None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    parsed_date = datetime.strptime(delivery_date, fmt).date()
                    break
                except ValueError:
                    continue

            is_continuation = is_merged_with_above(row_idx, 5, merged_cells)

            point = {
                "city": city, "tc": tc, "brand": brand, "boxes": boxes,
                "workers": workers_cell, "date": parsed_date,
                "date_str": delivery_date, "time": delivery_time,
                "phone": driver_phone, "is_continuation": is_continuation,
            }

            if is_continuation:
                current_group.append(point)
            else:
                if current_group:
                    groups.append(current_group)
                current_group = [point]

        if current_group:
            groups.append(current_group)

        for group in groups:
            first = group[0]

            if filter_date:
                dates_in_group = [p["date"] for p in group if p["date"]]
                if not any(d == filter_date for d in dates_in_group):
                    continue

            if len(group) == 1:
                if first['city'].lower() not in my_cities:
                    continue
                msg = f"📦 *{first['brand']}*\n"
                msg += f"📍 {first['city']}, {first['tc']}\n"
                msg += f"📅 {first['date_str']}"
                if first['time']:
                    msg += f"  🕐 {first['time']}"
                msg += "\n"
                msg += f"📦 Коробок: {first['boxes']}\n"
                msg += f"👷 Вантажників: {first['workers']}\n"
                if first['phone']:
                    msg += f"📞 {first['phone']}"
            else:
                group = [p for p in group if p['city'].lower() in my_cities]
                if not group:
                    continue
                first = group[0]
                unique_tc = {p['tc'] for p in group}

                if len(unique_tc) == 1:
                    brands = ", ".join(p['brand'] for p in group)
                    total_boxes = sum(int(p['boxes']) for p in group if p['boxes'].isdigit())
                    msg = f"📦 *{brands}*\n"
                    msg += f"📍 {first['city']}, {first['tc']}\n"
                    msg += f"📅 {first['date_str']}"
                    if first['time']:
                        msg += f"  🕐 {first['time']}"
                    msg += "\n"
                    msg += f"📦 Коробок: {total_boxes}\n"
                    msg += f"👷 Вантажників: {first['workers']}\n"
                    if first['phone']:
                        msg += f"📞 {first['phone']}"
                elif len(group) == 1:
                    msg = f"📦 *{first['brand']}*\n"
                    msg += f"📍 {first['city']}, {first['tc']}\n"
                    msg += f"📅 {first['date_str']}"
                    if first['time']:
                        msg += f"  🕐 {first['time']}"
                    msg += "\n"
                    msg += f"📦 Коробок: {first['boxes']}\n"
                    msg += f"👷 Вантажників: {first['workers']}\n"
                    if first['phone']:
                        msg += f"📞 {first['phone']}"
                else:
                    msg = f"🗺 Маршрут\n"
                    msg += f"👷 Вантажників: {first['workers']}\n"
                    if first['phone']:
                        msg += f"📞 {first['phone']}\n"
                    msg += "─────────────────\n"
                    for p in group:
                        msg += f"📦 *{p['brand']}*\n"
                        msg += f"📍 {p['city']}, {p['tc']}\n"
                        msg += f"📅 {p['date_str']}"
                        if p['time']:
                            msg += f"  🕐 {p['time']}"
                        msg += f"  📦 {p['boxes']} кор.\n\n"

            messages.append({
                "text": msg,
                "date": first["date"],
                "date_str": first["date_str"],
            })

    return messages


# ==================== ЗВІТ ====================
def parse_payment_message(text: str):
    pattern = r'^(.+?)\s+по\s+(\d+(?:[.,]\d+)?)\s+\((\d+(?:[.,]\d+)?)\)'
    match = re.match(pattern, text.strip())
    if not match:
        return None
    location = match.group(1).strip()
    amount_per_person = float(match.group(2).replace(',', '.'))
    hours = float(match.group(3).replace(',', '.'))
    return {
        'location': location,
        'amount_per_person': amount_per_person,
        'hours': hours,
    }


def parse_workers_count(text: str):
    mapping = {
        'за двох': 2, 'за трьох': 3, 'за чотирьох': 4,
        "за п'ятьох": 5, 'за шістьох': 6,
    }
    return mapping.get(text.strip().lower())


def is_card_number(text: str) -> bool:
    cleaned = re.sub(r'[\s\-]', '', text.strip().lstrip('*').strip())
    return cleaned.isdigit() and len(cleaned) >= 12


def build_report(report_date: str) -> list:
    messages = payment_sessions.get(report_date, [])
    if not messages:
        return []

    cities = load_cities()
    reports = []
    workers_ua = {
        1: '', 2: 'За двох', 3: 'За трьох',
        4: 'За чотирьох', 5: "За п'ятьох", 6: 'За шістьох'
    }

    i = 0
    while i < len(messages):
        text = messages[i]['text'].strip()
        payment = parse_payment_message(text)
        if not payment:
            i += 1
            continue

        location = payment['location']
        hours = payment['hours']

        city = None
        for city_name in cities.keys():
            if location.lower().startswith(city_name.lower()):
                city = city_name
                break

        my_rate = cities.get(city, 0) if city else 0

        workers_count = 0
        j = i + 1

        if j < len(messages):
            next_text = messages[j]['text'].strip()
            wc = parse_workers_count(next_text)
            if wc:
                workers_count = wc
                j += 1
                while j < len(messages) and is_card_number(messages[j]['text']):
                    j += 1
            else:
                while j < len(messages) and is_card_number(messages[j]['text']):
                    workers_count += 1
                    j += 1

        if workers_count == 0:
            workers_count = 1

        my_total = my_rate * hours * workers_count

        if workers_count > 1:
            per_person = int(my_total / workers_count)
            label = workers_ua.get(workers_count, f'За {workers_count}')
            report_text = f"{location} по {per_person} ({hours})\n{label}"
        else:
            report_text = f"{location} по {int(my_total)} ({hours})"

        reports.append(report_text)
        i = j

    return reports


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != REPORT_GROUP_ID:
        return
    if msg.from_user.id not in ALLOWED_USERS:
        return

    text = msg.text or ""
    if not text:
        return

    user_id = msg.from_user.id
    if user_id not in current_report_date:
        return

    report_date = current_report_date[user_id]
    if report_date == "waiting_date":
        return

    if report_date not in payment_sessions:
        payment_sessions[report_date] = []

    payment_sessions[report_date].append({
        'text': text,
        'timestamp': msg.date.isoformat(),
        'message_id': msg.message_id,
    })


# ==================== KEYBOARDS ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📦 Поставки"],
            ["🏙 Мої міста", "📊 Звіт"],
        ],
        resize_keyboard=True
    )


def get_deliveries_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📋 Всі поставки"],
            ["📅 На сьогодні", "📅 На завтра"],
            ["🔢 На конкретну дату"],
            ["◀️ Назад"],
        ],
        resize_keyboard=True
    )


# ==================== ВІДПРАВКА ПОСТАВОК ====================
async def send_deliveries_msg(update: Update, filter_date: date = None):
    await update.message.reply_text("⏳ Завантажую дані з таблиці...")
    try:
        all_values, merged_cells = get_sheet_data()
        routes = parse_routes(all_values, merged_cells)
        messages = build_delivery_messages(routes, merged_cells, filter_date=filter_date)

        if not messages:
            date_info = filter_date.strftime("%d.%m.%Y") if filter_date else ""
            await update.message.reply_text(
                f"❌ Поставок {'на ' + date_info if date_info else ''} не знайдено."
            )
            return

        date_info = filter_date.strftime("%d.%m.%Y") if filter_date else "всі"
        await update.message.reply_text(
            f"✅ Знайдено поставок: *{len(messages)}* (дата: {date_info})",
            parse_mode="Markdown"
        )

        for msg_data in messages:
            try:
                await update.message.reply_text(msg_data["text"], parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Помилка відправки, чекаємо: {e}")
                await asyncio.sleep(2)
                await update.message.reply_text(msg_data["text"], parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Помилка: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Помилка: {str(e)}")


async def send_deliveries_query(query, filter_date: date = None):
    await query.edit_message_text("⏳ Завантажую дані з таблиці...")
    try:
        all_values, merged_cells = get_sheet_data()
        routes = parse_routes(all_values, merged_cells)
        messages = build_delivery_messages(routes, merged_cells, filter_date=filter_date)

        if not messages:
            date_info = filter_date.strftime("%d.%m.%Y") if filter_date else ""
            await query.message.reply_text(
                f"❌ Поставок {'на ' + date_info if date_info else ''} не знайдено."
            )
            return

        date_info = filter_date.strftime("%d.%m.%Y") if filter_date else "всі"
        await query.message.reply_text(
            f"✅ Знайдено поставок: *{len(messages)}* (дата: {date_info})",
            parse_mode="Markdown"
        )

        for msg_data in messages:
            try:
                await query.message.reply_text(msg_data["text"], parse_mode="Markdown")
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Помилка відправки, чекаємо: {e}")
                await asyncio.sleep(2)
                await query.message.reply_text(msg_data["text"], parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Помилка: {e}", exc_info=True)
        await query.message.reply_text(f"❌ Помилка: {str(e)}")


# ==================== КОМАНДИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    await update.message.reply_text(
        "👋 Привіт! Я бот для поставок FM Logistics.\n\nОбери розділ:",
        reply_markup=get_main_keyboard()
    )


async def mycities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    cities = load_cities()
    if not cities:
        await update.message.reply_text("❌ Список міст порожній.")
        return
    text = "🏙 *Мої міста:*\n\n"
    for city, rate in sorted(cities.items()):
        text += f"📍 {city} — {rate} грн/год\n"
    text += "\nЩоб додати: /addcity Назва Тариф\nЩоб видалити: /removecity Назва"
    await update.message.reply_text(text, parse_mode="Markdown")


async def addcity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /addcity Назва 135\nПриклад: /addcity Вінниця 135")
        return
    rate_str = context.args[-1]
    city_name = " ".join(context.args[:-1])
    try:
        rate = int(rate_str)
    except ValueError:
        await update.message.reply_text("❌ Тариф має бути числом.")
        return
    cities = load_cities()
    cities[city_name] = rate
    save_cities(cities)
    await update.message.reply_text(f"✅ Додано: {city_name} — {rate} грн/год")


async def removecity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Використання: /removecity Назва\nПриклад: /removecity Вінниця")
        return
    city_name = " ".join(context.args)
    cities = load_cities()
    if city_name in cities:
        del cities[city_name]
        save_cities(cities)
        await update.message.reply_text(f"✅ Видалено: {city_name}")
    else:
        await update.message.reply_text(f"❌ Місто '{city_name}' не знайдено.")


# ==================== TEXT HANDLER ====================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if update.message.chat.type in ['group', 'supergroup']:
        return

    user_id = update.effective_user.id

    # Обработка ввода даты вручную
    if current_report_date.get(user_id) == "waiting_date":
        text_input = update.message.text.strip()
        try:
            datetime.strptime(text_input, "%d.%m.%Y")
            current_report_date[user_id] = text_input
            if text_input not in payment_sessions:
                payment_sessions[text_input] = []
            await update.message.reply_text(
                f"✅ Дата звіту: {text_input}\n\nТепер скидайте оплати в групу.",
                reply_markup=get_main_keyboard()
            )
        except ValueError:
            await update.message.reply_text("❌ Невірний формат. Введіть дату як ДД.ММ.РРРР")
        return

    text = update.message.text

    if text == "📦 Поставки":
        await update.message.reply_text(
            "Обери що показати:",
            reply_markup=get_deliveries_keyboard()
        )
    elif text == "◀️ Назад":
        await update.message.reply_text(
            "Головне меню:",
            reply_markup=get_main_keyboard()
        )
    elif text == "📋 Всі поставки":
        await send_deliveries_msg(update, filter_date=None)
    elif text == "📅 На сьогодні":
        await send_deliveries_msg(update, filter_date=date.today())
    elif text == "📅 На завтра":
        await send_deliveries_msg(update, filter_date=date.today() + timedelta(days=1))
    elif text == "🔢 На конкретну дату":
        keyboard = []
        for i in range(7):
            d = date.today() + timedelta(days=i)
            label = d.strftime("%d.%m.%Y")
            keyboard.append([InlineKeyboardButton(label, callback_data=f"date_{label}")])
        await update.message.reply_text(
            "Оберіть дату:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif text == "🏙 Мої міста":
        await mycities(update, context)
    elif text == "📊 Звіт":
        keyboard = [
            [InlineKeyboardButton("📅 Вчора", callback_data="report_yesterday")],
            [InlineKeyboardButton("📅 Сьогодні", callback_data="report_today")],
            [InlineKeyboardButton("✏️ Ввести дату", callback_data="report_custom")],
        ]
        await update.message.reply_text(
            "Оберіть дату звіту:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ==================== CALLBACK HANDLER ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS:
        await query.answer("⛔ Доступ заборонено.")
        return
    await query.answer()
    data = query.data

    if data.startswith("date_"):
        date_str = data.replace("date_", "")
        try:
            filter_date = datetime.strptime(date_str, "%d.%m.%Y").date()
            await send_deliveries_query(query, filter_date=filter_date)
        except ValueError:
            await query.edit_message_text("Помилка дати")

    elif data == "report_yesterday":
        d = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
        current_report_date[query.from_user.id] = d
        if d not in payment_sessions:
            payment_sessions[d] = []
        keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
        await query.edit_message_text(
            f"✅ Дата звіту: {d}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "report_today":
        d = date.today().strftime("%d.%m.%Y")
        current_report_date[query.from_user.id] = d
        if d not in payment_sessions:
            payment_sessions[d] = []
        keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
        await query.edit_message_text(
            f"✅ Дата звіту: {d}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "report_custom":
        current_report_date[query.from_user.id] = "waiting_date"
        await query.edit_message_text("Введіть дату у форматі ДД.ММ.РРРР:")

    elif data == "build_report":
        user_id = query.from_user.id
        if user_id not in current_report_date:
            await query.edit_message_text("❌ Спочатку оберіть дату звіту.")
            return
        report_date = current_report_date[user_id]
        reports = build_report(report_date)
        if not reports:
            await query.edit_message_text("❌ Немає даних для звіту.")
            return
        await query.edit_message_text(f"✅ Звіт за {report_date}:")
        for r in reports:
            await query.message.reply_text(r)


# ==================== MAIN ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mycities", mycities))
    app.add_handler(CommandHandler("addcity", addcity))
    app.add_handler(CommandHandler("removecity", removecity))
    app.add_handler(MessageHandler(
        filters.Chat(REPORT_GROUP_ID) & filters.TEXT & ~filters.COMMAND,
        group_message_handler
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Бот запущено!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
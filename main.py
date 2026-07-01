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

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


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
                "city": city,
                "tc": tc,
                "brand": brand,
                "boxes": boxes,
                "workers": workers_cell,
                "date": parsed_date,
                "date_str": delivery_date,
                "time": delivery_time,
                "phone": driver_phone,
                "is_continuation": is_continuation,
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
                        msg += f"  📦 {p['boxes']} кор.\n"
                        msg += "\n"

            messages.append({
                "text": msg,
                "date": first["date"],
                "date_str": first["date_str"],
            })

    return messages


# ==================== REPLY KEYBOARD ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📦 Поставки"],
            ["🏙 Мої міста"],
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


# ==================== MAIN ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mycities", mycities))
    app.add_handler(CommandHandler("addcity", addcity))
    app.add_handler(CommandHandler("removecity", removecity))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Бот запущено!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
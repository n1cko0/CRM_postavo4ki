import asyncio
import logging
import re
import difflib
import json
import os
import sqlite3
from datetime import datetime, date, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]  # токен берём тільки з env
FM_SPREADSHEET_ID = "1x-vsC2M1cLtitP2DF04EqkSB4emVwvyh4N3jaauLqZ4"
EKOL_SPREADSHEET_ID = os.environ.get("EKOL_SPREADSHEET_ID", "")  # поки не налаштовано
CREDENTIALS_FILE = "credentials.json"

# RAILWAY_VOLUME_MOUNT_PATH встановлюється Railway автоматично, якщо до сервісу
# прикріплено Volume. Локально (в PyCharm) цієї змінної немає — тоді файли
# зберігаються поруч зі скриптом, як і раніше.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
CITIES_FILE = os.path.join(DATA_DIR, "cities.json")       # тільки для одноразової міграції
REPORTS_FILE = os.path.join(DATA_DIR, "reports.json")     # тільки для одноразової міграції
DB_FILE = os.path.join(DATA_DIR, "postavo4ki.db")
ALLOWED_USERS = [7305470549, 506094120]
REPORT_GROUP_ID = -5344273524
MY_CARD_NUMBER = "4441111134286644"

# Міста, які треба виключити саме з парсингу FM (наприклад через однойменне
# але зовсім інше місто в тій же колонці "Місто" — реальний Миколаїв ведеться
# тільки через Ekol, а в FM під цією ж назвою трапляється Миколаїв Львівської області)
FM_EXCLUDED_CITIES = {"Миколаїв"}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

current_report_date = {}
edit_state = {}  # user_id -> {"date": ..., "location": ..., "field": "hours"|"workers"|"paid"}
worker_flow_state = {}  # user_id -> {"mode": "add_name"|"add_phone"|"add_username"|"add_card"|"edit_field", ...}
anketa_state = {}  # user_id -> {"step": "name"|"age"|"phone", "data": {...}} — для незнайомих людей (не ALLOWED_USERS)

WORKER_FIELD_LABELS = {
    "name": "ім'я",
    "phone": "телефон",
    "username": "username в Telegram (без @)",
    "card": "номер картки",
    "city": "місто",
}

FIELD_LABELS = {
    "hours": "години",
    "workers": "кількість людей",
    "paid": "сума виплат (те, що я реально заплатив)",
}
FIELD_TO_OVERRIDE_KEY = {
    "hours": "hours",
    "workers": "total_workers",
    "paid": "paid_to_workers",
}

# Тексты кнопок Reply Keyboard — их нельзя перехватывать как оплату/карту в группе
BUTTON_TEXTS = {
    "📦 Поставки", "🏙 Мої міста", "📊 Звіт", "🗂 Мої звіти", "👷 Робітники", "📇 Кандидати",
}


# ==================== SQLITE ====================
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            name TEXT PRIMARY KEY,
            rate INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS city_aliases (
            alias TEXT PRIMARY KEY,
            city_name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            text TEXT NOT NULL,
            timestamp TEXT,
            message_id INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_messages_date ON payment_messages(report_date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            username TEXT,
            card_number TEXT,
            created_at TEXT
        )
    """)
    worker_cols = [r["name"] for r in conn.execute("PRAGMA table_info(workers)")]
    if "telegram_id" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN telegram_id INTEGER")
    if "city" not in worker_cols:
        conn.execute("ALTER TABLE workers ADD COLUMN city TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_contacts (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            full_name_ua TEXT,
            age TEXT,
            phone TEXT,
            city_raw TEXT,
            anketa_completed_at TEXT,
            converted_to_worker_id INTEGER
        )
    """)
    contact_cols = [r["name"] for r in conn.execute("PRAGMA table_info(bot_contacts)")]
    if "city_raw" not in contact_cols:
        conn.execute("ALTER TABLE bot_contacts ADD COLUMN city_raw TEXT")
    if "dismissed" not in contact_cols:
        conn.execute("ALTER TABLE bot_contacts ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_overrides (
            report_date TEXT NOT NULL,
            location TEXT NOT NULL,
            hours REAL,
            total_workers INTEGER,
            paid_to_workers REAL,
            PRIMARY KEY (report_date, location)
        )
    """)
    conn.commit()
    conn.close()


def migrate_json_to_db():
    """Одноразово переносить дані зі старих cities.json / reports.json у SQLite,
    якщо відповідні таблиці ще порожні. Самі JSON-файли не видаляються — лежать як бекап."""
    conn = get_conn()

    cities_count = conn.execute("SELECT COUNT(*) AS c FROM cities").fetchone()["c"]
    if cities_count == 0 and os.path.exists(CITIES_FILE):
        try:
            with open(CITIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, info in data.items():
                if isinstance(info, (int, float)):
                    rate, aliases = info, []
                else:
                    rate, aliases = info.get("rate", 0), info.get("aliases", [])
                conn.execute("INSERT OR IGNORE INTO cities (name, rate) VALUES (?, ?)", (name, rate))
                for alias in aliases:
                    conn.execute(
                        "INSERT OR IGNORE INTO city_aliases (alias, city_name) VALUES (?, ?)",
                        (alias, name)
                    )
            conn.commit()
            logger.info(f"Мігровано {len(data)} міст з cities.json у SQLite")
        except Exception as e:
            logger.error(f"Помилка міграції cities.json: {e}", exc_info=True)

    messages_count = conn.execute("SELECT COUNT(*) AS c FROM payment_messages").fetchone()["c"]
    if messages_count == 0 and os.path.exists(REPORTS_FILE):
        try:
            with open(REPORTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            total = 0
            for report_date, session in data.items():
                for msg in session.get("messages", []):
                    conn.execute(
                        "INSERT INTO payment_messages (report_date, text, timestamp, message_id) VALUES (?, ?, ?, ?)",
                        (report_date, msg.get("text", ""), msg.get("timestamp"), msg.get("message_id"))
                    )
                    total += 1
                for location, ov in session.get("overrides", {}).items():
                    conn.execute(
                        """INSERT INTO report_overrides (report_date, location, hours, total_workers, paid_to_workers)
                           VALUES (?, ?, ?, ?, ?)""",
                        (report_date, location, ov.get("hours"), ov.get("total_workers"), ov.get("paid_to_workers"))
                    )
            conn.commit()
            logger.info(f"Мігровано {total} повідомлень з reports.json у SQLite")
        except Exception as e:
            logger.error(f"Помилка міграції reports.json: {e}", exc_info=True)

    conn.close()


# ==================== МІСТА ====================
def load_cities() -> dict:
    """Формат: {"Назва": {"rate": 200, "aliases": ["Скорочення", ...]}}"""
    conn = get_conn()
    cities = {}
    for row in conn.execute("SELECT name, rate FROM cities"):
        cities[row["name"]] = {"rate": row["rate"], "aliases": []}
    for row in conn.execute("SELECT alias, city_name FROM city_aliases"):
        if row["city_name"] in cities:
            cities[row["city_name"]]["aliases"].append(row["alias"])
    conn.close()
    return cities


def save_cities(cities: dict):
    """Повністю синхронізує таблиці cities/city_aliases зі станом переданого словника."""
    conn = get_conn()
    existing = {row["name"] for row in conn.execute("SELECT name FROM cities")}
    incoming = set(cities.keys())

    for name in existing - incoming:
        conn.execute("DELETE FROM city_aliases WHERE city_name = ?", (name,))
        conn.execute("DELETE FROM cities WHERE name = ?", (name,))

    for name, info in cities.items():
        rate = info.get("rate", 0) if isinstance(info, dict) else info
        aliases = info.get("aliases", []) if isinstance(info, dict) else []
        conn.execute(
            "INSERT INTO cities (name, rate) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET rate=excluded.rate",
            (name, rate)
        )
        conn.execute("DELETE FROM city_aliases WHERE city_name = ?", (name,))
        for alias in aliases:
            conn.execute(
                "INSERT OR REPLACE INTO city_aliases (alias, city_name) VALUES (?, ?)",
                (alias, name)
            )

    conn.commit()
    conn.close()


def build_city_index(cities: dict) -> dict:
    """Ключ у нижньому регістрі (назва або синонім) -> канонічна назва міста."""
    index = {}
    for city_name, info in cities.items():
        aliases = info.get("aliases", []) if isinstance(info, dict) else []
        index[city_name.lower()] = city_name
        for alias in aliases:
            index[alias.lower()] = city_name
    return index


# ==================== ЗВІТИ (сирі повідомлення про оплату + ручні правки) ====================
def db_add_message(report_date: str, text: str, timestamp: str, message_id: int):
    conn = get_conn()
    conn.execute(
        "INSERT INTO payment_messages (report_date, text, timestamp, message_id) VALUES (?, ?, ?, ?)",
        (report_date, text, timestamp, message_id)
    )
    conn.commit()
    conn.close()


def db_get_messages(report_date: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT text, timestamp, message_id FROM payment_messages WHERE report_date = ? ORDER BY id",
        (report_date,)
    ).fetchall()
    conn.close()
    return [{"text": r["text"], "timestamp": r["timestamp"], "message_id": r["message_id"]} for r in rows]


def db_list_report_dates() -> list:
    conn = get_conn()
    rows = conn.execute("""
        SELECT report_date FROM payment_messages
        UNION
        SELECT report_date FROM report_overrides
    """).fetchall()
    conn.close()
    dates = {r["report_date"] for r in rows}
    return sorted(dates, key=lambda d: datetime.strptime(d, "%d.%m.%Y"), reverse=True)


def db_delete_report(report_date: str):
    conn = get_conn()
    conn.execute("DELETE FROM payment_messages WHERE report_date = ?", (report_date,))
    conn.execute("DELETE FROM report_overrides WHERE report_date = ?", (report_date,))
    conn.commit()
    conn.close()


def db_get_overrides(report_date: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT location, hours, total_workers, paid_to_workers FROM report_overrides WHERE report_date = ?",
        (report_date,)
    ).fetchall()
    conn.close()
    overrides = {}
    for r in rows:
        ov = {}
        if r["hours"] is not None:
            ov["hours"] = r["hours"]
        if r["total_workers"] is not None:
            ov["total_workers"] = r["total_workers"]
        if r["paid_to_workers"] is not None:
            ov["paid_to_workers"] = r["paid_to_workers"]
        overrides[r["location"]] = ov
    return overrides


def db_set_override(report_date: str, location: str, field: str, value):
    if field not in ("hours", "total_workers", "paid_to_workers"):
        raise ValueError(f"Невідоме поле override: {field}")
    conn = get_conn()
    conn.execute(
        "INSERT INTO report_overrides (report_date, location) VALUES (?, ?) "
        "ON CONFLICT(report_date, location) DO NOTHING",
        (report_date, location)
    )
    conn.execute(
        f"UPDATE report_overrides SET {field} = ? WHERE report_date = ? AND location = ?",
        (value, report_date, location)
    )
    conn.commit()
    conn.close()


def db_delete_override(report_date: str, location: str):
    conn = get_conn()
    conn.execute(
        "DELETE FROM report_overrides WHERE report_date = ? AND location = ?",
        (report_date, location)
    )
    conn.commit()
    conn.close()


# ==================== РОБІТНИКИ ====================
def db_add_worker(name: str, phone: str = "", username: str = "", card: str = "", telegram_id: int = None, city: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO workers (name, phone, username, card_number, telegram_id, city, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, phone, username, card, telegram_id, city, datetime.now().isoformat())
    )
    conn.commit()
    worker_id = cur.lastrowid
    conn.close()
    return worker_id


UA_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"


def ua_sort_key(name: str):
    name = name.lower()
    return [UA_ALPHABET.index(c) if c in UA_ALPHABET else 1000 + ord(c) for c in name]


def db_get_workers() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT id, name, phone, username, card_number, telegram_id, city FROM workers").fetchall()
    conn.close()
    workers = [
        {
            "id": r["id"], "name": r["name"], "phone": r["phone"] or "",
            "username": r["username"] or "", "card": r["card_number"] or "",
            "telegram_id": r["telegram_id"], "city": r["city"] or "",
        }
        for r in rows
    ]
    workers.sort(key=lambda w: ua_sort_key(w["name"]))
    return workers


def db_get_worker(worker_id: int):
    conn = get_conn()
    r = conn.execute(
        "SELECT id, name, phone, username, card_number, telegram_id, city FROM workers WHERE id = ?",
        (worker_id,)
    ).fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r["id"], "name": r["name"], "phone": r["phone"] or "",
        "username": r["username"] or "", "card": r["card_number"] or "",
        "telegram_id": r["telegram_id"], "city": r["city"] or "",
    }


def db_update_worker(worker_id: int, field: str, value: str):
    column = {"name": "name", "phone": "phone", "username": "username", "card": "card_number", "city": "city"}.get(field)
    if not column:
        raise ValueError(f"Невідоме поле працівника: {field}")
    conn = get_conn()
    conn.execute(f"UPDATE workers SET {column} = ? WHERE id = ?", (value, worker_id))
    conn.commit()
    conn.close()


def db_delete_worker(worker_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
    conn.commit()
    conn.close()


# ==================== КОНТАКТИ БОТА (для "знайомства" з незнайомими людьми) ====================
def db_touch_contact(user):
    """Логуємо/оновлюємо будь-якого, хто написав боту — незалежно від того, чи є в ALLOWED_USERS."""
    conn = get_conn()
    now = datetime.now().isoformat()
    existing = conn.execute("SELECT telegram_id FROM bot_contacts WHERE telegram_id = ?", (user.id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE bot_contacts SET username=?, first_name=?, last_name=?, last_seen=? WHERE telegram_id=?",
            (user.username or "", user.first_name or "", user.last_name or "", now, user.id)
        )
    else:
        conn.execute(
            "INSERT INTO bot_contacts (telegram_id, username, first_name, last_name, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, user.username or "", user.first_name or "", user.last_name or "", now, now)
        )
    conn.commit()
    conn.close()


def db_save_anketa(telegram_id: int, data: dict):
    conn = get_conn()
    conn.execute(
        "UPDATE bot_contacts SET full_name_ua=?, age=?, phone=?, city_raw=?, anketa_completed_at=? WHERE telegram_id=?",
        (
            data.get("full_name", ""), data.get("age", ""), data.get("phone", ""),
            data.get("city", ""), datetime.now().isoformat(), telegram_id
        )
    )
    conn.commit()
    conn.close()


def db_get_contact(telegram_id: int):
    conn = get_conn()
    r = conn.execute("SELECT * FROM bot_contacts WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(r) if r else None


def db_list_pending_contacts() -> list:
    """Ті, хто заповнив анкету, але ще не доданий у реєстр робітників і не відхилений."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_contacts WHERE anketa_completed_at IS NOT NULL "
        "AND converted_to_worker_id IS NULL AND dismissed = 0 "
        "ORDER BY anketa_completed_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_mark_converted(telegram_id: int, worker_id: int):
    conn = get_conn()
    conn.execute("UPDATE bot_contacts SET converted_to_worker_id = ? WHERE telegram_id = ?", (worker_id, telegram_id))
    conn.commit()
    conn.close()


def db_dismiss_contact(telegram_id: int):
    conn = get_conn()
    conn.execute("UPDATE bot_contacts SET dismissed = 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


init_db()
migrate_json_to_db()


# ==================== GOOGLE SHEETS ====================
def get_sheet_data(source: str = "fm"):
    import base64
    import json as json_module

    if source == "fm":
        spreadsheet_id = FM_SPREADSHEET_ID
    elif source == "ekol":
        spreadsheet_id = EKOL_SPREADSHEET_ID
    else:
        raise ValueError(f"Невідоме джерело: {source}")

    if not spreadsheet_id:
        raise ValueError(f"Таблиця для '{source}' ще не налаштована (немає SPREADSHEET_ID)")

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
    spreadsheet = client.open_by_key(spreadsheet_id)
    last_sheet = spreadsheet.worksheets()[-1]
    logger.info(f"Читаємо лист ({source}): {last_sheet.title}")

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
    excluded = {c.lower() for c in FM_EXCLUDED_CITIES}
    my_cities = {c.lower() for c in cities.keys()} - excluded
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


# ==================== EKOL ====================
def normalize_apostrophes(text: str) -> str:
    return text.replace('’', "'").replace('`', "'").replace('ʼ', "'")


def address_matches_my_cities(address: str, cities: dict) -> bool:
    """Перевіряє, чи згадується в адресі хоча б одне з моїх міст (або синонім) —
    точним співпадінням цілого слова, без урахування регістру."""
    addr_lower = normalize_apostrophes(address.lower())
    for city_name, info in cities.items():
        aliases = info.get('aliases', []) if isinstance(info, dict) else []
        for candidate in [city_name] + aliases:
            candidate_lower = normalize_apostrophes(candidate.lower())
            pattern = r'\b' + re.escape(candidate_lower) + r'\b'
            if re.search(pattern, addr_lower):
                return True
    return False


def parse_ekol_deliveries(all_values: list, filter_date: date = None) -> list:
    """Колонки: A компанія, B адреса, C дата, D час, E кількість, F вантажники, G телефон.
    Кожен рядок — окрема, незалежна поставка (без merged cells)."""
    cities = load_cities()
    messages = []

    for row in all_values:
        company = row[0].strip() if len(row) > 0 else ""
        address = row[1].strip() if len(row) > 1 else ""
        date_str = row[2].strip() if len(row) > 2 else ""
        time_str = row[3].strip() if len(row) > 3 else ""
        qty = row[4].strip() if len(row) > 4 else ""
        workers = row[5].strip() if len(row) > 5 else ""
        phone = row[6].strip() if len(row) > 6 else ""

        if not address or not date_str:
            continue

        parsed_date = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                parsed_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        if not parsed_date:
            continue  # не рядок з даними (шапка таблиці, порожній рядок тощо)

        if filter_date and parsed_date != filter_date:
            continue

        if not address_matches_my_cities(address, cities):
            continue

        msg = f"📦 *{company or '—'}*\n"
        msg += f"📍 {address}\n"
        msg += f"📅 {date_str}"
        if time_str:
            msg += f"  🕐 {time_str}"
        msg += "\n"
        if qty:
            msg += f"📦 Кількість: {qty}\n"
        if workers:
            msg += f"👷 Вантажників: {workers}\n"
        if phone:
            msg += f"📞 {phone}"

        messages.append({
            "text": msg.strip(),
            "date": parsed_date,
            "date_str": date_str,
        })

    return messages


# ==================== ЗВІТ ТА СТАТИСТИКА ====================
def parse_payment_message(text: str):
    """Парсим первую строку сообщения об оплате"""
    first_line = text.strip().split('\n')[0].strip()
    pattern = r'^(.+?)\s+по\s+(\d+(?:[.,]\d+)?)\s+\((\d+(?:[.,]\d+)?)\)'
    match = re.match(pattern, first_line)
    if not match:
        return None
    location = match.group(1).strip()
    amount = float(match.group(2).replace(',', '.'))
    hours = float(match.group(3).replace(',', '.'))

    # Ищем "за двох/трьох..." в остальных строках того же сообщения
    za_kilkokh = 0
    lines = text.strip().split('\n')
    for line in lines[1:]:
        wc = parse_workers_count(line.strip())
        if wc:
            za_kilkokh = wc
            break

    return {
        'location': location,
        'amount': amount,  # сколько реально заплачено вантажникам за цей блок
        'hours': hours,
        'za_kilkokh': za_kilkokh,
    }


def parse_workers_count(text: str):
    mapping = {
        'за двох': 2, 'за трьох': 3, 'за чотирьох': 4,
        "за п'ятьох": 5, 'за шістьох': 6,
    }
    return mapping.get(text.strip().lower())


def match_city_detailed(location: str, cities: dict):
    """Повертає (місто, суфікс) — суфікс це текст після назви міста/синоніма
    (наприклад 'Велес' для 'Івано-Франківськ Велес')."""
    loc_lower = location.lower()
    index = build_city_index(cities)
    best_key = None
    for key in index.keys():
        if loc_lower.startswith(key):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    if best_key is None:
        return None, location
    city = index[best_key]
    suffix = location[len(best_key):].strip()
    return city, (suffix if suffix else location)


def match_city(location: str, cities: dict):
    """Шукає місто за точним співпадінням назви або явного синоніма
    (тільки якщо location починається саме з цього слова)."""
    city, _ = match_city_detailed(location, cities)
    return city


def guess_city(raw: str, cities: dict) -> str:
    """Найкраще припущення, яке місто мав на увазі користувач у вільному тексті
    (наприклад "Ивано-Франковск", "ІФ", "ИФ", "і-ф"). Це лише підказка —
    не використовується ніде в фінансових розрахунках, тільки для довідки."""
    if not raw or not raw.strip():
        return ""

    text = normalize_apostrophes(raw.strip().lower())
    index = build_city_index(cities)

    # 1) точне співпадіння з назвою чи синонімом
    if text in index:
        return index[text]

    # 2) абревіатура: "ІФ" / "ИФ" / "і-ф" -> перші літери слів "Івано-Франківськ"
    translit = str.maketrans({"и": "і", "ы": "і"})
    letters_only = re.sub(r"[^а-яіїєґ]", "", text.translate(translit))
    if letters_only:
        for city_name in cities.keys():
            parts = re.split(r"[ \-]", city_name.lower().translate(translit))
            initials = "".join(p[0] for p in parts if p)
            if letters_only == initials:
                return city_name

    # 3) нечітке співпадіння (одруківки, транслітерація)
    close = difflib.get_close_matches(text, list(index.keys()), n=1, cutoff=0.6)
    if close:
        return index[close[0]]

    return ""


def format_hours(hours: float) -> str:
    if hours == int(hours):
        return str(int(hours))
    return str(hours)


def is_card_number(text: str) -> bool:
    cleaned = re.sub(r'[\s\-]', '', text.strip().lstrip('*').strip())
    return cleaned.isdigit() and len(cleaned) >= 12


def compute_location_data(report_date: str) -> list:
    """Повертає впорядкований список локацій з годинами/кількістю людей/сумою виплат/тарифом/доходом.
    Враховує ручні правки (overrides), якщо вони є."""
    messages = db_get_messages(report_date)
    if not messages:
        return []
    overrides = db_get_overrides(report_date)

    cities = load_cities()

    # Группируем сообщения по блокам
    blocks = []
    current_block = None

    for msg in messages:
        text = msg['text'].strip()
        payment = parse_payment_message(text)

        if payment:
            if current_block:
                blocks.append(current_block)
            current_block = {
                'location': payment['location'],
                'hours': payment['hours'],
                'za_kilkokh': payment['za_kilkokh'],
                'cards_count': 0,
                'raw_amount': payment['amount'],
            }
        elif current_block:
            wc = parse_workers_count(text)
            if wc:
                current_block['za_kilkokh'] += wc
            elif is_card_number(text):
                current_block['cards_count'] += 1

    if current_block:
        blocks.append(current_block)

    # Группируем блоки по локации
    grouped = {}
    grouped_order = []
    for block in blocks:
        loc = block['location']
        if loc not in grouped:
            grouped[loc] = {
                'location': loc,
                'hours': block['hours'] if block['hours'] > 0 else 0,
                'total_workers': 0,
                'paid_to_workers': 0.0,
            }
            grouped_order.append(loc)
        elif grouped[loc]['hours'] == 0 and block['hours'] > 0:
            # перше повідомлення для цієї локації мало (0) годин — беремо години з наступного блоку
            grouped[loc]['hours'] = block['hours']

        # Скільки людей отримали гроші за цей блок:
        # якщо є мітка "За N" — сума вже загальна на всіх N.
        # якщо мітки немає — сума вказана ЗА ОДНУ картку, і карток може бути декілька.
        if block['za_kilkokh'] > 0:
            block_workers = block['za_kilkokh']
            block_paid = block['raw_amount']
        else:
            block_workers = block['cards_count'] if block['cards_count'] > 0 else 1
            block_paid = block['raw_amount'] * block_workers

        # (0) годин — це особиста доплата (наприклад водію), яка йде тільки в мінус,
        # але не рахується як офіційний вантажник для звіту логістам
        if block['hours'] > 0:
            grouped[loc]['total_workers'] += block_workers

        grouped[loc]['paid_to_workers'] += block_paid

    # Застосовуємо ручні правки (якщо є)
    for loc in grouped_order:
        if loc in overrides:
            ov = overrides[loc]
            if 'hours' in ov:
                grouped[loc]['hours'] = ov['hours']
            if 'total_workers' in ov:
                grouped[loc]['total_workers'] = ov['total_workers']
            if 'paid_to_workers' in ov:
                grouped[loc]['paid_to_workers'] = ov['paid_to_workers']

    result = []
    for loc in grouped_order:
        data = grouped[loc]
        city, sub_label = match_city_detailed(data['location'], cities)
        my_rate = cities.get(city, {}).get('rate', 0) if city else 0
        hours = data['hours']
        total_workers = data['total_workers']
        my_total = my_rate * hours * total_workers

        result.append({
            'location': loc,
            'city': city,
            'sub_label': sub_label,
            'hours': hours,
            'total_workers': total_workers,
            'paid_to_workers': data['paid_to_workers'],
            'rate': my_rate,
            'income': my_total,
            'edited': loc in overrides,
        })

    return result


def build_report_and_stats(report_date: str):
    """Возвращает (список строк отчёта, словарь статистики), с учётом ручных правок."""
    locations = compute_location_data(report_date)
    if not locations:
        return [], {}

    workers_ua = {
        1: '', 2: 'За двох', 3: 'За трьох',
        4: 'За чотирьох', 5: "За п'ятьох", 6: 'За шістьох'
    }

    reports = []
    city_stats = {}
    total_paid = 0.0
    total_income = 0.0
    total_manhours = 0.0

    for item in locations:
        loc = item['location']
        hours = item['hours']
        total_workers = item['total_workers']
        my_total = item['income']
        paid = item['paid_to_workers']
        city = item['city']

        hours_str = format_hours(hours)
        if total_workers > 1:
            label = workers_ua.get(total_workers, f'За {total_workers}')
            report_text = f"{loc} по {int(my_total)} ({hours_str})\n{label}"
        else:
            report_text = f"{loc} по {int(my_total)} ({hours_str})"
        reports.append(report_text)

        total_paid += paid
        total_income += my_total
        total_manhours += hours * total_workers

        city_key = city if city else loc
        if city_key not in city_stats:
            city_stats[city_key] = {'paid': 0.0, 'income': 0.0, 'entries': []}
        city_stats[city_key]['paid'] += paid
        city_stats[city_key]['income'] += my_total
        city_stats[city_key]['entries'].append({
            'label': item['sub_label'],
            'profit': my_total - paid,
        })

    city_list = []
    for city_name, vals in city_stats.items():
        profit = vals['income'] - vals['paid']
        city_list.append({
            'city': city_name,
            'paid': vals['paid'],
            'income': vals['income'],
            'profit': profit,
            'entries': vals['entries'],
        })
    city_list.sort(key=lambda x: x['profit'], reverse=True)

    stats = {
        'total_deliveries': len(locations),
        'total_paid': total_paid,
        'total_income': total_income,
        'total_profit': total_income - total_paid,
        'total_manhours': total_manhours,
        'cities': city_list,
    }

    return reports, stats


def format_stats_message(report_date: str, stats: dict) -> str:
    if not stats:
        return "❌ Немає даних для статистики."

    lines = [f"📊 *Статистика за {report_date}*", ""]
    lines.append(f"*{stats['total_deliveries']} поставок*")
    lines.append("")
    lines.append(f"💸 Заплачено вантажникам: {int(stats['total_paid'])} грн")
    lines.append(f"💰 Отримаю за роботу: {int(stats['total_income'])} грн")

    profit = stats['total_profit']
    emoji = "📈" if profit >= 0 else "📉"
    lines.append(f"{emoji} Чистий прибуток: {int(profit)} грн")

    if stats['total_manhours'] > 0:
        lines.append(f"⏱ Людино-годин відпрацьовано: {stats['total_manhours']:.1f}")
        lines.append(f"📐 Маржа на людино-годину: {profit / stats['total_manhours']:.1f} грн")

    if stats['cities']:
        lines.append("")
        lines.append("🏙 *Міста за прибутковістю:*")
        for i, c in enumerate(stats['cities'], start=1):
            lines.append(f"{i}. {c['city']} — {int(c['profit'])} грн")
            if len(c['entries']) > 1:
                for entry in c['entries']:
                    sign = "+" if entry['profit'] >= 0 else ""
                    lines.append(f"    • {entry['label']} — {sign}{int(entry['profit'])} грн")

    return "\n".join(lines)


async def send_with_retry(bot, chat_id: int, text: str, parse_mode: str = None, max_retries: int = 5):
    """Отправка с обробкою Telegram flood control (RetryAfter) та інших тимчасових помилок."""
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
            return
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(f"Flood control, чекаємо {wait}с (спроба {attempt + 1})")
            await asyncio.sleep(wait)
        except Exception as e:
            logger.warning(f"Помилка відправки, чекаємо 2с: {e}")
            await asyncio.sleep(2)
    # остання спроба без придушення помилки — щоб вона стала видною в логах
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)


async def send_report_and_stats(bot, chat_id: int, report_date: str, reports: list, stats: dict, mode: str = "both"):
    if mode in ("report", "both"):
        for r in reports:
            await send_with_retry(bot, chat_id, r)
            await asyncio.sleep(0.3)

        # службові повідомлення для логістів: за яку дату звіт і куди переказати оплату
        await send_with_retry(bot, chat_id, f"ЗА {report_date}")
        await asyncio.sleep(0.3)
        await send_with_retry(bot, chat_id, MY_CARD_NUMBER)
        await asyncio.sleep(0.3)

    if mode in ("stats", "both"):
        # особистий підсумок — тільки для мене
        await send_with_retry(bot, chat_id, format_stats_message(report_date, stats), parse_mode="Markdown")


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != REPORT_GROUP_ID:
        return
    if msg.from_user.id not in ALLOWED_USERS:
        return

    text = msg.text or ""
    if not text:
        return
    if text in BUTTON_TEXTS:
        return  # это нажатие кнопки меню, а не оплата — не перехватываем

    user_id = msg.from_user.id
    if user_id in edit_state:
        return  # це введення нового значення при редагуванні позиції, а не оплата
    if user_id in worker_flow_state:
        return  # це введення даних працівника, а не оплата
    if user_id not in current_report_date:
        return

    report_date = current_report_date[user_id]
    if report_date == "waiting_date":
        return

    db_add_message(report_date, text, msg.date.isoformat(), msg.message_id)


# ==================== KEYBOARDS ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📦 Поставки"],
            ["🏙 Мої міста", "📊 Звіт"],
            ["🗂 Мої звіти", "👷 Робітники"],
            ["📇 Кандидати"],
        ],
        resize_keyboard=True
    )


def get_reports_list_keyboard():
    dates = db_list_report_dates()
    keyboard = [[InlineKeyboardButton(d, callback_data=f"rdate_{d}")] for d in dates]
    return keyboard, dates


def get_workers_list_keyboard():
    workers = db_get_workers()
    keyboard = [[InlineKeyboardButton(w["name"] or f"#{w['id']}", callback_data=f"wview_{w['id']}")] for w in workers]
    keyboard.append([InlineKeyboardButton("➕ Додати працівника", callback_data="waddnew")])
    return keyboard, workers


def get_candidates_list_keyboard():
    candidates = db_list_pending_contacts()
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
        guess = guess_city(c["city_raw"], load_cities())
        if guess and guess.lower() != c["city_raw"].strip().lower():
            lines.append(f"   ймовірно: {guess}")
    lines.append(f"🆔 {c['telegram_id']}")
    return "\n".join(lines)


def format_worker_card(w: dict) -> str:
    lines = [f"👷 *{w['name']}*"]
    if w["phone"]:
        lines.append(f"📞 {w['phone']}")
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
    w = db_get_worker(worker_id)
    keyboard = []

    if w and (w.get("telegram_id") or w.get("username")):
        chat_url = f"tg://user?id={w['telegram_id']}" if w.get("telegram_id") else f"https://t.me/{w['username']}"
        keyboard.append([InlineKeyboardButton("💬 Написати", url=chat_url)])

    keyboard += [
        [
            InlineKeyboardButton("✏️ Ім'я", callback_data=f"wf_{worker_id}_name"),
            InlineKeyboardButton("✏️ Телефон", callback_data=f"wf_{worker_id}_phone"),
        ],
        [
            InlineKeyboardButton("✏️ Місто", callback_data=f"wf_{worker_id}_city"),
            InlineKeyboardButton("✏️ Username", callback_data=f"wf_{worker_id}_username"),
        ],
        [InlineKeyboardButton("✏️ Картка", callback_data=f"wf_{worker_id}_card")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"wdel_{worker_id}")],
        [InlineKeyboardButton("◀️ До списку", callback_data="wback")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ==================== ВІДПРАВКА ПОСТАВОК ====================
SOURCE_EMOJI = {"fm": "🔴", "ekol": "🔵"}
SOURCE_LABEL = {"fm": "FM", "ekol": "Ekol"}


async def send_deliveries_query(query, context: ContextTypes.DEFAULT_TYPE, source: str = "fm", filter_date: date = None):
    chat_id = query.message.chat_id
    bot = context.bot
    emoji = SOURCE_EMOJI.get(source, "")
    label = SOURCE_LABEL.get(source, source)
    await query.edit_message_text(f"⏳ {emoji} Завантажую дані з таблиці {label}...")
    try:
        all_values, merged_cells = get_sheet_data(source=source)

        if source == "ekol":
            messages = parse_ekol_deliveries(all_values, filter_date=filter_date)
        else:
            routes = parse_routes(all_values, merged_cells)
            messages = build_delivery_messages(routes, merged_cells, filter_date=filter_date)

        if not messages:
            date_info = filter_date.strftime("%d.%m.%Y") if filter_date else ""
            await send_with_retry(
                bot, chat_id,
                f"❌ {emoji} Поставок {label} {'на ' + date_info if date_info else ''} не знайдено."
            )
            return

        date_info = filter_date.strftime("%d.%m.%Y") if filter_date else "всі"
        await send_with_retry(
            bot, chat_id,
            f"✅ {emoji} {label}: знайдено поставок *{len(messages)}* (дата: {date_info})",
            parse_mode="Markdown"
        )

        for msg_data in messages:
            await send_with_retry(bot, chat_id, msg_data["text"], parse_mode="Markdown")
            await asyncio.sleep(0.35)

    except Exception as e:
        logger.error(f"Помилка: {e}", exc_info=True)
        await send_with_retry(bot, chat_id, f"❌ Помилка: {str(e)}")


# ==================== КОМАНДИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_touch_contact(user)

    if user.id not in ALLOWED_USERS:
        anketa_state[user.id] = {"step": "name", "data": {}}
        await update.message.reply_text(
            "👋 Вітаю! Щоб зв'язатися з вами щодо роботи, надішліть, будь ласка, "
            "невелику інформацію про себе.\n\nПрізвище та ім'я:"
        )
        return

    await update.message.reply_text(
        "👋 Привіт! Я бот для поставок FM Logistics.\n\nОбери розділ:",
        reply_markup=get_main_keyboard()
    )


async def notify_admins_new_contact(bot, user, data: dict):
    text = (
        f"📇 *Нова заявка*\n\n"
        f"Прізвище та ім'я: {data.get('full_name', '—')}\n"
        f"Вік: {data.get('age', '—')}\n"
        f"Телефон: {data.get('phone', '—')}\n"
        f"Місто (як написав): {data.get('city', '—')}\n"
        f"Telegram: {'@' + user.username if user.username else '(немає username)'}\n"
        f"ID: {user.id}"
    )
    for admin_id in ALLOWED_USERS:
        try:
            await send_with_retry(bot, admin_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Не вдалося сповістити {admin_id} про нову заявку: {e}")


async def handle_anketa_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = anketa_state[user.id]
    raw = update.message.text.strip()
    step = state["step"]

    if step == "name":
        if not raw:
            await update.message.reply_text("Введіть, будь ласка, прізвище та ім'я:")
            return
        state["data"]["full_name"] = raw
        state["step"] = "age"
        await update.message.reply_text("Вік:")
        return

    if step == "age":
        state["data"]["age"] = raw
        state["step"] = "phone"
        await update.message.reply_text("Номер телефону:")
        return

    if step == "phone":
        state["data"]["phone"] = raw
        state["step"] = "city"
        await update.message.reply_text("З якого ви міста?")
        return

    if step == "city":
        state["data"]["city"] = raw
        db_save_anketa(user.id, state["data"])
        del anketa_state[user.id]
        await update.message.reply_text("✅ Дякую! Ми зв'яжемося з вами за потреби.")
        await notify_admins_new_contact(context.bot, user, state["data"])
        return


async def mycities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    cities = load_cities()
    if not cities:
        await update.message.reply_text("❌ Список міст порожній.")
        return
    text = "🏙 *Мої міста:*\n\n"
    for city, info in sorted(cities.items()):
        rate = info.get('rate', 0)
        aliases = info.get('aliases', [])
        text += f"📍 {city} — {rate} грн/год"
        if aliases:
            text += f" (синоніми: {', '.join(aliases)})"
        text += "\n"
    text += (
        "\nЩоб додати місто: /addcity Назва Тариф"
        "\nЩоб видалити місто: /removecity Назва"
        "\nЩоб додати синонім: /addalias Назва Синонім"
        "\nЩоб видалити синонім: /removealias Синонім"
    )
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
    existing_aliases = cities.get(city_name, {}).get('aliases', [])
    cities[city_name] = {"rate": rate, "aliases": existing_aliases}
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


async def addalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Використання: /addalias Назва Синонім\n"
            "Приклад: /addalias Могилів-Подільський Могилів"
        )
        return
    alias = context.args[-1]
    city_name = " ".join(context.args[:-1])
    cities = load_cities()
    if city_name not in cities:
        await update.message.reply_text(
            f"❌ Місто '{city_name}' не знайдено. Спочатку додайте його: /addcity {city_name} <тариф>"
        )
        return
    aliases = cities[city_name].get('aliases', [])
    if alias.lower() in [a.lower() for a in aliases]:
        await update.message.reply_text(f"⚠️ Синонім '{alias}' вже є у '{city_name}'.")
        return
    aliases.append(alias)
    cities[city_name]['aliases'] = aliases
    save_cities(cities)
    await update.message.reply_text(f"✅ Додано синонім: '{alias}' → {city_name}")


async def removealias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Використання: /removealias Синонім\nПриклад: /removealias Могилів")
        return
    alias = " ".join(context.args)
    cities = load_cities()
    for city_name, info in cities.items():
        aliases = info.get('aliases', [])
        new_aliases = [a for a in aliases if a.lower() != alias.lower()]
        if len(new_aliases) != len(aliases):
            info['aliases'] = new_aliases
            save_cities(cities)
            await update.message.reply_text(f"✅ Синонім '{alias}' видалено з '{city_name}'.")
            return
    await update.message.reply_text(f"❌ Синонім '{alias}' не знайдено.")


# ==================== TEXT HANDLER ====================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_touch_contact(user)

    if user.id not in ALLOWED_USERS:
        if user.id in anketa_state:
            await handle_anketa_step(update, context)
        return

    user_id = user.id

    if user_id in worker_flow_state:
        state = worker_flow_state[user_id]
        raw = update.message.text.strip()
        mode = state["mode"]

        if mode == "add_name":
            if not raw:
                await update.message.reply_text("❌ Ім'я не може бути порожнім. Введіть ім'я:")
                return
            state["data"]["name"] = raw
            state["mode"] = "add_phone"
            await update.message.reply_text("Телефон (або «-», щоб пропустити):")
            return

        if mode == "add_phone":
            state["data"]["phone"] = "" if raw == "-" else raw
            state["mode"] = "add_username"
            await update.message.reply_text("Username в Telegram без @ (або «-», щоб пропустити):")
            return

        if mode == "add_username":
            state["data"]["username"] = "" if raw == "-" else raw.lstrip("@")
            state["mode"] = "add_card"
            await update.message.reply_text("Номер картки (або «-», щоб пропустити):")
            return

        if mode == "add_card":
            state["data"]["card"] = "" if raw == "-" else raw
            worker_id = db_add_worker(**state["data"])
            del worker_flow_state[user_id]
            w = db_get_worker(worker_id)
            await update.message.reply_text(
                f"✅ Додано працівника:\n\n{format_worker_card(w)}",
                parse_mode="Markdown",
                reply_markup=get_worker_card_keyboard(worker_id)
            )
            return

        if mode == "edit_field":
            worker_id = state["worker_id"]
            field = state["field"]
            value = "" if raw == "-" else (raw.lstrip("@") if field == "username" else raw)
            db_update_worker(worker_id, field, value)
            del worker_flow_state[user_id]
            w = db_get_worker(worker_id)
            if w:
                await update.message.reply_text(
                    f"✅ Оновлено.\n\n{format_worker_card(w)}",
                    parse_mode="Markdown",
                    reply_markup=get_worker_card_keyboard(worker_id)
                )
            return

    if user_id in edit_state:
        state = edit_state[user_id]
        text_input = update.message.text.strip().replace(',', '.')
        field = state['field']
        try:
            value = float(text_input)
            if field == 'workers':
                value = int(round(value))
            if value < 0:
                raise ValueError("negative")
        except ValueError:
            await update.message.reply_text("❌ Введіть коректне число (наприклад 2 або 2.5)")
            return

        report_date = state['date']
        location = state['location']
        override_key = FIELD_TO_OVERRIDE_KEY[field]

        db_set_override(report_date, location, override_key, value)

        del edit_state[user_id]

        keyboard = [
            [InlineKeyboardButton("✏️ Редагувати ще", callback_data=f"rloc_{report_date}_{state['idx']}")],
            [InlineKeyboardButton("◀️ До списку позицій", callback_data=f"redit_{report_date}")],
        ]
        await update.message.reply_text(
            f"✅ Оновлено: {location} → {FIELD_LABELS[field]} = {value}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if current_report_date.get(user_id) == "waiting_date":
        text_input = update.message.text.strip()
        try:
            datetime.strptime(text_input, "%d.%m.%Y")
            current_report_date[user_id] = text_input
            keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
            await update.message.reply_text(
                f"✅ Дата звіту: {text_input}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except ValueError:
            await update.message.reply_text("❌ Невірний формат. Введіть дату як ДД.ММ.РРРР")
        return

    text = update.message.text

    if text == "📦 Поставки":
        keyboard = [
            [InlineKeyboardButton("🔴 FM", callback_data="dlvsrc_fm")],
            [InlineKeyboardButton("🔵 Ekol", callback_data="dlvsrc_ekol")],
        ]
        await update.message.reply_text(
            "Оберіть таблицю:",
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
    elif text == "🗂 Мої звіти":
        keyboard, dates = get_reports_list_keyboard()
        if not dates:
            await update.message.reply_text("❌ Поки немає жодного звіту.")
        else:
            await update.message.reply_text(
                "Оберіть дату звіту:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif text == "👷 Робітники":
        keyboard, workers = get_workers_list_keyboard()
        msg = "Робітники:" if workers else "❌ Поки немає жодного працівника."
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif text == "📇 Кандидати":
        keyboard, candidates = get_candidates_list_keyboard()
        msg = "Нові заявки (ще не в реєстрі):" if candidates else "❌ Поки немає нових заявок."
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


# ==================== CALLBACK HANDLER ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS:
        await query.answer("⛔ Доступ заборонено.")
        return
    await query.answer()
    data = query.data

    if data.startswith("dlvsrc_"):
        source = data.replace("dlvsrc_", "")
        label = SOURCE_LABEL.get(source, source)
        emoji = SOURCE_EMOJI.get(source, "")
        keyboard = [
            [InlineKeyboardButton("📋 Всі поставки", callback_data=f"dlv_all_{source}")],
            [InlineKeyboardButton("📅 На сьогодні", callback_data=f"dlv_today_{source}")],
            [InlineKeyboardButton("📅 На завтра", callback_data=f"dlv_tomorrow_{source}")],
            [InlineKeyboardButton("🔢 На конкретну дату", callback_data=f"dlv_pick_{source}")],
        ]
        await query.edit_message_text(
            f"{emoji} {label}. Обери що показати:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("dlv_all_"):
        source = data.replace("dlv_all_", "")
        await send_deliveries_query(query, context, source=source, filter_date=None)

    elif data.startswith("dlv_today_"):
        source = data.replace("dlv_today_", "")
        await send_deliveries_query(query, context, source=source, filter_date=date.today())

    elif data.startswith("dlv_tomorrow_"):
        source = data.replace("dlv_tomorrow_", "")
        await send_deliveries_query(query, context, source=source, filter_date=date.today() + timedelta(days=1))

    elif data.startswith("dlv_pick_"):
        source = data.replace("dlv_pick_", "")
        keyboard = []
        for i in range(7):
            d = date.today() + timedelta(days=i)
            label = d.strftime("%d.%m.%Y")
            keyboard.append([InlineKeyboardButton(label, callback_data=f"date_{label}_{source}")])
        await query.edit_message_text("Оберіть дату:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("date_"):
        rest = data.replace("date_", "")
        date_str, source = rest.rsplit("_", 1)
        try:
            filter_date = datetime.strptime(date_str, "%d.%m.%Y").date()
            await send_deliveries_query(query, context, source=source, filter_date=filter_date)
        except ValueError:
            await query.edit_message_text("Помилка дати")

    elif data == "report_yesterday":
        d = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
        current_report_date[query.from_user.id] = d
        keyboard = [[InlineKeyboardButton("📋 Сформувати звіт", callback_data="build_report")]]
        await query.edit_message_text(
            f"✅ Дата звіту: {d}\n\nТепер скидайте оплати в групу.\nКоли закінчите — натисніть кнопку нижче.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "report_today":
        d = date.today().strftime("%d.%m.%Y")
        current_report_date[query.from_user.id] = d
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
        reports, stats = build_report_and_stats(report_date)
        if not reports:
            await query.edit_message_text("❌ Немає даних для звіту.")
            return
        keyboard = [
            [InlineKeyboardButton("📋 Тільки звіт", callback_data=f"show_{report_date}_report")],
            [InlineKeyboardButton("📊 Тільки статистика", callback_data=f"show_{report_date}_stats")],
            [InlineKeyboardButton("📋+📊 Все разом", callback_data=f"show_{report_date}_both")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"show_{report_date}_cancel")],
        ]
        await query.edit_message_text(
            f"Звіт за {report_date} готовий. Що показати?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "rback":
        keyboard, dates = get_reports_list_keyboard()
        if not dates:
            await query.edit_message_text("❌ Поки немає жодного звіту.")
        else:
            await query.edit_message_text("Оберіть дату звіту:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("rdelconfirm_"):
        d = data.replace("rdelconfirm_", "")
        db_delete_report(d)
        await query.edit_message_text(f"✅ Звіт за {d} видалено.")

    elif data.startswith("rdel_"):
        d = data.replace("rdel_", "")
        keyboard = [
            [InlineKeyboardButton("✅ Так, видалити", callback_data=f"rdelconfirm_{d}")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"rdate_{d}")],
        ]
        await query.edit_message_text(
            f"Видалити звіт за {d}? Цю дію не можна скасувати.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("rview_"):
        d = data.replace("rview_", "")
        reports, stats = build_report_and_stats(d)
        if not reports:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return
        keyboard = [
            [InlineKeyboardButton("📋 Тільки звіт", callback_data=f"show_{d}_report")],
            [InlineKeyboardButton("📊 Тільки статистика", callback_data=f"show_{d}_stats")],
            [InlineKeyboardButton("📋+📊 Все разом", callback_data=f"show_{d}_both")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"show_{d}_cancel")],
        ]
        await query.edit_message_text(
            f"Звіт за {d} готовий. Що показати?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("rdate_"):
        d = data.replace("rdate_", "")
        keyboard = [
            [InlineKeyboardButton("👁 Переглянути", callback_data=f"rview_{d}")],
            [InlineKeyboardButton("✏️ Редагувати", callback_data=f"redit_{d}")],
            [InlineKeyboardButton("🗑 Видалити", callback_data=f"rdel_{d}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="rback")],
        ]
        await query.edit_message_text(f"Звіт за {d}:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("redit_"):
        d = data.replace("redit_", "")
        locations = compute_location_data(d)
        if not locations:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return
        keyboard = []
        for idx, item in enumerate(locations):
            mark = "✏️ " if item['edited'] else ""
            label = f"{mark}{item['location']} — {int(item['income'])} грн ({item['total_workers']} люд.)"
            if len(label) > 64:
                label = label[:61] + "..."
            keyboard.append([InlineKeyboardButton(label, callback_data=f"rloc_{d}_{idx}")])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"rdate_{d}")])
        await query.edit_message_text(
            f"Позиції звіту за {d}\n(✏️ — вже відредаговано):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("rloc_"):
        rest = data.replace("rloc_", "")
        d, idx_str = rest.rsplit("_", 1)
        idx = int(idx_str)
        locations = compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        item = locations[idx]
        text = (
            f"📍 {item['location']}\n\n"
            f"⏱ Години: {format_hours(item['hours'])}\n"
            f"👷 Кількість людей: {item['total_workers']}\n"
            f"💸 Сума виплат: {int(item['paid_to_workers'])} грн\n"
            f"💰 Дохід (за тарифом): {int(item['income'])} грн"
        )
        keyboard = [
            [InlineKeyboardButton("⏱ Змінити години", callback_data=f"rf_{d}_{idx}_hours")],
            [InlineKeyboardButton("👷 Змінити кількість людей", callback_data=f"rf_{d}_{idx}_workers")],
            [InlineKeyboardButton("💸 Змінити суму виплат", callback_data=f"rf_{d}_{idx}_paid")],
        ]
        if item['edited']:
            keyboard.append([InlineKeyboardButton("♻️ Скинути правки", callback_data=f"rreset_{d}_{idx}")])
        keyboard.append([InlineKeyboardButton("◀️ До списку позицій", callback_data=f"redit_{d}")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("rf_"):
        rest = data.replace("rf_", "")
        d, idx_str, field = rest.rsplit("_", 2)
        idx = int(idx_str)
        locations = compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        location = locations[idx]['location']
        edit_state[query.from_user.id] = {"date": d, "location": location, "field": field, "idx": idx}
        await query.edit_message_text(
            f"📍 {location}\n\nВведіть нове значення для «{FIELD_LABELS[field]}»:"
        )

    elif data.startswith("rreset_"):
        rest = data.replace("rreset_", "")
        d, idx_str = rest.rsplit("_", 1)
        idx = int(idx_str)
        locations = compute_location_data(d)
        if idx >= len(locations):
            await query.edit_message_text(f"❌ Позицію не знайдено.")
            return
        location = locations[idx]['location']
        db_delete_override(d, location)
        await query.edit_message_text(f"✅ Правки для '{location}' скинуто до початкових значень.")

    elif data.startswith("show_"):
        rest = data.replace("show_", "")
        d, action = rest.rsplit("_", 1)

        if action == "cancel":
            await query.edit_message_text("Скасовано.")
            return

        reports, stats = build_report_and_stats(d)
        if not reports:
            await query.edit_message_text(f"❌ Немає даних для звіту за {d}.")
            return

        if action == "stats":
            await query.edit_message_text(f"📊 Статистика за {d}:")
        else:
            await query.edit_message_text(f"✅ Звіт за {d}:")

        await send_report_and_stats(context.bot, query.message.chat_id, d, reports, stats, mode=action)

    elif data == "waddnew":
        worker_flow_state[query.from_user.id] = {"mode": "add_name", "data": {}}
        await query.edit_message_text("Введіть ім'я нового працівника:")

    elif data == "wback":
        keyboard, workers = get_workers_list_keyboard()
        msg = "Робітники:" if workers else "❌ Поки немає жодного працівника."
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wdelconfirm_"):
        worker_id = int(data.replace("wdelconfirm_", ""))
        db_delete_worker(worker_id)
        keyboard, workers = get_workers_list_keyboard()
        msg = "✅ Видалено.\n\n" + ("Робітники:" if workers else "Поки немає жодного працівника.")
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wdel_"):
        worker_id = int(data.replace("wdel_", ""))
        keyboard = [
            [InlineKeyboardButton("✅ Так, видалити", callback_data=f"wdelconfirm_{worker_id}")],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"wview_{worker_id}")],
        ]
        await query.edit_message_text("Видалити цього працівника?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wf_"):
        rest = data.replace("wf_", "")
        worker_id_str, field = rest.rsplit("_", 1)
        worker_id = int(worker_id_str)
        worker_flow_state[query.from_user.id] = {"mode": "edit_field", "worker_id": worker_id, "field": field}
        await query.edit_message_text(f"Введіть нове значення для «{WORKER_FIELD_LABELS[field]}» (або «-», щоб очистити):")

    elif data.startswith("wview_"):
        worker_id = int(data.replace("wview_", ""))
        w = db_get_worker(worker_id)
        if not w:
            await query.edit_message_text("❌ Працівника не знайдено.")
            return
        await query.edit_message_text(
            format_worker_card(w),
            parse_mode="Markdown",
            reply_markup=get_worker_card_keyboard(worker_id)
        )

    elif data.startswith("cand_"):
        telegram_id = int(data.replace("cand_", ""))
        c = db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return
        keyboard = [
            [InlineKeyboardButton("➕ Додати в реєстр", callback_data=f"wclaim_{telegram_id}")],
            [InlineKeyboardButton("❌ Відхилити", callback_data=f"canddismiss_{telegram_id}")],
            [InlineKeyboardButton("◀️ До списку", callback_data="candback")],
        ]
        await query.edit_message_text(
            format_candidate_card(c),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "candback":
        keyboard, candidates = get_candidates_list_keyboard()
        msg = "Нові заявки (ще не в реєстрі):" if candidates else "❌ Поки немає нових заявок."
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("canddismiss_"):
        telegram_id = int(data.replace("canddismiss_", ""))
        db_dismiss_contact(telegram_id)
        keyboard, candidates = get_candidates_list_keyboard()
        msg = "✅ Заявку відхилено.\n\n" + ("Нові заявки (ще не в реєстрі):" if candidates else "Поки немає нових заявок.")
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("wclaim_"):
        telegram_id = int(data.replace("wclaim_", ""))
        c = db_get_contact(telegram_id)
        if not c:
            await query.edit_message_text("❌ Кандидата не знайдено.")
            return
        worker_id = db_add_worker(
            name=c.get("full_name_ua") or c.get("first_name") or f"ID {telegram_id}",
            phone=c.get("phone") or "",
            username=c.get("username") or "",
            telegram_id=telegram_id,
            city=c.get("city_raw") or "",
        )
        db_mark_converted(telegram_id, worker_id)
        w = db_get_worker(worker_id)
        await query.edit_message_text(
            f"✅ Додано в реєстр:\n\n{format_worker_card(w)}",
            parse_mode="Markdown",
            reply_markup=get_worker_card_keyboard(worker_id)
        )


# ==================== MAIN ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mycities", mycities))
    app.add_handler(CommandHandler("addcity", addcity))
    app.add_handler(CommandHandler("removecity", removecity))
    app.add_handler(CommandHandler("addalias", addalias))
    app.add_handler(CommandHandler("removealias", removealias))

    # group=0: сначала пробуем перехватить сообщение в группе как "оплату"
    app.add_handler(MessageHandler(
        filters.Chat(REPORT_GROUP_ID) & filters.TEXT & ~filters.COMMAND,
        group_message_handler
    ), group=0)

    # group=1: этот хендлер получит апдейт независимо от того, что сделал group_message_handler,
    # поэтому кнопки в группе тоже обрабатываются
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler), group=1)

    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Бот запущено!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
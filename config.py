import logging
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]  # токен берём тільки з env
FM_SPREADSHEET_ID = "1x-vsC2M1cLtitP2DF04EqkSB4emVwvyh4N3jaauLqZ4"
EKOL_SPREADSHEET_ID = os.environ.get("EKOL_SPREADSHEET_ID", "")  # поки не налаштовано

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
WEB_PORT = int(os.environ.get("PORT", 8080))
CREDENTIALS_FILE = "credentials.json"

# RAILWAY_VOLUME_MOUNT_PATH встановлюється Railway автоматично, якщо до сервісу
# прикріплено Volume. Локально (в PyCharm) цієї змінної немає — тоді файли
# зберігаються поруч зі скриптом, як і раніше.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
CITIES_FILE = os.path.join(DATA_DIR, "cities.json")       # тільки для одноразової міграції
REPORTS_FILE = os.path.join(DATA_DIR, "reports.json")     # тільки для одноразової міграції
DB_FILE = os.path.join(DATA_DIR, "postavo4ki.db")
ALLOWED_USERS = [7305470549, 506094120]
REPORT_GROUP_ID = -1004382686176  # оновлено 01.08.2026: групу "Оплати" сконвертувало в супергрупу, старий ID (-5344273524) більше не діє
MY_CARD_NUMBER = "4441111134286644"

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def kyiv_today() -> date:
    """'Сьогодні' саме по київському часу, а не по часовому поясу сервера
    (Railway працює в UTC, тому звичайний date.today() там відставав би на 2-3 години опівночі)."""
    return datetime.now(KYIV_TZ).date()


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

SOURCE_EMOJI = {"fm": "🔴", "ekol": "🔵"}
SOURCE_LABEL = {"fm": "FM", "ekol": "Ekol"}
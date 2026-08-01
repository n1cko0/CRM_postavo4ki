import db

current_report_date = {}  # user_id -> "дд.мм.рррр" | "waiting_date" ; дублюється в user_state (SQLite), щоб переживати рестарт
edit_state = {}  # user_id -> {"date": ..., "location": ..., "field": "hours"|"workers"|"paid"}
worker_flow_state = {}  # user_id -> {"mode": "add_name"|"add_phone"|"add_username"|"add_card"|"edit_field", ...}
anketa_state = {}  # user_id -> {"step": "name"|"age"|"phone", "data": {...}} — для незнайомих людей (не ALLOWED_USERS)


def set_report_date(user_id: int, value: str):
    """Записує вибрану дату звіту одразу і в пам'ять, і в БД —
    щоб вона не губилась при рестарті бота (Railway може перезапускати сервіс сам)."""
    current_report_date[user_id] = value
    db.db_set_user_report_date(user_id, value)
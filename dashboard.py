import base64
import hashlib
import hmac
import html as html_module
import os
import re
import time
from datetime import datetime, date, timedelta

from aiohttp import web

import config
import db
import parsing


# ==================== ВЕБ-ДАШБОРД МАРШРУТІВ ====================
def extract_phone_from_card(text: str):
    m = re.search(r'📞\s*(.+)$', text.strip())
    return m.group(1).strip() if m else None


def extract_time_minutes(text: str) -> int:
    m = re.search(r'🕐\s*(\d{1,2}):(\d{2})', text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 99999  # без часу — в кінець списку


def telegram_md_to_html(text: str) -> str:
    escaped = html_module.escape(text)
    escaped = re.sub(r'\*(.+?)\*', r'<b>\1</b>', escaped)
    return escaped.replace("\n", "<br>")


def build_driver_columns(target_date: date, date_str: str) -> dict:
    """Групує поставки FM+Ekol на дату по водію (за телефоном) і підвантажує
    реальні призначення робітників з бази — те, що бачить бот."""
    all_messages = []

    try:
        all_values, merged_cells = parsing.get_sheet_data(source="fm")
        routes = parsing.parse_routes(all_values, merged_cells)
        fm_msgs = parsing.build_delivery_messages(routes, merged_cells, filter_date=target_date)
        for m in fm_msgs:
            m["source"] = "fm"
        all_messages += fm_msgs
    except Exception as e:
        config.logger.error(f"Дашборд: помилка завантаження FM: {e}", exc_info=True)

    if config.EKOL_SPREADSHEET_ID:
        try:
            all_values_ekol, _ = parsing.get_sheet_data(source="ekol")
            ekol_msgs = parsing.parse_ekol_deliveries(all_values_ekol, filter_date=target_date)
            for m in ekol_msgs:
                m["source"] = "ekol"
            all_messages += ekol_msgs
        except Exception as e:
            config.logger.error(f"Дашборд: помилка завантаження Ekol: {e}", exc_info=True)

    columns = {}
    for m in all_messages:
        phone = extract_phone_from_card(m["text"]) or "Без номера водія"
        delivery_key = db.make_delivery_key(m["source"], m["date_str"], m["text"], needed=m.get("workers_needed"))
        assigned = db.db_get_assigned_workers(delivery_key)
        columns.setdefault(phone, []).append({
            "text": m["text"],
            "source": m["source"],
            "assigned": assigned,
            "needed": m.get("workers_needed"),
            "sort_key": extract_time_minutes(m["text"]),
        })

    for phone in columns:
        columns[phone].sort(key=lambda x: x["sort_key"])

    return columns


PX_PER_HOUR = 110
CARD_CENTER_OFFSET = 56  # приблизна половина висоти картки — щоб нитка проходила саме через центр
MIN_CARD_GAP = 110       # мінімальна відстань по вертикалі між сусідніми картками одного водія
HEADER_OFFSET = 44       # місце під заголовок колонки (номер водія) над першою карткою
DRIVER_COLORS = ["#2F5D46", "#3D5A73", "#8A5E74", "#8A6F2F", "#5E5A8A", "#2F7A6B", "#7A4A4A"]


def compute_time_range(columns: dict):
    minutes = [it["sort_key"] for items in columns.values() for it in items if it["sort_key"] < 99999]
    if not minutes:
        return 6 * 60, 20 * 60
    lo = max(0, (min(minutes) // 60) * 60 - 60)
    hi = min(24 * 60, ((max(minutes) // 60) + 1) * 60 + 60)
    if hi - lo < 120:
        hi = lo + 120
    return lo, hi


def assign_card_positions(columns: dict, lo: int) -> float:
    """Проставляє item['top'] (у пікселях, без урахування HEADER_OFFSET) для кожної картки.
    Повертає найбільший 'top' серед усіх колонок."""
    overall_max = 0.0
    for phone, items in columns.items():
        with_time = [it for it in items if it["sort_key"] < 99999]
        without_time = [it for it in items if it["sort_key"] >= 99999]

        prev_top = None
        for it in with_time:
            top = (it["sort_key"] - lo) / 60 * PX_PER_HOUR
            if prev_top is not None and top < prev_top + MIN_CARD_GAP:
                top = prev_top + MIN_CARD_GAP
            it["top"] = top
            prev_top = top

        next_top = (prev_top + MIN_CARD_GAP) if prev_top is not None else 0.0
        for it in without_time:
            it["top"] = next_top
            next_top += MIN_CARD_GAP

        col_items = with_time + without_time
        if col_items:
            overall_max = max(overall_max, max(it["top"] for it in col_items))

    return overall_max


def build_ruler_html(lo: int, hi: int, total_height: float) -> str:
    step = 60 if (hi - lo) <= 14 * 60 else 120
    ticks = ""
    m = lo
    while m <= hi:
        top = HEADER_OFFSET + (m - lo) / 60 * PX_PER_HOUR
        ticks += f'<div class="tick" style="top:{top}px;">{m // 60:02d}:{m % 60:02d}</div>'
        m += step

    periods = [
        ("РАНОК", lo, min(hi, 11 * 60)),
        ("ДЕНЬ", max(lo, 11 * 60), min(hi, 17 * 60)),
        ("ВЕЧІР", max(lo, 17 * 60), hi),
    ]
    period_html = ""
    for label, seg_lo, seg_hi in periods:
        if seg_hi > seg_lo:
            mid = (seg_lo + seg_hi) / 2
            top = HEADER_OFFSET + (mid - lo) / 60 * PX_PER_HOUR
            period_html += f'<div class="period" style="top:{top}px;">{label}</div>'

    return f'<div class="ruler" style="height:{total_height}px;">{period_html}{ticks}</div>'


DASHBOARD_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html, body { height:100%; overflow:hidden; background:#E9E9E7; font-family:'Inter',-apple-system,sans-serif; color:#14201A; }
.app { display:flex; flex-direction:column; height:100vh; }
.header { padding:16px 20px; flex-shrink:0; }
.header-title { font-size:16px; font-weight:700; }
.nav { display:flex; align-items:center; gap:12px; margin-top:6px; }
.nav a { text-decoration:none; color:#2F5D46; font-weight:600; font-size:14px; padding:4px 10px; border-radius:8px; background:#F7F7F4; border:1px solid #ECECE8; }
.nav span { font-size:13px; color:#6B6B68; }
.board { flex:1; overflow:auto; -webkit-overflow-scrolling:touch; touch-action:pan-x pan-y; position:relative; background:#E9E9E7; }
.board-spacer { position:relative; }
.board-inner {
  position:absolute; top:0; left:0; display:flex; align-items:flex-start; gap:14px; padding:20px 20px 40px;
  transform-origin:0 0; will-change:transform;
  background-image: radial-gradient(circle, #D6D6D0 1.6px, transparent 1.6px);
  background-size: 22px 22px;
  background-position: 6px 6px;
}
.ruler { flex:0 0 56px; position:relative; }
.ruler .tick { position:absolute; left:0; right:8px; text-align:right; font-size:10.5px; color:#ADADA8; font-weight:500; transform:translateY(-50%); white-space:nowrap; }
.ruler .tick::after { content:''; position:absolute; right:-8px; top:50%; width:6px; height:1px; background:#C7C7C1; }
.ruler .period { position:absolute; left:0; font-size:9px; letter-spacing:0.1em; color:#B7B7B0; font-weight:700; writing-mode:vertical-rl; text-orientation:mixed; transform:translateY(-50%); }
.col { flex:0 0 250px; position:relative; }
.col-head { font-size:13px; font-weight:700; padding:8px 6px; color:#14201A; }
.card {
  position:absolute; left:6px; right:6px; z-index:2;
  background:#F7F7F4; border:1px solid #ECECE8; border-radius:12px; border-left-width:3px;
  padding:10px 12px; font-size:12.5px; line-height:1.5;
  box-shadow:0 1px 2px rgba(0,0,0,0.03), 0 4px 10px rgba(0,0,0,0.04);
}
.thread { position:absolute; left:calc(50% - 1px); width:2px; z-index:1; border-radius:2px; opacity:0.55; }
.badge { display:inline-block; font-size:10.5px; font-weight:600; padding:2px 8px; border-radius:20px; margin-top:6px; margin-right:4px; }
.badge.ok { background:#DCE8E0; color:#2F5D46; }
.badge.need { background:#F5E3D8; color:#8A5E2F; }
.worker-chip { display:inline-block; font-size:11px; background:#EFEFEA; color:#3D5A46; padding:2px 8px; border-radius:20px; margin:2px 4px 0 0; text-decoration:none; }
.empty { color:#B0B0AC; font-size:13px; padding:40px 20px; }
.src { font-size:10px; color:#B0B0AC; }
.zoom-controls { position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:20; }
.zoom-controls button { width:42px; height:42px; border-radius:50%; border:1px solid #ECECE8; background:white; font-size:19px; box-shadow:0 2px 10px rgba(0,0,0,0.12); cursor:pointer; color:#14201A; }
.zoom-controls button:active { background:#F0F0EC; }
"""

LOGIN_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#E9E9E7; font-family:'Inter',-apple-system,sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; }
.box { background:#F7F7F4; border:1px solid #ECECE8; border-radius:16px; padding:28px; width:280px; }
.box h1 { font-size:15px; margin-bottom:16px; color:#14201A; }
.box input { width:100%; padding:10px 12px; border:1px solid #ECECE8; border-radius:10px; font-size:14px; margin-bottom:12px; }
.box button { width:100%; padding:10px; border:none; border-radius:10px; background:#2F5D46; color:white; font-weight:600; font-size:14px; cursor:pointer; }
.box .err { color:#B23A3A; font-size:12.5px; margin-bottom:10px; }
"""


async def dashboard_handler(request):
    date_str = request.query.get("date") or kyiv_today().strftime("%d.%m.%Y")
    try:
        target_date = datetime.strptime(date_str, "%d.%m.%Y").date()
    except ValueError:
        target_date = kyiv_today()
        date_str = target_date.strftime("%d.%m.%Y")

    prev_date = (target_date - timedelta(days=1)).strftime("%d.%m.%Y")
    next_date = (target_date + timedelta(days=1)).strftime("%d.%m.%Y")

    columns = build_driver_columns(target_date, date_str)

    cols_html = ""
    if not columns:
        cols_html = '<div class="empty">Поставок на цю дату не знайдено.</div>'
    else:
        lo, hi = compute_time_range(columns)
        overall_max_top = assign_card_positions(columns, lo)
        total_height = HEADER_OFFSET + max(overall_max_top + 160, (hi - lo) / 60 * PX_PER_HOUR + 40)

        cols_html += build_ruler_html(lo, hi, total_height)

        for col_idx, (phone, items) in enumerate(columns.items()):
            color = DRIVER_COLORS[col_idx % len(DRIVER_COLORS)]
            cards_html = ""

            for idx, item in enumerate(items):
                top = HEADER_OFFSET + item["top"]

                if idx < len(items) - 1:
                    next_top = HEADER_OFFSET + items[idx + 1]["top"]
                    line_top = top + CARD_CENTER_OFFSET
                    line_h = (next_top + CARD_CENTER_OFFSET) - line_top
                    cards_html += (
                        f'<div class="thread" style="top:{line_top}px; height:{line_h}px; '
                        f'background:{color};"></div>'
                    )

                assigned = item["assigned"]
                needed = item["needed"]
                chips = ""
                for w in assigned:
                    if w.get("telegram_id"):
                        url = f"tg://user?id={w['telegram_id']}"
                        chips += f'<a class="worker-chip" href="{url}">{html_module.escape(w["name"])}</a>'
                    elif w.get("username"):
                        url = f"https://t.me/{w['username']}"
                        chips += f'<a class="worker-chip" href="{url}">{html_module.escape(w["name"])}</a>'
                    else:
                        chips += f'<span class="worker-chip">{html_module.escape(w["name"])}</span>'

                badge = ""
                if needed:
                    count = len(assigned)
                    cls = "ok" if count >= needed else "need"
                    badge = f'<span class="badge {cls}">{count}/{needed}</span>'

                src_emoji = config.SOURCE_EMOJI.get(item["source"], "")
                cards_html += (
                    f'<div class="card" style="top:{top}px; border-left-color:{color};">'
                    f'{telegram_md_to_html(item["text"])}'
                    f'<div>{badge}{chips}</div>'
                    f'<div class="src">{src_emoji} {config.SOURCE_LABEL.get(item["source"], "")}</div></div>'
                )

            cols_html += (
                f'<div class="col" style="height:{total_height}px;">'
                f'<div class="col-head">📞 {html_module.escape(phone)}</div>{cards_html}</div>'
            )

    page = f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Маршрути водіїв</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{DASHBOARD_CSS}</style>
</head><body>
<div class="app">
<div class="header">
  <div class="header-title">Маршрути водіїв</div>
  <div class="nav">
    <a href="/?date={prev_date}">← {prev_date}</a>
    <span>{date_str}</span>
    <a href="/?date={next_date}">{next_date} →</a>
  </div>
</div>
<div class="board" id="board">
  <div class="board-spacer" id="boardSpacer">
    <div class="board-inner" id="boardInner">{cols_html}</div>
  </div>
</div>
<div class="zoom-controls">
  <button id="zoomIn" aria-label="Наблизити">+</button>
  <button id="zoomOut" aria-label="Віддалити">−</button>
  <button id="zoomFit" aria-label="Показати все">⤢</button>
</div>
</div>
<script>
(function() {{
  var board = document.getElementById('board');
  var spacer = document.getElementById('boardSpacer');
  var inner = document.getElementById('boardInner');

  var zoom = 1, minZoom = 0.3, maxZoom = 2.5;
  var naturalW = 0, naturalH = 0;

  function measure() {{
    inner.style.transform = 'scale(1)';
    naturalW = inner.scrollWidth;
    naturalH = inner.scrollHeight;
  }}

  function applyZoom(z, focalX, focalY) {{
    z = Math.min(maxZoom, Math.max(minZoom, z));
    var ratio = z / zoom;
    var beforeX = (focalX !== undefined) ? board.scrollLeft + focalX : 0;
    var beforeY = (focalY !== undefined) ? board.scrollTop + focalY : 0;
    zoom = z;
    spacer.style.width = (naturalW * zoom) + 'px';
    spacer.style.height = (naturalH * zoom) + 'px';
    inner.style.transform = 'scale(' + zoom + ')';
    if (focalX !== undefined) {{
      board.scrollLeft = beforeX * ratio - focalX;
      board.scrollTop = beforeY * ratio - focalY;
    }}
  }}

  function fitAll() {{
    var fitZoom = Math.min(board.clientWidth / naturalW, board.clientHeight / naturalH);
    minZoom = Math.min(fitZoom, 1);
    applyZoom(fitZoom);
    board.scrollLeft = 0;
    board.scrollTop = 0;
  }}

  function dist(t1, t2) {{
    return Math.hypot(t1.clientX - t2.clientX, t1.clientY - t2.clientY);
  }}

  window.addEventListener('load', function() {{
    measure();
    var fitZoom = Math.min(board.clientWidth / naturalW, board.clientHeight / naturalH);
    minZoom = Math.min(fitZoom, 1);
    applyZoom(1);
  }});

  var pinchStartDist = null, pinchStartZoom = 1;

  board.addEventListener('touchstart', function(e) {{
    if (e.touches.length === 2) {{
      e.preventDefault();
      pinchStartDist = dist(e.touches[0], e.touches[1]);
      pinchStartZoom = zoom;
    }}
  }}, {{passive:false}});

  board.addEventListener('touchmove', function(e) {{
    if (e.touches.length === 2 && pinchStartDist) {{
      e.preventDefault();
      var d = dist(e.touches[0], e.touches[1]);
      var factor = d / pinchStartDist;
      var rect = board.getBoundingClientRect();
      var midX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
      var midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
      applyZoom(pinchStartZoom * factor, midX, midY);
    }}
  }}, {{passive:false}});

  board.addEventListener('touchend', function(e) {{
    if (e.touches.length < 2) pinchStartDist = null;
  }});

  board.addEventListener('wheel', function(e) {{
    if (e.ctrlKey) {{
      e.preventDefault();
      var rect = board.getBoundingClientRect();
      applyZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9), e.clientX - rect.left, e.clientY - rect.top);
    }}
  }}, {{passive:false}});

  document.getElementById('zoomIn').onclick = function() {{
    applyZoom(zoom * 1.3, board.clientWidth / 2, board.clientHeight / 2);
  }};
  document.getElementById('zoomOut').onclick = function() {{
    applyZoom(zoom / 1.3, board.clientWidth / 2, board.clientHeight / 2);
  }};
  document.getElementById('zoomFit').onclick = fitAll;
}})();
</script>
</body></html>"""

    return web.Response(text=page, content_type="text/html")


SESSION_SECRET = os.environ.get("SESSION_SECRET", config.BOT_TOKEN)
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 днів


def make_session_token() -> str:
    expiry = str(int(time.time()) + SESSION_MAX_AGE)
    sig = hmac.new(SESSION_SECRET.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def verify_session_token(token: str) -> bool:
    try:
        expiry, sig = token.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), expiry.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        return int(expiry) > int(time.time())
    except Exception:
        return False


def render_login_page(error: str = "") -> str:
    err_html = f'<div class="err">{html_module.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Вхід</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{LOGIN_CSS}</style>
</head><body>
<form class="box" method="post" action="/login">
  <h1>Маршрути водіїв</h1>
  {err_html}
  <input type="password" name="password" placeholder="Пароль" autofocus>
  <button type="submit">Увійти</button>
</form>
</body></html>"""


async def login_handler(request):
    data = await request.post()
    pwd = data.get("password", "")
    if pwd == config.DASHBOARD_PASSWORD:
        resp = web.HTTPFound("/")
        resp.set_cookie(
            "dashboard_session", make_session_token(),
            max_age=SESSION_MAX_AGE, httponly=True, samesite="Lax"
        )
        return resp
    return web.Response(text=render_login_page("Невірний пароль"), content_type="text/html")


@web.middleware
async def auth_middleware(request, handler):
    if not config.DASHBOARD_PASSWORD:
        return web.Response(text="Дашборд не налаштовано: відсутня змінна config.py.DASHBOARD_PASSWORD.", status=503)

    if request.path == "/login":
        return await handler(request)

    token = request.cookies.get("dashboard_session", "")
    if verify_session_token(token):
        return await handler(request)

    return web.Response(text=render_login_page(), content_type="text/html")


def build_web_app() -> web.Application:
    webapp = web.Application(middlewares=[auth_middleware])
    webapp.router.add_get("/", dashboard_handler)
    webapp.router.add_post("/login", login_handler)
    return webapp
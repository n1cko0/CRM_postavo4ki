import hashlib
import hmac
import html as html_module
import os
import re
import time
from datetime import datetime, timedelta

from aiohttp import web

import config
import db
import parsing


# ==================== ВЕБ-ДАШБОРД МАРШРУТІВ ====================
def extract_time_minutes(text: str) -> int:
    m = re.search(r'🕐\s*(\d{1,2}):(\d{2})', text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 99999  # без часу — в кінець списку


def telegram_md_to_html(text: str) -> str:
    escaped = html_module.escape(text)
    escaped = re.sub(r'\*(.+?)\*', r'<b>\1</b>', escaped)
    return escaped.replace("\n", "<br>")


DRIVER_COLORS = ["#2F5D46", "#3D5A73", "#8A5E74", "#8A6F2F", "#5E5A8A", "#2F7A6B", "#7A4A4A"]


def build_driver_columns(date_str: str) -> dict:
    """Групує поставки на дату по водію (за телефоном) і підвантажує
    реальні призначення робітників з бази — те, що бачить бот."""
    deliveries = db.db_get_deliveries(delivery_date=date_str)

    columns = {}
    for d in deliveries:
        text = parsing.format_delivery_card(d)
        phone = d.get("driver_phone") or "Без номера водія"
        assigned = db.db_get_assigned_workers(d["id"])
        columns.setdefault(phone, []).append({
            "delivery_id": d["id"],
            "text": text,
            "source": d["source"],
            "assigned": assigned,
            "needed": d.get("workers_needed"),
            "sort_key": extract_time_minutes(text),
        })

    for phone in columns:
        columns[phone].sort(key=lambda x: x["sort_key"])

    return columns


def build_flat_list(date_str: str) -> list:
    """Всі поставки дня в одному хронологічному списку (без групування по водію) —
    для загальної картини дня цілком."""
    deliveries = db.db_get_deliveries(delivery_date=date_str)
    items = []
    for d in deliveries:
        text = parsing.format_delivery_card(d)
        assigned = db.db_get_assigned_workers(d["id"])
        items.append({
            "delivery_id": d["id"],
            "text": text,
            "source": d["source"],
            "phone": d.get("driver_phone") or "Без номера водія",
            "assigned": assigned,
            "needed": d.get("workers_needed"),
            "sort_key": extract_time_minutes(text),
        })
    items.sort(key=lambda x: x["sort_key"])
    return items


def render_card_inner(item: dict, color: str = None) -> str:
    """Спільна розмітка вмісту картки (бейдж призначення + чіпи людей) —
    використовується і в колонках, і в хронологічному списку."""
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
    style = f' style="border-left-color:{color};"' if color else ""
    return (
        f'<div class="card"{style}>{telegram_md_to_html(item["text"])}'
        f'<div>{badge}{chips}</div>'
        f'<div class="src">{src_emoji} {config.SOURCE_LABEL.get(item["source"], "")}</div></div>'
    )


DASHBOARD_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
html, body { height:100%; background:#E9E9E7; font-family:'Inter',-apple-system,sans-serif; color:#14201A; }
.app { display:flex; flex-direction:column; height:100vh; }
.header { padding:16px 20px; flex-shrink:0; }
.header-title { font-size:16px; font-weight:700; }
.nav { display:flex; align-items:center; gap:12px; margin-top:6px; flex-wrap:wrap; }
.nav a { text-decoration:none; color:#2F5D46; font-weight:600; font-size:14px; padding:4px 10px; border-radius:8px; background:#F7F7F4; border:1px solid #ECECE8; }
.nav span { font-size:13px; color:#6B6B68; }
.view-switch { display:flex; gap:6px; padding:0 20px 12px; flex-shrink:0; }
.view-switch a { text-decoration:none; font-size:13px; font-weight:600; padding:6px 14px; border-radius:20px; border:1px solid #ECECE8; color:#6B6B68; background:#F7F7F4; }
.view-switch a.active { background:#14201A; color:white; border-color:#14201A; }

/* ==== вид "колонки по водіям" ==== */
.board { flex:1; overflow:auto; -webkit-overflow-scrolling:touch; touch-action:pan-x pan-y; position:relative; background:#E9E9E7; }
.board-spacer { position:relative; }
.board-inner {
  position:absolute; top:0; left:0; display:flex; align-items:flex-start; gap:14px; padding:4px 20px 40px;
  transform-origin:0 0; will-change:transform;
  background-image: radial-gradient(circle, #D6D6D0 1.6px, transparent 1.6px);
  background-size: 22px 22px;
  background-position: 6px 6px;
}
.col { flex:0 0 250px; }
.col-head { font-size:13px; font-weight:700; padding:8px 6px; color:#14201A; }
.zoom-controls { position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:20; }
.zoom-controls button { width:42px; height:42px; border-radius:50%; border:1px solid #ECECE8; background:white; font-size:19px; box-shadow:0 2px 10px rgba(0,0,0,0.12); cursor:pointer; color:#14201A; }
.zoom-controls button:active { background:#F0F0EC; }

/* ==== вид "хронологічно" (звичайний список, без зуму) ==== */
.list-wrap { flex:1; overflow-y:auto; padding:4px 20px 60px; -webkit-overflow-scrolling:touch; }
.list-item { margin-bottom:12px; }
.list-time { font-size:11px; font-weight:700; color:#8A8A86; margin-bottom:4px; padding-left:2px; }
.driver-tag { display:inline-block; font-size:10.5px; font-weight:600; color:white; padding:2px 8px; border-radius:20px; margin-bottom:6px; }

/* ==== картка (спільна для обох видів) ==== */
.card {
  background:#F7F7F4; border:1px solid #ECECE8; border-radius:12px; border-left-width:3px;
  padding:10px 12px; font-size:12.5px; line-height:1.5; margin-bottom:10px;
  box-shadow:0 1px 2px rgba(0,0,0,0.03), 0 4px 10px rgba(0,0,0,0.04);
}
.badge { display:inline-block; font-size:10.5px; font-weight:600; padding:2px 8px; border-radius:20px; margin-top:6px; margin-right:4px; }
.badge.ok { background:#DCE8E0; color:#2F5D46; }
.badge.need { background:#F5E3D8; color:#8A5E2F; }
.worker-chip { display:inline-block; font-size:11px; background:#EFEFEA; color:#3D5A46; padding:2px 8px; border-radius:20px; margin:2px 4px 0 0; text-decoration:none; }
.empty { color:#B0B0AC; font-size:13px; padding:40px 20px; }
.src { font-size:10px; color:#B0B0AC; }
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


def render_columns_view(date_str: str) -> str:
    columns = build_driver_columns(date_str)
    if not columns:
        return '<div class="empty">Поставок на цю дату не знайдено.</div>'

    cols_html = ""
    for col_idx, (phone, items) in enumerate(columns.items()):
        color = DRIVER_COLORS[col_idx % len(DRIVER_COLORS)]
        cards_html = "".join(render_card_inner(it, color) for it in items)
        cols_html += (
            f'<div class="col"><div class="col-head">📞 {html_module.escape(phone)}</div>{cards_html}</div>'
        )

    return f"""<div class="board" id="board">
  <div class="board-spacer" id="boardSpacer">
    <div class="board-inner" id="boardInner">{cols_html}</div>
  </div>
</div>
<div class="zoom-controls">
  <button id="zoomIn" aria-label="Наблизити">+</button>
  <button id="zoomOut" aria-label="Віддалити">−</button>
  <button id="zoomFit" aria-label="Показати все">⤢</button>
</div>"""


def render_list_view(date_str: str) -> str:
    items = build_flat_list(date_str)
    if not items:
        return '<div class="empty">Поставок на цю дату не знайдено.</div>'

    phone_colors = {}
    rows_html = ""
    for item in items:
        phone = item["phone"]
        if phone not in phone_colors:
            phone_colors[phone] = DRIVER_COLORS[len(phone_colors) % len(DRIVER_COLORS)]
        color = phone_colors[phone]
        time_label = item["text"]
        m = re.search(r'🕐\s*(\d{1,2}:\d{2})', item["text"])
        time_label = m.group(1) if m else "без часу"

        rows_html += (
            f'<div class="list-item">'
            f'<div class="list-time">{time_label}</div>'
            f'<span class="driver-tag" style="background:{color};">📞 {html_module.escape(phone)}</span>'
            f'{render_card_inner(item, color)}'
            f'</div>'
        )

    return f'<div class="list-wrap">{rows_html}</div>'


ZOOM_SCRIPT = """
(function() {
  var board = document.getElementById('board');
  if (!board) return;
  var spacer = document.getElementById('boardSpacer');
  var inner = document.getElementById('boardInner');

  var zoom = 1, minZoom = 0.3, maxZoom = 2.5;
  var naturalW = 0, naturalH = 0;

  function measure() {
    inner.style.transform = 'scale(1)';
    naturalW = inner.scrollWidth;
    naturalH = inner.scrollHeight;
  }

  function applyZoom(z, focalX, focalY) {
    z = Math.min(maxZoom, Math.max(minZoom, z));
    var ratio = z / zoom;
    var beforeX = (focalX !== undefined) ? board.scrollLeft + focalX : 0;
    var beforeY = (focalY !== undefined) ? board.scrollTop + focalY : 0;
    zoom = z;
    spacer.style.width = (naturalW * zoom) + 'px';
    spacer.style.height = (naturalH * zoom) + 'px';
    inner.style.transform = 'scale(' + zoom + ')';
    if (focalX !== undefined) {
      board.scrollLeft = beforeX * ratio - focalX;
      board.scrollTop = beforeY * ratio - focalY;
    }
  }

  function fitAll() {
    var fitZoom = Math.min(board.clientWidth / naturalW, board.clientHeight / naturalH);
    minZoom = Math.min(fitZoom, 1);
    applyZoom(fitZoom);
    board.scrollLeft = 0;
    board.scrollTop = 0;
  }

  function dist(t1, t2) {
    return Math.hypot(t1.clientX - t2.clientX, t1.clientY - t2.clientY);
  }

  window.addEventListener('load', function() {
    measure();
    var fitZoom = Math.min(board.clientWidth / naturalW, board.clientHeight / naturalH);
    minZoom = Math.min(fitZoom, 1);
    applyZoom(1);
  });

  var pinchStartDist = null, pinchStartZoom = 1;

  board.addEventListener('touchstart', function(e) {
    if (e.touches.length === 2) {
      e.preventDefault();
      pinchStartDist = dist(e.touches[0], e.touches[1]);
      pinchStartZoom = zoom;
    }
  }, {passive:false});

  board.addEventListener('touchmove', function(e) {
    if (e.touches.length === 2 && pinchStartDist) {
      e.preventDefault();
      var d = dist(e.touches[0], e.touches[1]);
      var factor = d / pinchStartDist;
      var rect = board.getBoundingClientRect();
      var midX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
      var midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
      applyZoom(pinchStartZoom * factor, midX, midY);
    }
  }, {passive:false});

  board.addEventListener('touchend', function(e) {
    if (e.touches.length < 2) pinchStartDist = null;
  });

  board.addEventListener('wheel', function(e) {
    if (e.ctrlKey) {
      e.preventDefault();
      var rect = board.getBoundingClientRect();
      applyZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9), e.clientX - rect.left, e.clientY - rect.top);
    }
  }, {passive:false});

  var zoomInBtn = document.getElementById('zoomIn');
  var zoomOutBtn = document.getElementById('zoomOut');
  var zoomFitBtn = document.getElementById('zoomFit');
  if (zoomInBtn) zoomInBtn.onclick = function() { applyZoom(zoom * 1.3, board.clientWidth / 2, board.clientHeight / 2); };
  if (zoomOutBtn) zoomOutBtn.onclick = function() { applyZoom(zoom / 1.3, board.clientWidth / 2, board.clientHeight / 2); };
  if (zoomFitBtn) zoomFitBtn.onclick = fitAll;
})();
"""


async def dashboard_handler(request):
    date_str = request.query.get("date") or config.kyiv_today().strftime("%d.%m.%Y")
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        target_date = datetime.strptime(date_str, "%d.%m.%Y").date()
    except ValueError:
        target_date = config.kyiv_today()
        date_str = target_date.strftime("%d.%m.%Y")

    view = request.query.get("view", "columns")
    if view not in ("columns", "list"):
        view = "columns"

    prev_date = (target_date - timedelta(days=1)).strftime("%d.%m.%Y")
    next_date = (target_date + timedelta(days=1)).strftime("%d.%m.%Y")

    if view == "list":
        body_html = render_list_view(date_str)
        script = ""
    else:
        body_html = render_columns_view(date_str)
        script = f"<script>{ZOOM_SCRIPT}</script>"

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
    <a href="/?date={prev_date}&view={view}">← {prev_date}</a>
    <span>{date_str}</span>
    <a href="/?date={next_date}&view={view}">{next_date} →</a>
  </div>
</div>
<div class="view-switch">
  <a href="/?date={date_str}&view=columns" class="{'active' if view == 'columns' else ''}">🚚 По водіям</a>
  <a href="/?date={date_str}&view=list" class="{'active' if view == 'list' else ''}">🕐 Хронологічно</a>
</div>
{body_html}
</div>
{script}
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
        return web.Response(text="Дашборд не налаштовано: відсутня змінна config.DASHBOARD_PASSWORD.", status=503)

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
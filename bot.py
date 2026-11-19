"""
Portals NFT Flipper — уведомления о выгодных лотах на Portals (@portals).
Рассчитан на запуск в GitHub Actions (см. .github/workflows/flipper.yml).

Бот НИЧЕГО не покупает сам. Для каждого нового лота он:
  • отбрасывает всё, что дороже вашего баланса (с комиссией покупателя);
  • смотрит модель, фон, символ, номер и «монохромность»;
  • оценивает справедливую цену по ДВУМ источникам:
      – текущие лоты той же модели и той же модели + фона;
      – недавние реальные продажи той же модели и модели + фона;
  • считает чистую прибыль с учётом комиссий покупателя и продавца;
  • присылает уведомление, если скидка >= порога и прибыль > 0.
"""

import asyncio
import json
import logging
import math
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import base64
import hashlib
import io

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message
from cryptography.fernet import Fernet, InvalidToken
from curl_cffi.requests import AsyncSession
from telethon import TelegramClient, functions, types, utils
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError
from telethon.sessions import StringSession

# ───────────────────────── Настройки (из GitHub Secrets / Variables) ─────────────────────────


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
API_ID = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "")

POLL_SECONDS = int(env_float("POLL_SECONDS", 15))
RUN_MINUTES = env_float("RUN_MINUTES", 0)          # 0 = работать бесконечно (локальный запуск)
BUYER_FEE = env_float("BUYER_FEE", 0.05)           # комиссия покупателя Portals
SELLER_FEE = env_float("SELLER_FEE", 0.05)         # комиссия продавца при перепродаже
UNDERCUT = env_float("UNDERCUT", 0.01)             # на сколько дешевле рынка выставлять при перепродаже
SALES_DAYS = env_float("SALES_DAYS", 30)           # учитывать продажи не старше N дней

STATE_DIR = Path(os.getenv("STATE_DIR") or Path(__file__).resolve().parent / "state")
STATE_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = STATE_DIR / "settings.json"
SEEN_FILE = STATE_DIR / "seen.json"

DEFAULT_SETTINGS = {
    "balance": env_float("BALANCE", 0),
    "discount": env_float("DISCOUNT", 15),
    "min_profit": env_float("MIN_PROFIT", 0.1),    # минимальная чистая прибыль в TON
    "running": True,
}

API_URL = "https://portal-market.com/api/"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Origin": "https://portal-market.com",
    "Referer": "https://portal-market.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flipper")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
seen_order: list[str] = load_json(SEEN_FILE, [])[-5000:]
seen_ids: set[str] = set(seen_order)


def save_state() -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    SEEN_FILE.write_text(json.dumps(seen_order[-5000:]), encoding="utf-8")


def remember(lot_id: str) -> None:
    seen_ids.add(lot_id)
    seen_order.append(lot_id)
    if len(seen_order) > 6000:
        for old in seen_order[:1000]:
            seen_ids.discard(old)
        del seen_order[:1000]

# ───────────────────────── Авторизация в Portals ─────────────────────────


# Сессия Telegram хранится в кэше GitHub в ЗАШИФРОВАННОМ виде (ключ — из ваших секретов).
SESSION_FILE = STATE_DIR / "session.enc"
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"{BOT_TOKEN}|{API_HASH}".encode()).digest()))

tg: TelegramClient | None = None
tg_ready = asyncio.Event()
password_waiter: asyncio.Future | None = None
login_task: asyncio.Task | None = None


def load_session() -> str:
    if TG_SESSION:
        return TG_SESSION
    try:
        return _fernet.decrypt(SESSION_FILE.read_bytes()).decode()
    except (FileNotFoundError, InvalidToken):
        return ""


def store_session(s: str) -> None:
    SESSION_FILE.write_bytes(_fernet.encrypt(s.encode()))


def qr_png(url: str) -> BufferedInputFile:
    buf = io.BytesIO()
    qrcode.make(url, box_size=10, border=3).save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="login.png")


QR_TEXT = (
    "📲 <b>Вход в Telegram для доступа к Portals</b>\n\n"
    "Отсканируйте этот QR <b>телефоном</b>:\n"
    "Telegram → Настройки → Устройства → <b>Подключить устройство</b>\n\n"
    "QR должен быть на другом экране: откройте этот чат на компьютере "
    "(или на web.telegram.org) либо покажите скриншот на другом устройстве.\n"
    "Код обновляется каждые 30 секунд."
)


async def qr_login(bot: Bot) -> None:
    """Вход по QR-коду через бота: без номера телефона и кодов."""
    global tg, password_waiter
    client = TelegramClient(StringSession(), int(API_ID), API_HASH)
    await client.connect()
    qr = await client.qr_login()
    msg = await bot.send_photo(OWNER_ID, qr_png(qr.url), caption=QR_TEXT)
    deadline = time.time() + 10 * 60
    try:
        while True:
            try:
                await qr.wait(timeout=25)
                break
            except asyncio.TimeoutError:
                if time.time() > deadline:
                    await bot.send_message(OWNER_ID, "⌛ QR не отсканирован. Отправьте /login, чтобы получить новый.")
                    await client.disconnect()
                    return
                await qr.recreate()
                await bot.edit_message_media(
                    media=InputMediaPhoto(media=qr_png(qr.url), caption=QR_TEXT),
                    chat_id=OWNER_ID, message_id=msg.message_id)
            except SessionPasswordNeededError:
                while True:
                    password_waiter = asyncio.get_running_loop().create_future()
                    await bot.send_message(OWNER_ID, "🔐 У аккаунта включена двухэтапная проверка.\n"
                                                     "Отправьте сюда облачный пароль. Сообщение с ним я сразу удалю.")
                    password = await password_waiter
                    try:
                        await client.sign_in(password=password)
                        break
                    except PasswordHashInvalidError:
                        await bot.send_message(OWNER_ID, "❌ Неверный пароль, попробуйте ещё раз.")
                break
    finally:
        password_waiter = None
        try:
            await bot.delete_message(OWNER_ID, msg.message_id)
        except Exception:
            pass

    me = await client.get_me()
    store_session(client.session.save())
    save_state()
    tg = client
    tg_ready.set()
    await bot.send_message(OWNER_ID, f"✅ Вход выполнен: {me.first_name}. Начинаю искать лоты.")


async def ensure_telegram(bot: Bot) -> None:
    """Подключает сохранённую сессию или запускает вход по QR."""
    global tg, login_task
    session = load_session()
    if session:
        client = TelegramClient(StringSession(session), int(API_ID), API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            tg = client
            tg_ready.set()
            return
        await client.disconnect()
        await bot.send_message(OWNER_ID, "⚠️ Сохранённый вход больше не действует, нужен новый.")
    login_task = asyncio.create_task(qr_login(bot))


async def fetch_tma_token() -> str:
    """Открывает мини-приложение Portals от имени вашего аккаунта и берёт initData."""
    await tg_ready.wait()
    if not tg.is_connected():
        await tg.connect()
    bot_user = utils.get_input_user(await tg.get_input_entity("portals"))
    web_view = await tg(functions.messages.RequestAppWebViewRequest(
        peer=bot_user,
        app=types.InputBotAppShortName(bot_id=bot_user, short_name="market"),
        platform="desktop",
    ))
    init_data = unquote(web_view.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])
    return f"tma {init_data}"


class PortalsAuth:
    TTL = 30 * 60

    def __init__(self):
        self.token, self.created = "", 0.0
        self.lock = asyncio.Lock()

    async def get(self, force: bool = False) -> str:
        async with self.lock:
            if force or not self.token or time.time() - self.created > self.TTL:
                log.info("Обновляю токен Portals…")
                self.token = await fetch_tma_token()
                self.created = time.time()
            return self.token


auth = PortalsAuth()
http: AsyncSession | None = None


async def api_get(path: str, params: dict) -> dict:
    refreshed = False
    for attempt in range(3):
        token = await auth.get()
        r = await http.get(API_URL + path, params=params,
                           headers={**HEADERS, "Authorization": token},
                           impersonate="chrome110", timeout=15)
        if r.status_code in (401, 403):
            if refreshed:
                raise RuntimeError(f"Portals {path}: доступ запрещён (HTTP {r.status_code}): {r.text[:200]}")
            await auth.get(force=True)
            refreshed = True
            continue
        if r.status_code == 429:
            await asyncio.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Portals {path}: HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"Portals {path}: нет ответа после 3 попыток")


def _filters(params: dict, collection="", collection_id="", model="", backdrop="", max_price=None) -> dict:
    if max_price is not None:
        params["min_price"] = 0
        params["max_price"] = max(1, math.ceil(max_price))
    if collection_id:                       # поиск лотов фильтрует коллекцию по ID
        params["collection_ids"] = collection_id
    elif collection:                        # история сделок — по названию
        params["filter_by_collections"] = collection
    if model:
        params["filter_by_models"] = model
    if backdrop:
        params["filter_by_backdrops"] = backdrop
    return params


async def search_listings(sort: str, limit=20, offset=0, **flt) -> list[dict]:
    params = _filters({"offset": offset, "limit": limit, "sort_by": sort, "status": "listed"}, **flt)
    return (await api_get("nfts/search", params)).get("results") or []


async def search_sales(limit=20, offset=0, **flt) -> list[dict]:
    # Portals: limit не больше 20, коллекция — по collection_ids
    params = _filters({"offset": offset, "limit": min(limit, 20), "sort_by": "created_at desc",
                       "action_types": "buy"}, **flt)
    return (await api_get("market/actions/", params)).get("actions") or []

# ───────────────────────── Атрибуты подарка ─────────────────────────


def attr(nft: dict, kind: str) -> tuple[str, float | None]:
    for a in nft.get("attributes") or []:
        if a.get("type") == kind:
            r = a.get("rarity_per_mille")
            return a.get("value") or "?", (float(r) / 10 if r is not None else None)
    return "?", None


def gift_number(nft: dict) -> int | None:
    try:
        return int(nft.get("external_collection_number") or nft.get("tg_id"))
    except (TypeError, ValueError):
        return None


def number_tag(n: int | None) -> str:
    """Красивый номер: маленький, повторяющиеся цифры, зеркальный, круглый, лесенка."""
    if not n:
        return ""
    s = str(n)
    tags = []
    if n <= 100:
        tags.append("топ-100")
    elif n <= 1000:
        tags.append("до 1000")
    if len(s) >= 2 and len(set(s)) == 1:
        tags.append("одинаковые цифры")
    elif len(s) >= 3 and s == s[::-1]:
        tags.append("зеркальный")
    if len(s) >= 3 and re.fullmatch(r"[1-9]0+", s):
        tags.append("круглый")
    if len(s) >= 3 and (s in "1234567890" or s in "9876543210"):
        tags.append("лесенка")
    return ", ".join(tags)


# Цветовые семейства для эвристики «монохрома»: модель и фон одного цвета.
COLOR_FAMILIES = {
    "чёрный": ["black", "onyx", "obsidian", "raven", "midnight", "shadow", "dark", "noir", "ink", "coal", "night"],
    "белый": ["white", "ivory", "pearl", "snow", "milk", "frost", "cream", "light"],
    "серый": ["gray", "grey", "silver", "steel", "ash", "smoke", "platinum", "stone", "graphite"],
    "красный": ["red", "ruby", "crimson", "scarlet", "cherry", "carmine", "blood", "wine", "burgundy", "fire"],
    "розовый": ["pink", "rose", "magenta", "fuchsia", "flamingo", "blush", "strawberry"],
    "оранжевый": ["orange", "tangerine", "coral", "copper", "pumpkin", "carrot", "peach", "apricot"],
    "жёлтый": ["yellow", "gold", "golden", "amber", "lemon", "honey", "banana", "sun", "mustard"],
    "зелёный": ["green", "emerald", "mint", "jade", "lime", "malachite", "olive", "forest", "toxic", "pine", "moss"],
    "голубой": ["cyan", "turquoise", "aqua", "sky", "azure", "teal", "ice", "celeste"],
    "синий": ["blue", "sapphire", "navy", "cobalt", "ocean", "indigo", "denim", "ultramarine"],
    "фиолетовый": ["purple", "violet", "amethyst", "lavender", "lilac", "plum", "grape", "orchid", "mystic"],
    "коричневый": ["brown", "chocolate", "coffee", "caramel", "bronze", "mocha", "wood", "cocoa", "rust"],
}


def color_family(name: str) -> str | None:
    words = re.findall(r"[a-z]+", name.lower())
    for family, keys in COLOR_FAMILIES.items():
        if any(w in keys for w in words):
            return family
    return None


def monochrome(model: str, backdrop: str) -> str | None:
    fm, fb = color_family(model), color_family(backdrop)
    return fm if fm and fm == fb else None


def nft_link(nft: dict) -> str:
    name = (nft.get("name") or "").replace("'", "").replace("’", "")
    short = "".join(p[:1].upper() + p[1:] for p in re.split(r"[^0-9A-Za-z]+", name) if p)
    return f"https://t.me/nft/{short}-{gift_number(nft)}"

# ───────────────────────── Оценка цены ─────────────────────────

_cache: dict[tuple, tuple[float, list]] = {}
CACHE_TTL = {"listings": 90, "sales": 600}


async def cached(kind: str, collection: str, collection_id: str, model: str) -> list[dict]:
    key = (kind, collection, model)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL[kind]:
        return hit[1]
    if kind == "listings":
        data = await search_listings("price asc", limit=20, collection_id=collection_id,
                                     collection="" if collection_id else collection, model=model)
    else:
        data = []
        try:
            for page in range(2):  # до 40 последних продаж
                part = await search_sales(offset=page * 20, collection_id=collection_id,
                                          collection="" if collection_id else collection, model=model)
                data += part
                if len(part) < 20:
                    break
        except Exception as e:
            log.warning("История продаж недоступна: %s", e)
    _cache[key] = (time.time(), data)
    return data


def parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def median_or_none(values: list[float], min_n: int) -> float | None:
    return statistics.median(values) if len(values) >= min_n else None


async def estimate(lot: dict) -> dict | None:
    name = lot.get("name") or ""
    model, _ = attr(lot, "model")
    backdrop, _ = attr(lot, "backdrop")
    if model == "?":
        return None

    listings = [x for x in await cached("listings", name, lot.get("collection_id") or "", model) if x.get("id") != lot.get("id")]
    sales_raw = await cached("sales", name, lot.get("collection_id") or "", model)

    def price(x, field="price"):
        try:
            return float(x.get(field) or 0)
        except ValueError:
            return 0.0

    ask_m = [price(x) for x in listings if price(x) > 0]
    ask_mb = [price(x) for x in listings if price(x) > 0 and attr(x, "backdrop")[0] == backdrop]

    now = datetime.now(timezone.utc)
    sales_m, sales_mb, last_sale = [], [], None
    for s in sales_raw:
        amount = price(s, "amount")
        ts = parse_time(s.get("created_at"))
        if amount <= 0 or (ts and (now - ts).days > SALES_DAYS):
            continue
        sales_m.append(amount)
        if attr(s.get("nft") or {}, "backdrop")[0] == backdrop:
            sales_mb.append(amount)
        if ts and (last_sale is None or ts > last_sale):
            last_sale = ts

    floor_m = min(ask_m) if ask_m else None
    floor_mb = min(ask_mb) if ask_mb else None
    med_m = median_or_none(sales_m, 3)
    med_mb = median_or_none(sales_mb, 2)

    # Консервативная оценка: берём МЕНЬШЕЕ из «за сколько выставлено» и «за сколько реально продают».
    ask = floor_mb or floor_m
    sold = med_mb or med_m
    candidates = [v for v in (ask, sold) if v]
    if not candidates:
        return None
    fair = min(candidates)

    return {
        "fair": fair,
        "floor_m": floor_m, "n_ask_m": len(ask_m),
        "floor_mb": floor_mb, "n_ask_mb": len(ask_mb),
        "med_m": med_m, "n_sales_m": len(sales_m),
        "med_mb": med_mb, "n_sales_mb": len(sales_mb),
        "last_sale": last_sale,
        "only_listings": sold is None,
    }

# ───────────────────────── Проверка лотов ─────────────────────────


async def check_lot(bot: Bot, lot: dict) -> bool:
    try:
        price = float(lot.get("price") or 0)
    except ValueError:
        return False
    cost = price * (1 + BUYER_FEE)
    if price <= 0 or cost > settings["balance"]:
        return False

    est = await estimate(lot)
    if not est:
        return False
    fair = est["fair"]
    discount = (1 - price / fair) * 100
    sell_price = fair * (1 - UNDERCUT)
    net = sell_price * (1 - SELLER_FEE) - cost
    if discount < settings["discount"] or net < settings["min_profit"]:
        return False

    await bot.send_message(OWNER_ID, format_lot(lot, price, cost, discount, sell_price, net, est),
                           disable_web_page_preview=False)
    return True


def fmt(v: float | None) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if v is not None else "—"


def format_lot(lot, price, cost, discount, sell_price, net, est) -> str:
    model, m_r = attr(lot, "model")
    backdrop, b_r = attr(lot, "backdrop")
    symbol, s_r = attr(lot, "symbol")
    pct = lambda r: f" ({r:g}%)" if r is not None else ""
    num = gift_number(lot)
    num_tag = number_tag(num)
    mono = monochrome(model, backdrop)

    lines = [f"🔥 <b>−{discount:.0f}%</b> · <b>{lot.get('name')} #{num}</b>",
             f"🧸 Модель: {model}{pct(m_r)}",
             f"🎨 Фон: {backdrop}{pct(b_r)}",
             f"✳️ Символ: {symbol}{pct(s_r)}"]
    if mono:
        lines.append(f"⚫ Похоже на монохром ({mono}: модель и фон одного цвета)")
    if num_tag:
        lines.append(f"🔢 Номер #{num}: {num_tag}")

    lines += ["",
              f"💰 Цена: <b>{fmt(price)} TON</b> → с комиссией покупателя {fmt(cost)}",
              f"📊 Оценка рынка: <b>{fmt(est['fair'])} TON</b>",
              f"  • мин. лот модель+фон: {fmt(est['floor_mb'])} ({est['n_ask_mb']} шт.)",
              f"  • мин. лот модели: {fmt(est['floor_m'])} ({est['n_ask_m']} шт.)",
              f"  • продажи модель+фон: медиана {fmt(est['med_mb'])} (n={est['n_sales_mb']})",
              f"  • продажи модели: медиана {fmt(est['med_m'])} (n={est['n_sales_m']})"]
    if est["last_sale"]:
        days = (datetime.now(timezone.utc) - est["last_sale"]).days
        lines.append(f"  • последняя продажа: {'сегодня' if days == 0 else f'{days} дн. назад'}")
    if est["only_listings"]:
        lines.append("⚠️ Мало продаж — оценка только по выставленным лотам, ликвидность под вопросом")

    roi = net / cost * 100
    lines += ["",
              f"💸 Если выставить за {fmt(sell_price)}: чистыми <b>{net:+.2f} TON ({roi:+.0f}%)</b>",
              f"   (комиссия продавца {SELLER_FEE * 100:g}%, покупателя {BUYER_FEE * 100:g}%)"]
    if mono or num_tag:
        lines.append("✨ Монохром/номер в оценку не заложены — это возможный бонус сверху")
    if lot.get("unlocks_at"):
        lines.append(f"🔒 Заблокирован до: {lot['unlocks_at']}")
    lines += ["", f"🎁 {nft_link(lot)}", "🛒 Купить: https://t.me/portals"]
    return "\n".join(lines)


scan_lock = asyncio.Lock()


async def scan(bot: Bot, pages: int = 1, recheck: bool = False) -> dict:
    """recheck=True — проверить и уже просмотренные лоты (для /scan)."""
    stats = {"lots": 0, "checked": 0, "found": 0, "errors": 0, "first_error": ""}
    max_price = settings["balance"] / (1 + BUYER_FEE)
    async with scan_lock:
        for page in range(pages):
            lots = await search_listings("listed_at desc", limit=20, offset=page * 20, max_price=max_price)
            stats["lots"] += len(lots)
            for lot in lots:
                lot_id = lot.get("id")
                if not lot_id or (lot_id in seen_ids and not recheck):
                    continue
                remember(lot_id)
                stats["checked"] += 1
                try:
                    stats["found"] += await check_lot(bot, lot)
                except Exception as e:
                    stats["errors"] += 1
                    stats["first_error"] = stats["first_error"] or str(e)[:300]
                    log.warning("Лот %s: %s", lot_id, e)
                    if stats["errors"] >= 3 and stats["checked"] == stats["errors"]:
                        raise RuntimeError(f"Все проверки падают: {stats['first_error']}")
            if len(lots) < 20:
                break
        save_state()
    return stats


async def scanner_loop(bot: Bot) -> None:
    errors = 0
    while True:
        await tg_ready.wait()
        if settings["running"] and settings["balance"] > 0:
            try:
                await scan(bot)
                errors = 0
            except Exception as e:
                errors += 1
                log.error("Ошибка сканирования: %s", e)
                if errors == 5:
                    await bot.send_message(OWNER_ID, f"⚠️ Сканер падает 5 раз подряд:\n<code>{str(e)[:300]}</code>")
                await asyncio.sleep(min(60, 5 * errors))
        await asyncio.sleep(POLL_SECONDS)

# ───────────────────────── Команды бота ─────────────────────────

dp = Dispatcher()
owner = F.from_user.id == OWNER_ID

HELP = (
    "🤖 <b>Portals Flipper</b>\n\n"
    "Отправьте число — это ваш баланс в TON.\n\n"
    "/balance 50 — баланс\n"
    "/discount 15 — мин. скидка от оценки рынка, %\n"
    "/profit 0.5 — мин. чистая прибыль, TON\n"
    "/scan — проверить последние ~100 лотов\n"
    "/debug — проверить связь с Portals\n"
    "/pause, /resume, /status\n"
    "/login — заново войти по QR"
)


def status_text() -> str:
    return (f"💼 Баланс: <b>{fmt(settings['balance'])} TON</b>\n"
            f"🎯 Мин. скидка: <b>{fmt(settings['discount'])}%</b>\n"
            f"💸 Мин. прибыль: <b>{fmt(settings['min_profit'])} TON</b>\n"
            f"🧾 Комиссии: покупатель {BUYER_FEE * 100:g}%, продавец {SELLER_FEE * 100:g}%\n"
            f"Состояние: {'▶️ работает' if settings['running'] else '⏸ на паузе'}")


def parse_number(text: str | None) -> float | None:
    try:
        v = float((text or "").strip().replace(",", "."))
        return v if v >= 0 else None
    except ValueError:
        return None


@dp.message(owner, Command("start", "help"))
async def cmd_start(m: Message):
    await m.answer(HELP + "\n\n" + status_text())


@dp.message(owner, Command("status"))
async def cmd_status(m: Message):
    await m.answer(status_text())


async def set_balance(m: Message, value: float | None):
    if value is None:
        return await m.answer("Пример: <code>/balance 50</code>")
    settings["balance"] = value
    seen_ids.clear()
    seen_order.clear()
    save_state()
    await m.answer(f"✅ Баланс <b>{fmt(value)} TON</b>. Ищу лоты до {value / (1 + BUYER_FEE):.2f} TON.")


@dp.message(owner, Command("balance"))
async def cmd_balance(m: Message, command: CommandObject):
    await set_balance(m, parse_number(command.args))


@dp.message(owner, Command("discount"))
async def cmd_discount(m: Message, command: CommandObject):
    v = parse_number(command.args)
    if v is None or not 1 <= v <= 90:
        return await m.answer("Пример: <code>/discount 15</code> (1–90)")
    settings["discount"] = v
    save_state()
    await m.answer(f"✅ Мин. скидка: <b>{fmt(v)}%</b>")


@dp.message(owner, Command("profit"))
async def cmd_profit(m: Message, command: CommandObject):
    v = parse_number(command.args)
    if v is None:
        return await m.answer("Пример: <code>/profit 0.5</code>")
    settings["min_profit"] = v
    save_state()
    await m.answer(f"✅ Мин. чистая прибыль: <b>{fmt(v)} TON</b>")


@dp.message(owner, Command("pause", "resume"))
async def cmd_pause(m: Message, command: CommandObject):
    settings["running"] = command.command == "resume"
    save_state()
    await m.answer("▶️ Поиск запущен." if settings["running"] else "⏸ Пауза. /resume — продолжить.")


@dp.message(owner, Command("scan"))
async def cmd_scan(m: Message):
    if settings["balance"] <= 0:
        return await m.answer("Сначала отправьте баланс числом.")
    await m.answer("🔎 Проверяю последние лоты (до минуты)…")
    started = time.time()
    try:
        st = await scan(m.bot, pages=5, recheck=True)
        text = (f"Готово за {time.time() - started:.0f} с.\n"
                f"Лотов в бюджете: {st['lots']}, проверено: {st['checked']}\n"
                f"Выгодных: <b>{st['found']}</b>")
        if st["errors"]:
            text += f"\n⚠️ Ошибок: {st['errors']}\n<code>{st['first_error']}</code>"
        await m.answer(text)
    except Exception as e:
        await m.answer(f"⚠️ <code>{str(e)[:300]}</code>")


@dp.message(owner, Command("debug"))
async def cmd_debug(m: Message):
    """Показывает, что отвечает Portals на каждый тип запроса."""
    lines = []
    try:
        lots = await search_listings("listed_at desc", limit=3)
        lines.append(f"✅ Лоты: {len(lots)} шт.")
        if lots:
            lot = lots[0]
            lines.append(f"Пример: {lot.get('name')} #{gift_number(lot)} — {lot.get('price')} TON, "
                         f"модель {attr(lot, 'model')[0]}, фон {attr(lot, 'backdrop')[0]}")
            lines.append(f"Поля: <code>{', '.join(sorted(lot.keys()))[:300]}</code>")
            same = await search_listings("price asc", limit=5, collection_id=lot.get("collection_id") or "",
                                         model=attr(lot, "model")[0])
            lines.append(f"✅ Та же модель на рынке: {len(same)} шт.")
            sales = await search_sales(collection_id=lot.get("collection_id") or "", model=attr(lot, "model")[0])
            lines.append(f"✅ Продажи модели: {len(sales)} шт.")
            if sales:
                lines.append(f"Поля продажи: <code>{', '.join(sorted(sales[0].keys()))[:200]}</code>")
    except Exception as e:
        lines.append(f"❌ <code>{str(e)[:400]}</code>")
    await m.answer("\n".join(lines))


@dp.message(owner, Command("login"))
async def cmd_login(m: Message):
    global login_task, tg
    if login_task and not login_task.done():
        return await m.answer("QR уже отправлен выше — отсканируйте его.")
    tg_ready.clear()
    if tg:
        await tg.disconnect()
        tg = None
    SESSION_FILE.unlink(missing_ok=True)
    login_task = asyncio.create_task(qr_login(m.bot))


@dp.message(owner, F.text, lambda m: password_waiter is not None and not password_waiter.done())
async def got_password(m: Message):
    try:
        await m.delete()
    except Exception:
        pass
    password_waiter.set_result(m.text)


@dp.message(owner, F.text)
async def plain_number(m: Message):
    v = parse_number(m.text)
    await (set_balance(m, v) if v is not None else m.answer(HELP))

# ───────────────────────── Запуск ─────────────────────────


async def stop_after(minutes: float):
    await asyncio.sleep(minutes * 60)
    log.info("Время смены вышло, сохраняю состояние и выхожу.")
    save_state()
    await dp.stop_polling()


async def main():
    global http
    required = {"BOT_TOKEN": BOT_TOKEN, "OWNER_ID": OWNER_ID, "API_ID": API_ID, "API_HASH": API_HASH}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit(f"Не заданы секреты: {', '.join(missing)}")

    http = AsyncSession()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await ensure_telegram(bot)
    if tg_ready.is_set():
        try:
            await auth.get()
        except Exception as e:
            await bot.send_message(OWNER_ID, f"❌ Не удалось получить доступ к Portals:\n<code>{str(e)[:300]}</code>")
            raise
    if RUN_MINUTES:
        asyncio.create_task(stop_after(RUN_MINUTES))
    asyncio.create_task(scanner_loop(bot))
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        save_state()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

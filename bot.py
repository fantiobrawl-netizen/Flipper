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
import colorsys
import tempfile
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
from telethon.errors import FloodWaitError, SessionPasswordNeededError, PasswordHashInvalidError
from rlottie_python import LottieAnimation
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


def _filters(params: dict, collection="", collection_id="", model="", backdrop="", max_price=None,
             external_collection_number=None) -> dict:
    if external_collection_number:
        params["external_collection_number"] = external_collection_number
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


def gift_slug(nft: dict) -> str:
    name = (nft.get("name") or "").replace("'", "").replace("’", "")
    short = "".join(p[:1].upper() + p[1:] for p in re.split(r"[^0-9A-Za-z]+", name) if p)
    return f"{short}-{gift_number(nft)}"


def nft_link(nft: dict) -> str:
    return f"https://t.me/nft/{gift_slug(nft)}"


def to_float(x, field="price") -> float:
    try:
        return float((x or {}).get(field) or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def fmt(v: float | None) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if v is not None else "—"

# ───────────────────────── Монохром по реальным цветам ─────────────────────────
# Цвет фона берём у Telegram (center_color), цвет модели — рендерим её стикер
# и считаем, какая доля цветных пикселей модели совпадает по оттенку с фоном.

COLORS_FILE = STATE_DIR / "colors.json"
_colors = load_json(COLORS_FILE, {"backdrops": {}, "models": {}})
_tg_sem = asyncio.Semaphore(1)
MONO_THRESHOLD = 0.40  # >= 40% цветных пикселей модели в оттенке фона — монохром


def save_colors() -> None:
    COLORS_FILE.write_text(json.dumps(_colors, ensure_ascii=False), encoding="utf-8")


def rgb_to_hsl(rgb: int) -> tuple[float, float, float]:
    r, g, b = ((rgb >> 16) & 255) / 255, ((rgb >> 8) & 255) / 255, (rgb & 255) / 255
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s, l


def model_pixels(tgs: bytes) -> list[list[float]]:
    """Рендерит модель и возвращает выборку пикселей [hue, sat, light]."""
    with tempfile.NamedTemporaryFile(suffix=".tgs") as f:
        f.write(tgs)
        f.flush()
        anim = LottieAnimation.from_tgs(f.name)
        frames = max(1, anim.lottie_animation_get_totalframe())
        img = anim.render_pillow_frame(frame_num=frames // 2, width=96, height=96)
    pixels = []
    for r, g, b, a in img.getdata():
        if a < 200:
            continue
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        pixels.append([round(h * 360, 1), round(s, 3), round(l, 3)])
    return pixels[::3]


def mono_share(pixels: list, backdrop_rgb: int) -> float:
    """Доля пикселей модели того же цвета, что и фон."""
    if not pixels:
        return 0.0
    bh, bs, bl = rgb_to_hsl(backdrop_rgb)
    if bs < 0.15:  # чёрный / белый / серый фон: сравниваем светлоту бесцветных пикселей
        same = [p for p in pixels if (p[1] < 0.25 or p[2] < 0.12 or p[2] > 0.9) and abs(p[2] - bl) < 0.25]
        return len(same) / len(pixels)
    colored = [p for p in pixels if p[1] >= 0.2 and 0.12 < p[2] < 0.92]
    if len(colored) < len(pixels) * 0.15:
        return 0.0
    same = [p for p in colored if min(abs(p[0] - bh), 360 - abs(p[0] - bh)) <= 40]
    return len(same) / len(colored)


async def learn_colors(nft: dict) -> None:
    """Узнаёт у Telegram цвет фона и картинку модели (один раз на фон и на модель)."""
    model, _ = attr(nft, "model")
    backdrop, _ = attr(nft, "backdrop")
    mkey = f"{nft.get('name')}|{model}"
    if backdrop in _colors["backdrops"] and mkey in _colors["models"]:
        return
    if not tg_ready.is_set() or not gift_number(nft):
        return
    async with _tg_sem:
        if backdrop in _colors["backdrops"] and mkey in _colors["models"]:
            return
        try:
            res = await tg(functions.payments.GetUniqueStarGiftRequest(slug=gift_slug(nft)))
            for a in res.gift.attributes:
                if isinstance(a, types.StarGiftAttributeBackdrop):
                    _colors["backdrops"][a.name] = a.center_color
                elif isinstance(a, types.StarGiftAttributeModel) and mkey not in _colors["models"]:
                    tgs = await tg.download_media(a.document, file=bytes)
                    _colors["models"][mkey] = await asyncio.to_thread(model_pixels, tgs)
            save_colors()
        except FloodWaitError as e:
            log.warning("Telegram просит подождать %s с (цвета подарков)", e.seconds)
            await asyncio.sleep(min(e.seconds, 60))
        except Exception as e:
            log.warning("Цвета %s: %s", gift_slug(nft), e)
            _colors["models"].setdefault(mkey, [])
        await asyncio.sleep(0.4)


def mono_of(nft: dict) -> float | None:
    """Доля совпадения цвета (0..1) или None, если цвета ещё неизвестны."""
    model, _ = attr(nft, "model")
    backdrop, _ = attr(nft, "backdrop")
    pixels = _colors["models"].get(f"{nft.get('name')}|{model}")
    rgb = _colors["backdrops"].get(backdrop)
    if not pixels or rgb is None:
        return None
    return mono_share(pixels, rgb)


def is_mono(nft: dict) -> bool:
    share = mono_of(nft)
    return share is not None and share >= MONO_THRESHOLD

# ───────────────────────── Данные рынка ─────────────────────────

_cache: dict[tuple, tuple[float, list]] = {}
CACHE_TTL = {"listings": 90, "sales": 600, "history": 600}


async def model_listings(lot: dict) -> list[dict]:
    model, _ = attr(lot, "model")
    key = ("listings", lot.get("collection_id"), model)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL["listings"]:
        return hit[1]
    data = await search_listings("price asc", limit=20, collection_id=lot.get("collection_id") or "",
                                 collection="" if lot.get("collection_id") else lot.get("name") or "",
                                 model=model)
    _cache[key] = (time.time(), data)
    return data


async def model_sales(lot: dict) -> list[dict]:
    model, _ = attr(lot, "model")
    key = ("sales", lot.get("collection_id"), model)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL["sales"]:
        return hit[1]
    data = []
    try:
        for page in range(2):  # до 40 последних продаж модели
            part = await search_sales(offset=page * 20, collection_id=lot.get("collection_id") or "",
                                      collection="" if lot.get("collection_id") else lot.get("name") or "",
                                      model=model)
            data += part
            if len(part) < 20:
                break
    except Exception as e:
        log.warning("История продаж недоступна: %s", e)
    now = datetime.now(timezone.utc)
    fresh = []
    for s in data:
        ts = parse_time(s.get("created_at"))
        if to_float(s, "amount") > 0 and not (ts and (now - ts).days > SALES_DAYS):
            s["_ts"] = ts
            fresh.append(s)
    _cache[key] = (time.time(), fresh)
    return fresh


async def gift_history(lot: dict) -> dict:
    """Покупки именно этого подарка: видно, не перекупщик ли его выставил."""
    key = ("history", lot.get("collection_id"), gift_number(lot))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL["history"]:
        return hit[1][0]
    buys = []
    try:
        actions = await search_sales(collection_id=lot.get("collection_id") or "",
                                     external_collection_number=gift_number(lot))
        for s in actions:
            ts = parse_time(s.get("created_at"))
            if to_float(s, "amount") > 0 and ts:
                buys.append((ts, to_float(s, "amount")))
    except Exception as e:
        log.warning("История подарка %s: %s", gift_slug(lot), e)
    buys.sort(reverse=True)
    now = datetime.now(timezone.utc)
    result = {
        "buys_30d": sum(1 for ts, _ in buys if (now - ts).days <= 30),
        "last_buy": buys[0] if buys else None,
    }
    _cache[key] = (time.time(), [result])
    return result

# ───────────────────────── Оценка цены ─────────────────────────


def median_or_none(values: list[float], min_n: int) -> float | None:
    return statistics.median(values) if len(values) >= min_n else None


def estimate(lot: dict, listings: list[dict], sales: list[dict]) -> dict | None:
    """Справедливая цена по похожим подаркам.

    Похожие = та же модель и тот же фон. Если лот монохромный — дополнительно
    сравниваем с другими монохромами этой модели. Берём МЕНЬШЕЕ из
    «минимальной цены похожего лота» и «медианы продаж похожих» — консервативно.
    """
    backdrop, _ = attr(lot, "backdrop")
    mono = is_mono(lot)
    others = [x for x in listings if x.get("id") != lot.get("id") and to_float(x) > 0]

    def group(items, same_backdrop: bool, mono_peer: bool):
        out = []
        for x in items:
            nft = x.get("nft") or x
            if same_backdrop and attr(nft, "backdrop")[0] != backdrop:
                continue
            if mono_peer and not is_mono(nft):
                continue
            out.append(x)
        return out

    ask_m = [to_float(x) for x in others]
    ask_mb = [to_float(x) for x in group(others, True, False)]
    ask_mono = [to_float(x) for x in group(others, False, True)] if mono else []
    sales_m = [to_float(s, "amount") for s in sales]
    sales_mb = [to_float(s, "amount") for s in group(sales, True, False)]
    sales_mono = [to_float(s, "amount") for s in group(sales, False, True)] if mono else []

    floor_m = min(ask_m) if ask_m else None
    floor_mb = min(ask_mb) if ask_mb else None
    floor_mono = min(ask_mono) if ask_mono else None
    med_m = median_or_none(sales_m, 3)
    med_mb = median_or_none(sales_mb, 2)
    med_mono = median_or_none(sales_mono, 2)

    # Чем точнее группа похожих, тем выше приоритет
    if floor_mb or med_mb:
        ask, sold, basis = floor_mb, med_mb, "модель + фон"
    elif mono and (floor_mono or med_mono):
        ask, sold, basis = floor_mono, med_mono, "монохромы этой модели"
    else:
        ask, sold, basis = floor_m, med_m, "модель"
    # если у точной группы чего-то не хватает — добираем из модели
    ask = ask or floor_m
    sold = sold or med_m
    candidates = [v for v in (ask, sold) if v]
    if not candidates:
        return None

    last_sale = max((s["_ts"] for s in sales if s.get("_ts")), default=None)
    return {
        "fair": min(candidates), "basis": basis, "mono": mono, "mono_share": mono_of(lot),
        "floor_m": floor_m, "n_ask_m": len(ask_m),
        "floor_mb": floor_mb, "n_ask_mb": len(ask_mb),
        "floor_mono": floor_mono, "n_ask_mono": len(ask_mono),
        "med_m": med_m, "n_sales_m": len(sales_m),
        "med_mb": med_mb, "n_sales_mb": len(sales_mb),
        "med_mono": med_mono, "n_sales_mono": len(sales_mono),
        "last_sale": last_sale,
        "only_listings": sold is None,
    }


def is_locked(lot: dict) -> datetime | None:
    ts = parse_time(lot.get("unlocks_at"))
    return ts if ts and ts > datetime.now(timezone.utc) else None


def deal_score(discount: float, roi: float, est: dict, hist: dict | None, lot: dict) -> tuple[int, list[str]]:
    """Насколько стоит брать: 0–100 и короткие причины."""
    notes = []
    score = min(35.0, max(0.0, discount) * 35 / 30)                 # скидка: 30% и выше = 35 баллов
    score += min(20.0, max(0.0, roi) * 20 / 25)                      # чистая доходность: 25% = 20 баллов
    liq = est["n_sales_m"]
    score += min(15.0, liq * 15 / 20)                                # ликвидность: 20+ продаж за 30 дн.
    if liq < 3:
        notes.append("мало продаж модели — может долго не продаваться")
    if est["n_sales_mb"] >= 2 or est["n_sales_mono"] >= 2:
        score += 10                                                  # цена подтверждена продажами похожих
    elif not est["only_listings"]:
        score += 5
    else:
        notes.append("оценка только по выставленным лотам")
    if est["last_sale"] and (datetime.now(timezone.utc) - est["last_sale"]).days <= 1:
        score += 5
    bonus = 0
    if est["mono"]:
        bonus += 8
    if number_tag(gift_number(lot)):
        bonus += 5
    if (attr(lot, "backdrop")[1] or 100) < 1 or (attr(lot, "symbol")[1] or 100) < 0.3:
        bonus += 4
    score += min(15, bonus)
    if is_locked(lot):
        score -= 10
        notes.append("заблокирован — перепродать сразу нельзя")
    if hist and hist["last_buy"]:
        days = (datetime.now(timezone.utc) - hist["last_buy"][0]).days
        if days <= 7:
            score -= 10
            notes.append("недавно перекупили — продавец, скорее всего, флиппер")
    return max(0, min(100, round(score))), notes


def verdict(score: int) -> str:
    return "🟢 брать" if score >= 70 else "🟡 можно, но проверьте" if score >= 45 else "🔴 рискованно"

# ───────────────────────── Проверка лотов ─────────────────────────

NOTIFIED_FILE = STATE_DIR / "notified.json"
notified: dict[str, float] = load_json(NOTIFIED_FILE, {})


async def evaluate_model(bot: Bot, sample: dict, stats: dict) -> None:
    """Оценивает ВСЕ дешёвые лоты модели, а не только только что выставленный."""
    listings = await model_listings(sample)
    sales = await model_sales(sample)
    if not listings:
        return
    # Узнаём цвета для фонов, которые встречаются среди лотов и продаж модели
    for nft in [sample] + listings + [s.get("nft") or {} for s in sales]:
        if nft.get("name") and gift_number(nft):
            await learn_colors(nft)

    deals = []
    for lot in listings:
        price = to_float(lot)
        cost = price * (1 + BUYER_FEE)
        if price <= 0 or cost > settings["balance"]:
            continue
        est = estimate(lot, listings, sales)
        stats["checked"] += 1
        if not est:
            stats["no_estimate"] += 1
            continue
        fair = est["fair"]
        discount = (1 - price / fair) * 100
        sell_price = fair * (1 - UNDERCUT)
        net = sell_price * (1 - SELLER_FEE) - cost
        stats["estimated"] += 1
        stats["with_sales"] += not est["only_listings"]
        stats["top"].append((discount, net, f"{lot.get('name')} #{gift_number(lot)} "
                             f"({attr(lot, 'model')[0]}{', моно' if est['mono'] else ''}): "
                             f"{fmt(price)} → {fmt(fair)}"))
        log.info("Лот %s: цена %s, оценка %s (%s), скидка %.1f%%, прибыль %.2f, моно %s",
                 gift_slug(lot), price, fair, est["basis"], discount, net, est["mono_share"])
        if discount < settings["discount"] or net < settings["min_profit"]:
            continue
        if lot.get("id") in notified and price >= notified[lot["id"]]:
            continue  # уже присылали по этой или меньшей цене
        deals.append((lot, price, cost, discount, sell_price, net, est))

    for lot, price, cost, discount, sell_price, net, est in sorted(deals, key=lambda d: -d[3]):
        hist = await gift_history(lot)
        score, notes = deal_score(discount, net / cost * 100, est, hist, lot)
        text = format_lot(lot, price, cost, discount, sell_price, net, est, hist, score, notes, listings)
        await bot.send_message(OWNER_ID, text, disable_web_page_preview=False)
        notified[lot["id"]] = price
        stats["found"] += 1
    if deals:
        NOTIFIED_FILE.write_text(json.dumps(dict(list(notified.items())[-3000:])), encoding="utf-8")


def format_lot(lot, price, cost, discount, sell_price, net, est, hist, score, notes, listings) -> str:
    model, m_r = attr(lot, "model")
    backdrop, b_r = attr(lot, "backdrop")
    symbol, s_r = attr(lot, "symbol")
    pct = lambda r: f" ({r:g}%)" if r is not None else ""
    num = gift_number(lot)
    num_tag = number_tag(num)
    now = datetime.now(timezone.utc)

    lines = [f"🔥 <b>−{discount:.0f}%</b> · <b>{lot.get('name')} #{num}</b>",
             f"⭐ Оценка сделки: <b>{score}/100</b> — {verdict(score)}",
             "",
             f"🧸 Модель: {model}{pct(m_r)}",
             f"🎨 Фон: {backdrop}{pct(b_r)}",
             f"✳️ Символ: {symbol}{pct(s_r)}"]
    if est["mono_share"] is not None:
        share = est["mono_share"] * 100
        lines.append(f"⚫ Монохром: <b>да</b> ({share:.0f}% цвета модели совпадает с фоном)" if est["mono"]
                     else f"⚪ Монохром: нет ({share:.0f}% совпадения цвета)")
    if num_tag:
        lines.append(f"🔢 Номер #{num}: {num_tag}")

    lines += ["",
              f"💰 Цена: <b>{fmt(price)} TON</b> → с комиссией покупателя {fmt(cost)}",
              f"📊 Оценка рынка: <b>{fmt(est['fair'])} TON</b> (по группе: {est['basis']})",
              f"  • мин. лот модель+фон: {fmt(est['floor_mb'])} ({est['n_ask_mb']} шт.)"]
    if est["mono"]:
        lines.append(f"  • мин. лот монохрома модели: {fmt(est['floor_mono'])} ({est['n_ask_mono']} шт.)")
    lines.append(f"  • мин. лот модели: {fmt(est['floor_m'])} ({est['n_ask_m']} шт.)")
    lines.append(f"  • продажи модель+фон: медиана {fmt(est['med_mb'])} (n={est['n_sales_mb']})")
    if est["mono"]:
        lines.append(f"  • продажи монохромов: медиана {fmt(est['med_mono'])} (n={est['n_sales_mono']})")
    lines.append(f"  • продажи модели: медиана {fmt(est['med_m'])} (n={est['n_sales_m']})")
    if est["last_sale"]:
        days = (now - est["last_sale"]).days
        lines.append(f"  • последняя продажа модели: {'сегодня' if days == 0 else f'{days} дн. назад'}")

    # Похожие лоты на рынке — чтобы сравнить глазами
    same_group = [x for x in listings if x.get("id") != lot.get("id") and (
        attr(x, "backdrop")[0] == backdrop or (est["mono"] and is_mono(x)))][:3]
    if same_group:
        lines.append("")
        lines.append("🔍 Похожие на рынке:")
        for x in same_group:
            tag = " · моно" if is_mono(x) else ""
            lines.append(f"  • #{gift_number(x)} — {fmt(to_float(x))} TON ({attr(x, 'backdrop')[0]}{tag})")

    lines.append("")
    if hist and hist["last_buy"]:
        ts, amount = hist["last_buy"]
        days = (now - ts).days
        markup = (price / amount - 1) * 100 if amount else 0
        lines.append(f"🕓 Этот подарок покупали {'сегодня' if days == 0 else f'{days} дн. назад'} "
                     f"за {fmt(amount)} TON (сейчас {markup:+.0f}%). Покупок за 30 дн.: {hist['buys_30d']}")
    else:
        lines.append("🕓 Недавних покупок этого подарка нет — не перекупленный")

    roi = net / cost * 100
    lines += [f"💸 Если выставить за {fmt(sell_price)}: чистыми <b>{net:+.2f} TON ({roi:+.0f}%)</b>",
              f"   (комиссия продавца {SELLER_FEE * 100:g}%, покупателя {BUYER_FEE * 100:g}%)"]
    locked = is_locked(lot)
    if locked:
        lines.append(f"🔒 Заблокирован до {locked:%d.%m %H:%M} UTC")
    if notes:
        lines.append("⚠️ " + "; ".join(notes))
    lines += ["", f"🎁 {nft_link(lot)}", "🛒 Купить: https://t.me/portals"]
    return "\n".join(lines)


scan_lock = asyncio.Lock()


async def scan(bot: Bot, pages: int = 1, recheck: bool = False) -> dict:
    """Смотрит новые лоты и для каждой встреченной модели проверяет все её дешёвые лоты."""
    stats = {"lots": 0, "checked": 0, "found": 0, "errors": 0, "first_error": "",
             "estimated": 0, "with_sales": 0, "no_estimate": 0, "top": []}
    max_price = settings["balance"] / (1 + BUYER_FEE)
    done_models = set()
    async with scan_lock:
        for page in range(pages):
            lots = await search_listings("listed_at desc", limit=20, offset=page * 20, max_price=max_price)
            stats["lots"] += len(lots)
            for lot in lots:
                lot_id = lot.get("id")
                if not lot_id or (lot_id in seen_ids and not recheck):
                    continue
                remember(lot_id)
                key = (lot.get("collection_id"), attr(lot, "model")[0])
                if key in done_models:
                    continue
                done_models.add(key)
                try:
                    await evaluate_model(bot, lot, stats)
                except Exception as e:
                    stats["errors"] += 1
                    stats["first_error"] = stats["first_error"] or str(e)[:300]
                    log.warning("Модель %s: %s", key, e)
                    if stats["errors"] >= 3 and stats["estimated"] == 0:
                        raise RuntimeError(f"Все проверки падают: {stats['first_error']}")
            if len(lots) < 20:
                break
        save_state()
    stats["top"].sort(key=lambda x: -x[0])
    log.info("Скан: лотов %s, моделей %s, проверено %s, оценено %s (с продажами %s), найдено %s, ошибок %s",
             stats["lots"], len(done_models), stats["checked"], stats["estimated"], stats["with_sales"],
             stats["found"], stats["errors"])
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
                f"Оценено: {st['estimated']} (по продажам: {st['with_sales']}), "
                f"без данных для оценки: {st['no_estimate']}\n"
                f"Выгодных (от {fmt(settings['discount'])}%): <b>{st['found']}</b>")
        if st["top"]:
            text += "\n\n<b>Ближе всего к порогу:</b>\n" + "\n".join(
                f"{d:+.0f}% · {n:+.2f} TON · {t}" for d, n, t in st["top"][:5])
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

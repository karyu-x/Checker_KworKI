import os
import re
import time
import json
import html as html_lib
import threading
import asyncio
import email
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Union

from dotenv import load_dotenv
from imapclient import IMAPClient
from bs4 import BeautifulSoup
from bs4 import FeatureNotFound

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties


# ---------------- Windows friendly ----------------
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass


# ---------------- ENV ----------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TG_CHAT_ID_RAW = os.getenv("TG_CHAT_ID", "").strip()
TG_CHANNEL_CHAT_ID_RAW = os.getenv("TG_CHANNEL_CHAT_ID", "").strip()

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com").strip()
FOLDER = os.getenv("GMAIL_FOLDER", "KWORK_PROJECTS").strip()

# Важно: RAW_QUERY может содержать русский subject — ищем через charset UTF-8
RAW_QUERY = os.getenv(
    "GMAIL_RAW_QUERY",
    'from:news@kwork.ru subject:"Новые проекты на бирже Kwork"'
).strip()

MAX_ITEMS = int(os.getenv("MAX_ITEMS", "10"))
MAX_EMAIL_AGE_MINUTES = int(os.getenv("MAX_EMAIL_AGE_MINUTES", "600"))

# watcher: отправлять в личку или только в канал
SEND_WATCHER_TO_USER = os.getenv("SEND_WATCHER_TO_USER", "0") == "1"

# отключать превью ссылок в Telegram
DISABLE_WEB_PREVIEW = os.getenv("DISABLE_WEB_PREVIEW", "1") == "1"

# State: куда писать (для Railway лучше на Volume)
# Если на Railway подключен Volume — появится переменная RAILWAY_VOLUME_MOUNT_PATH
RAILWAY_VOLUME_MOUNT_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
STATE_FILE_ENV = os.getenv("STATE_FILE", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _default_state_path() -> str:
    if STATE_FILE_ENV:
        # Если абсолютный путь — используем как есть
        if os.path.isabs(STATE_FILE_ENV):
            return STATE_FILE_ENV
        return os.path.join(BASE_DIR, STATE_FILE_ENV)

    if RAILWAY_VOLUME_MOUNT_PATH:
        return os.path.join(RAILWAY_VOLUME_MOUNT_PATH, "state.json")

    return os.path.join(BASE_DIR, "state.json")

STATE_FILE = _default_state_path()
SEARCH_CHARSET = "UTF-8"


def _parse_chat_id(v: str) -> Union[int, str]:
    v = (v or "").strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v  # например "@kwork_checker"


missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "TG_CHAT_ID": TG_CHAT_ID_RAW,
    "TG_CHANNEL_CHAT_ID": TG_CHANNEL_CHAT_ID_RAW,
    "GMAIL_USER": GMAIL_USER,
    "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
}.items() if not v]
if missing:
    raise RuntimeError(f"Не хватает переменных: {', '.join(missing)}")

TG_CHAT_ID = _parse_chat_id(TG_CHAT_ID_RAW)
TG_CHANNEL_CHAT_ID = _parse_chat_id(TG_CHANNEL_CHAT_ID_RAW)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


# ---------------- State (anti-duplicates) ----------------
_state_lock = threading.Lock()
_last_seen: Optional[Tuple[datetime, int]] = None  # (dt_utc, uid)


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True) if os.path.dirname(STATE_FILE) else None
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _state_get_last_seen() -> Optional[Tuple[datetime, int]]:
    st = _load_state()
    dt_iso = st.get("last_dt_iso")
    uid = st.get("last_uid")
    if not dt_iso or uid is None:
        return None
    try:
        dt = datetime.fromisoformat(dt_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return (dt, int(uid))
    except Exception:
        return None


def _state_set_last_seen(dt: datetime, uid: int) -> None:
    dt = dt.astimezone(timezone.utc)
    _save_state({"last_dt_iso": dt.isoformat(), "last_uid": int(uid)})


with _state_lock:
    _last_seen = _state_get_last_seen()


# ---------------- Helpers ----------------
def _decode_mime(s: str) -> str:
    if not s:
        return ""
    out = ""
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="ignore")
        else:
            out += part
    return out


def _soup(html_text: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html_text, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html_text, "html.parser")


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_fresh(dt: datetime) -> bool:
    now = datetime.now(timezone.utc)
    return dt >= now - timedelta(minutes=MAX_EMAIL_AGE_MINUTES)


def extract_parts(rfc822: bytes) -> dict:
    msg = email.message_from_bytes(rfc822)
    subject = _decode_mime(msg.get("Subject", ""))
    from_ = _decode_mime(msg.get("From", ""))
    date_ = _decode_mime(msg.get("Date", ""))

    text_plain = ""
    text_html = ""

    if msg.is_multipart():
        for p in msg.walk():
            ctype = p.get_content_type()
            disp = str(p.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            payload = p.get_payload(decode=True) or b""
            if ctype == "text/plain" and not text_plain:
                text_plain = payload.decode(errors="ignore")
            elif ctype == "text/html" and not text_html:
                text_html = payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True) or b""
        text_plain = payload.decode(errors="ignore")

    return {"from": from_, "subject": subject, "date": date_, "plain": text_plain, "html": text_html}


def extract_project_links_ordered(html_text: str) -> list[str]:
    """
    Достаём ссылки на отклик вида /new_offer?project=XXXX (или где есть project=).
    """
    soup = _soup(html_text)
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if "kwork.ru" not in href:
            continue
        if ("new_offer?project=" in href) or ("project=" in href):
            if href not in seen:
                seen.add(href)
                urls.append(href)
    return urls


def parse_from_text(text: str) -> dict:
    available = None
    total = None
    timeframe = None

    # +108 новых подходящих проектов
    m = re.search(r"\+(\d+)\s+новых\s+подходящих\s+проект", text, re.IGNORECASE)
    if m:
        available = int(m.group(1))

    # "За последние 6 дней ... размещено 1886 новых проектов"
    m = re.search(
        r"За последние\s+([0-9]+\s+\S+)\s+на бирже.*?размещено\s+([\d\s]+)\s+новых\s+проект",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        timeframe = m.group(1).strip()
        total = int(m.group(2).replace(" ", ""))

    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]

    price_re = re.compile(r"^\d[\d\s]*\s*(Р|₽)$", re.IGNORECASE)
    cat_re = re.compile(r">")

    projects = []
    i = 0
    while i < len(lines):
        title = lines[i]
        low = title.lower()

        # мусор
        if title in ("Название", "Покупатель", "Цена"):
            i += 1
            continue
        if ("перейти на" in low) or ("ваши настройки" in low) or ("отписаться" in low):
            i += 1
            continue
        if "новых подходящих" in low:
            i += 1
            continue

        category = None
        if i + 1 < len(lines) and cat_re.search(lines[i + 1]):
            category = lines[i + 1]

        price = None
        buyer = None
        hired = None

        j = i + 1
        limit = min(len(lines), i + 20)
        while j < limit:
            line = lines[j]
            llow = line.lower()
            tokens = line.split()

            # buyer: "v v_ritme 1" -> buyer=v_ritme
            if buyer is None and len(tokens) >= 2 and tokens[0].isalpha() and len(tokens[0]) == 1:
                cand = tokens[1]
                if (not cand.isdigit()) and (not re.search(r"\bпроект", llow)):
                    if re.match(r"^[A-Za-z0-9_.-]{3,}$", cand):
                        buyer = cand

            # buyer: "codesDF" (без префикса)
            if buyer is None and len(tokens) >= 1:
                t0 = tokens[0]
                if re.match(r"^[A-Za-z][A-Za-z0-9_.-]{2,}$", t0) and not re.search(r"\bпроект", llow):
                    buyer = t0

            if "%" in line and "нанят" in llow:
                hired = line

            if price_re.match(line):
                price = line.replace("Р", "₽").strip()
                break

            j += 1

        if category and price:
            projects.append({
                "title": title,
                "category": category,
                "buyer": buyer,
                "hired": hired,
                "price": price,
            })
            i = j + 1
        else:
            i += 1

    return {"available": available, "total": total, "timeframe": timeframe, "projects": projects}


def parse_kwork_email(parts: dict) -> dict:
    if parts["html"]:
        soup = _soup(parts["html"])
        text = soup.get_text("\n")
        data = parse_from_text(text)

        links = extract_project_links_ordered(parts["html"])
        if links and data.get("projects"):
            # приклеим ссылки по порядку
            for idx, p in enumerate(data["projects"]):
                if idx < len(links):
                    p["url"] = links[idx]
        return data

    return parse_from_text(parts["plain"] or "")


def compose_message(meta: dict, data: dict) -> str:
    available = data.get("available")
    total = data.get("total")
    timeframe = data.get("timeframe")
    projects = (data.get("projects") or [])[:MAX_ITEMS]

    header = "📬 <b>Kwork: новые проекты</b>"
    if available is not None and total is not None and timeframe:
        header += f"\n➕ <b>{available}</b> подходящих (из {total} за {html_lib.escape(timeframe)})"
    elif available is not None:
        header += f"\n➕ <b>{available}</b> подходящих"

    subj = meta.get("subject", "")
    if subj:
        header += f"\n🧾 <i>{html_lib.escape(subj)}</i>"

    out = [header, ""]
    for p in projects:
        title = p.get("title", "")
        price = p.get("price", "")
        buyer = p.get("buyer")
        category = p.get("category", "")
        hired = p.get("hired")
        url = p.get("url")

        safe_title = html_lib.escape(title)
        if url:
            safe_url = html_lib.escape(url, quote=True)
            title_part = f'<a href="{safe_url}">{safe_title}</a>'
        else:
            title_part = safe_title

        line = f"• {title_part} — <b>{html_lib.escape(price)}</b>"
        if buyer:
            line += f" (👤 {html_lib.escape(buyer)})"

        out.append(line)
        if category:
            out.append(f"  {html_lib.escape(category)}")
        if hired:
            out.append(f"  {html_lib.escape(hired)}")
        out.append("")

    if not projects:
        out.append("Не смог распарсить проекты (если надо — подточим парсер под HTML письма).")

    return "\n".join(out).strip()


async def send_to_targets(text: str, user_chat_id: Optional[Union[int, str]] = None, force_user: bool = False):
    # Канал — всегда
    await bot.send_message(TG_CHANNEL_CHAT_ID, text, disable_web_page_preview=DISABLE_WEB_PREVIEW)

    # Личка — если нужно
    if user_chat_id is not None and (force_user or SEND_WATCHER_TO_USER):
        await bot.send_message(user_chat_id, text, disable_web_page_preview=DISABLE_WEB_PREVIEW)


# ---------------- IMAP logic ----------------
def _select_folder_try(c: IMAPClient, folder: str) -> bool:
    try:
        c.select_folder(folder)
        return True
    except Exception:
        return False


def _select_folder_smart(c: IMAPClient) -> str:
    # 1) то, что указано (например KWORK_PROJECTS)
    if _select_folder_try(c, FOLDER):
        return FOLDER
    # 2) fallback: All Mail
    if _select_folder_try(c, "[Gmail]/Вся почта"):
        return "[Gmail]/Вся почта"
    # 3) fallback: inbox
    c.select_folder("INBOX")
    return "INBOX"


def _search_uids(c: IMAPClient):
    # charset UTF-8 — чтобы русские буквы в RAW_QUERY нормально работали
    return c.search(["X-GM-RAW", RAW_QUERY], charset=SEARCH_CHARSET)


def _fetch_candidates(c: IMAPClient) -> list[tuple[datetime, int, bytes]]:
    """
    Возвращаем список (dt_utc, uid, raw), отсортированный по (dt, uid).
    """
    uids = _search_uids(c)
    if not uids:
        return []

    tail = uids[-80:]
    fetched = c.fetch(tail, ["INTERNALDATE", "RFC822"])

    items = []
    for uid in tail:
        item = fetched.get(uid)
        if not item:
            continue
        dt = item.get(b"INTERNALDATE")
        raw = item.get(b"RFC822")
        if not dt or not raw:
            continue
        items.append((_to_utc(dt), int(uid), raw))

    items.sort(key=lambda x: (x[0], x[1]))
    return items


def imap_idle_worker(loop: asyncio.AbstractEventLoop):
    backoff = 5

    while True:
        try:
            with IMAPClient(IMAP_HOST, ssl=True) as c:
                c.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                used_folder = _select_folder_smart(c)

                # первый запуск: если state пустой — фиксируем последнее и не шлём старое
                global _last_seen
                with _state_lock:
                    if _last_seen is None:
                        items = _fetch_candidates(c)
                        if items:
                            dt0, uid0, _ = items[-1]
                            _last_seen = (dt0, uid0)
                            _state_set_last_seen(dt0, uid0)

                backoff = 5

                while True:
                    c.idle()
                    responses = c.idle_check(timeout=60 * 25)
                    c.idle_done()

                    if not responses:
                        continue

                    items = _fetch_candidates(c)
                    if not items:
                        continue

                    with _state_lock:
                        last_seen = _last_seen

                    new_items = []
                    for dt, uid, raw in items:
                        if not _is_fresh(dt):
                            continue
                        if last_seen is not None and (dt, uid) <= last_seen:
                            continue
                        new_items.append((dt, uid, raw))

                    if not new_items:
                        continue

                    for dt, uid, raw in new_items:
                        parts = extract_parts(raw)
                        parsed = parse_kwork_email(parts)
                        text = compose_message(parts, parsed)

                        asyncio.run_coroutine_threadsafe(
                            send_to_targets(text, user_chat_id=TG_CHAT_ID, force_user=False),
                            loop
                        )

                        with _state_lock:
                            _last_seen = (dt, uid)
                            _state_set_last_seen(dt, uid)

        except Exception as e:
            # уведомим в личку
            try:
                asyncio.run_coroutine_threadsafe(
                    bot.send_message(TG_CHAT_ID, f"⚠️ Gmail watcher перезапускается: {e}"),
                    loop
                )
            except Exception:
                pass

            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------- Telegram commands ----------------
HELP_TEXT = (
    "🤖 <b>Kwork Gmail Checker</b>\n\n"
    "<b>Команды:</b>\n"
    "• /help — помощь\n"
    "• /now — отправить <b>только если есть новое</b> письмо (в канал и тебе)\n"
    "• /now_force — переслать последнее письмо <b>принудительно</b> (даже если уже было)\n"
    "• /status — показать состояние (state, папка, query)\n"
    "• /reset_state — зафиксировать текущее последнее письмо как отправленное\n\n"
    "<b>Логика:</b>\n"
    "• watcher шлёт в канал всегда\n"
    "• watcher шлёт в личку только если SEND_WATCHER_TO_USER=1\n"
    "• /now шлёт и в канал, и в личку всегда (но без повторов)\n"
)


@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(HELP_TEXT)


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(HELP_TEXT)


@dp.message(Command("status"))
async def status(m: Message):
    with _state_lock:
        ls = _last_seen
    ls_txt = "нет" if ls is None else f"{ls[0].strftime('%Y-%m-%d %H:%M UTC')} (uid={ls[1]})"

    await m.answer(
        "📌 <b>Status</b>\n"
        f"STATE_FILE: <code>{html_lib.escape(STATE_FILE)}</code>\n"
        f"FOLDER: <code>{html_lib.escape(FOLDER)}</code>\n"
        f"RAW_QUERY: <code>{html_lib.escape(RAW_QUERY)}</code>\n"
        f"MAX_EMAIL_AGE_MINUTES: <b>{MAX_EMAIL_AGE_MINUTES}</b>\n"
        f"last_seen: <b>{html_lib.escape(ls_txt)}</b>\n"
        f"SEND_WATCHER_TO_USER: <b>{'1' if SEND_WATCHER_TO_USER else '0'}</b>\n"
    )


@dp.message(Command("now"))
async def now(m: Message):
    await m.answer("Проверяю, есть ли новое письмо…")
    try:
        with IMAPClient(IMAP_HOST, ssl=True) as c:
            c.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            used_folder = _select_folder_smart(c)

            items = _fetch_candidates(c)
            if not items:
                await m.answer(
                    "Не нашёл писем по запросу.\n"
                    f"Папка: <code>{html_lib.escape(used_folder)}</code>\n"
                    f"RAW_QUERY: <code>{html_lib.escape(RAW_QUERY)}</code>"
                )
                return

            dt, uid, raw = items[-1]

            if not _is_fresh(dt):
                await m.answer(
                    "Нашёл письмо, но оно слишком старое по MAX_EMAIL_AGE_MINUTES.\n"
                    f"MAX_EMAIL_AGE_MINUTES=<b>{MAX_EMAIL_AGE_MINUTES}</b>"
                )
                return

            with _state_lock:
                global _last_seen
                last_seen = _last_seen

            if last_seen is not None and (dt, uid) <= last_seen:
                await m.answer(
                    "⏳ Новых писем пока нет.\n"
                    f"Последнее уже отправленное: <b>{last_seen[0].strftime('%Y-%m-%d %H:%M UTC')}</b> (uid={last_seen[1]})\n"
                    "Если всё равно хочешь переслать — /now_force"
                )
                return

            parts = extract_parts(raw)
            parsed = parse_kwork_email(parts)
            text = compose_message(parts, parsed)

            # /now: всегда и в канал, и пользователю
            await send_to_targets(text, user_chat_id=m.chat.id, force_user=True)

            with _state_lock:
                _last_seen = (dt, uid)
                _state_set_last_seen(dt, uid)

    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@dp.message(Command("now_force"))
async def now_force(m: Message):
    await m.answer("Ок, пересылаю последнее письмо принудительно…")
    try:
        with IMAPClient(IMAP_HOST, ssl=True) as c:
            c.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            _select_folder_smart(c)

            items = _fetch_candidates(c)
            if not items:
                await m.answer("Не нашёл писем по запросу.")
                return

            dt, uid, raw = items[-1]

            parts = extract_parts(raw)
            parsed = parse_kwork_email(parts)
            text = compose_message(parts, parsed)

            await send_to_targets(text, user_chat_id=m.chat.id, force_user=True)

            with _state_lock:
                global _last_seen
                _last_seen = (dt, uid)
                _state_set_last_seen(dt, uid)

    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@dp.message(Command("reset_state"))
async def reset_state(m: Message):
    """
    Зафиксировать текущее последнее письмо как "уже отправлено".
    """
    await m.answer("Сбрасываю state: фиксирую текущее последнее письмо как отправленное…")
    try:
        with IMAPClient(IMAP_HOST, ssl=True) as c:
            c.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            used_folder = _select_folder_smart(c)

            items = _fetch_candidates(c)
            if not items:
                await m.answer("Нет писем по запросу — сбрасывать нечего.")
                return

            dt, uid, _ = items[-1]
            with _state_lock:
                global _last_seen
                _last_seen = (dt, uid)
                _state_set_last_seen(dt, uid)

            await m.answer("✅ Готово. Теперь бот будет ждать только новые письма.")

    except Exception as e:
        await m.answer(f"Ошибка reset_state: {e}")


async def main():
    loop = asyncio.get_running_loop()
    t = threading.Thread(target=imap_idle_worker, args=(loop,), daemon=True)
    t.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    print("Start_checker")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stop_checker")
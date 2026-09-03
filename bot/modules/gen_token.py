"""Generate token.pickle via /token command in bot.
Uses bot's BASE_URL as redirect URI — no localhost issue."""
import os
import secrets
from asyncio import Event, wait_for, TimeoutError as AsyncTimeout
from pickle import dump as pdump, load as pload
from urllib.parse import urlencode, urlparse, parse_qs

from pyrogram.enums import ChatType
from pyrogram.filters import create, user, text, private
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from ..core.tg_client import TgClient
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_time
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message, delete_message

_STOP = "gentoken_stop"
_TIMEOUT = 300
SCOPES = "https://www.googleapis.com/auth/drive"
CREDENTIALS_FILE = "/usr/src/app/credentials.json"
TOKEN_FILE = "/usr/src/app/token.pickle"
_pending = {}


def _stop_filter(uid):
    async def _check(_, __, update):
        return update.data == _STOP and update.from_user.id == uid
    return create(_check)


def _stop_btns():
    btns = ButtonMaker()
    btns.data_button("Cancel", data=_STOP)
    return btns.build_menu(1)


def _header(name):
    return f"⌬ <u><b>Google Drive Token Generator</b></u>\n│ <b>{name}</b>"


async def _wait_input(user_id, timeout=_TIMEOUT):
    event = Event()
    result = [None]

    async def _on_text(_, msg):
        await delete_message(msg)
        result[0] = ("text", msg.text or "")
        event.set()

    async def _on_stop(_, q):
        await q.answer()
        result[0] = ("stop", None)
        event.set()

    h1 = TgClient.bot.add_handler(
        MessageHandler(_on_text, filters=user(user_id) & text & private), group=-1)
    h2 = TgClient.bot.add_handler(
        CallbackQueryHandler(_on_stop, filters=_stop_filter(user_id)), group=-1)
    try:
        await wait_for(event.wait(), timeout)
    except AsyncTimeout:
        result[0] = None
    finally:
        TgClient.bot.remove_handler(*h1)
        TgClient.bot.remove_handler(*h2)
    return result[0]


async def _handle_token_exchange(code, user_id, h):
    """Exchange code for token — called by callback handler."""
    try:
        import json
        from google_auth_oauthlib.flow import InstalledAppFlow
        with open(CREDENTIALS_FILE) as f:
            cid = json.load(f).get("installed", {}).get("web", {}).get("client_id", "")

        # Create flow with our redirect_uri
        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE, [SCOPES],
            redirect_uri=f"{Config.BASE_URL.rstrip('/')}/token_callback"
        )
        flow.fetch_token(code=code)
        creds = flow.credentials
        with open(TOKEN_FILE, "wb") as f:
            pdump(creds, f)
        await TgClient.bot.send_message(
            user_id,
            f"{_header('Ajay')}\n┃\n"
            "┠  <b>token.pickle generated!</b>\n"
            "┠ Set: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
            "┖ <code>/restart</code>",
        )
    except Exception as e:
        await TgClient.bot.send_message(
            user_id,
            f"{_header('Ajay')}\n┃\n┖ <b>Failed:</b> <i>{e}</i>",
        )
    _pending.pop(user_id, None)


async def token_callback_handler(request):
    """Web callback — Google redirects here after auth."""
    from aiohttp import web
    params = parse_qs(request.query_string)
    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    error = params.get("error", [None])[0]

    if error:
        return web.Response(
            text=f"<h2>Error: {error}</h2><p>Close and try /token again.</p>",
            content_type="text/html")

    if not code or state not in _pending:
        return web.Response(
            text="<h2>Invalid request</h2>", content_type="text/html")

    user_id = _pending[state]
    import __main__
    import asyncio
    asyncio.get_event_loop().create_task(_handle_token_exchange(code, user_id, ""))

    return web.Response(
        text="<h2>Token saved!</h2><p>You can close this tab.</p>",
        content_type="text/html")


@new_task
async def gen_gdrive_token(_, message):
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    h = _header(user_name)
    btns = _stop_btns()

    # Check credentials.json
    if not os.path.exists(CREDENTIALS_FILE):
        return await send_message(
            message,
            f"{h}\n┃\n┖ <b>credentials.json not found!</b>\nSend the file first.")

    # Check valid token
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "rb") as f:
                creds = pload(f)
            if creds and creds.valid:
                return await send_message(
                    message, f"{h}\n┃\n┖ <b>Already valid!</b>")
        except Exception:
            pass

    # Check BASE_URL
    base_url = (Config.BASE_URL or "").rstrip("/")
    if not base_url:
        return await send_message(
            message, f"{h}\n┃\n┖ <b>BASE_URL not set!</b>")

    callback_url = f"{base_url}/token_callback"

    # Build auth URL with explicit redirect_uri
    import json
    try:
        with open(CREDENTIALS_FILE) as f:
            creds_data = json.load(f)
        client_id = creds_data.get("installed", creds_data.get("web", {}))["client_id"]
    except Exception as e:
        return await send_message(message, f"{h}\n┃\n┖ <b>Error reading credentials.json:</b> <i>{e}</i>")

    state = secrets.token_urlsafe(16)
    _pending[state] = user_id

    auth_url = "https://accounts.google.com/o/oauth2/auth?" + urlencode({
        "client_id": client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })

    await send_message(
        message,
        f"{h}\n┃\n"
        f"┖ <a href='{auth_url}'>🔐 Authorize Google Drive</a>\n\n"
        f"Click → Login → Allow\n"
        f"Token auto-saves.",
        btns)

    # Wait for callback or timeout
    done = Event()

    async def _poll():
        while state in _pending:
            await __import__("asyncio").sleep(1)
        done.set()

    import asyncio
    asyncio.get_event_loop().create_task(_poll())

    try:
        await wait_for(done.wait(), timeout=_TIMEOUT)
    except AsyncTimeout:
        _pending.pop(state, None)
        await send_message(message, f"{h}\n┃\n┖ <b>Timed Out!</b>")

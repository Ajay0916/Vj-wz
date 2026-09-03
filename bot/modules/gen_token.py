"""Generate token.pickle via /token command in bot.
Callback flow — bot's stream server captures Google OAuth redirect."""
import asyncio
import os
import secrets
from pickle import dump as pdump, load as pload

from pyrogram.enums import ChatType
from pyrogram.filters import create, user, text, private, document
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from ..core.tg_client import TgClient
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message, delete_message

_STOP = "gentoken_stop"
_TIMEOUT = 300
SCOPES = ["https://www.googleapis.com/auth/drive"]
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


def _callback_url():
    base = (Config.BASE_URL or "").rstrip("/")
    return f"{base}/token_callback"


async def token_callback_handler(request):
    """Web callback — Google redirects here after auth.
    Registered in stream_server.py build_app()."""
    from aiohttp import web
    params = request.query
    code = params.get("code")
    state = params.get("state")
    error = params.get("error")

    if error:
        return web.Response(
            text=f"<h2>Error: {error}</h2><p>Close and try /token again.</p>",
            content_type="text/html")

    if not code or state not in _pending:
        return web.Response(
            text="<h2>Invalid request</h2>", content_type="text/html")

    entry = _pending[state]
    entry["code"] = code
    entry["event"].set()

    return web.Response(
        text="<h2>✅ Token saved! You can close this tab.</h2>",
        content_type="text/html")


async def _wait_for_callback(state, timeout=_TIMEOUT):
    """Wait for Google to redirect back with auth code."""
    entry = _pending.get(state)
    if not entry:
        return None
    try:
        await asyncio.wait_for(entry["event"].wait(), timeout)
        return entry.get("code")
    except asyncio.TimeoutError:
        return None


def _clean_files():
    for f in (TOKEN_FILE, CREDENTIALS_FILE):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


@new_task
async def gen_gdrive_token(_, message):
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    h = _header(user_name)
    btns = _stop_btns()

    callback_url = _callback_url()
    if not callback_url:
        return await send_message(
            message, f"{h}\n┃\n┖ <b>BASE_URL not set!</b>")

    # Step 1: Clean old files, ask for credentials.json
    _clean_files()

    await send_message(
        message,
        f"{h}\n┃\n"
        f"┠  <b>Setup (one-time):</b>\n"
        f"┠  1. Open <a href='https://console.cloud.google.com/apis/credentials'>Google Cloud Console</a>\n"
        f"┠  2. Click your OAuth Client ID (Desktop type)\n"
        f"┠  3. In <b>Authorized redirect URIs</b> add:\n"
        f"┠  <code>{callback_url}</code>\n"
        f"┠  4. Click <b>Save</b>\n"
        f"┠\n"
        f"┠  <b>Then send credentials.json file here</b>\n"
        f"┖  <i>Timeout: {_TIMEOUT}s</i>",
        btns,
    )

    # Wait for credentials.json upload
    doc_event = asyncio.Event()
    doc_result = [None]

    async def _on_doc(_, msg):
        if msg.document and msg.document.file_name == "credentials.json":
            doc_result[0] = msg
            doc_event.set()

    async def _on_stop(_, q):
        await q.answer()
        doc_result[0] = "stop"
        doc_event.set()

    h1 = TgClient.bot.add_handler(
        MessageHandler(_on_doc, filters=user(user_id) & document & private), group=-1)
    h2 = TgClient.bot.add_handler(
        CallbackQueryHandler(_on_stop, filters=_stop_filter(user_id)), group=-1)
    try:
        await asyncio.wait_for(doc_event.wait(), _TIMEOUT)
    except asyncio.TimeoutError:
        doc_result[0] = None
    finally:
        TgClient.bot.remove_handler(*h1)
        TgClient.bot.remove_handler(*h2)

    if doc_result[0] is None:
        return await send_message(message, f"{h}\n┃\n┖ <b>Timed Out!</b>")
    if doc_result[0] == "stop":
        return await send_message(message, f"{h}\n┃\n┖ <b>Cancelled.</b>")

    # Download credentials.json
    try:
        await TgClient.bot.download_media(doc_result[0], file_name=CREDENTIALS_FILE)
    except Exception as e:
        return await send_message(
            message, f"{h}\n┃\n┖ <b>Download failed:</b> <i>{e}</i>")

    # Step 2: Build auth URL with callback redirect
    from google_auth_oauthlib.flow import Flow

    try:
        flow = Flow.from_client_secrets_file(
            CREDENTIALS_FILE, scopes=SCOPES,
            redirect_uri=callback_url,
        )
    except Exception as e:
        _clean_files()
        return await send_message(
            message, f"{h}\n┃\n┖ <b>Invalid credentials.json:</b> <i>{e}</i>")

    state = secrets.token_urlsafe(16)
    _pending[state] = {"event": asyncio.Event(), "code": None, "user_id": user_id}

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )

    await send_message(
        message,
        f"{h}\n┃\n"
        f"┠  <b>Step 1:</b> Open link below in browser\n"
        f"┠  <b>Step 2:</b> Login → Allow access\n"
        f"┠  <b>Step 3:</b> You'll see \"Token saved!\"\n"
        f"┖  <a href='{auth_url}'>🔐 Authorize Google Drive</a>",
        btns,
    )

    # Step 3: Wait for callback (stream_server handles /token_callback)
    code = await _wait_for_callback(state, _TIMEOUT)
    _pending.pop(state, None)

    if code is None:
        _clean_files()
        return await send_message(message, f"{h}\n┃\n┖ <b>Timed Out! No callback received.</b>")

    # Step 4: Exchange code for token
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        with open(TOKEN_FILE, "wb") as f:
            pdump(creds, f)
        _clean_files()
        await send_message(
            message,
            f"{h}\n┃\n"
            "┠  <b>token.pickle generated!</b>\n"
            "┠  Set: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
            "┖  <code>/restart</code> to apply",
        )
    except Exception as e:
        _clean_files()
        err = str(e)
        hint = ""
        if "redirect_uri" in err or "invalid_request" in err:
            hint = (
                "\n┠  <i>Redirect URI mismatch.</i>"
                "\n┠  <i>Make sure you added this EXACT URL in Google Cloud Console:</i>"
                f"\n┠  <code>{callback_url}</code>"
            )
        await send_message(
            message, f"{h}\n┃\n┖ <b>Error:</b> <i>{err}</i>{hint}")

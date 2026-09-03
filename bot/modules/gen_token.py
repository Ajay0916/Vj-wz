"""Generate token.pickle for Google Drive via /token command.

Flow:
1. /token → bot sends Google auth link
2. User clicks → authorizes → redirects to bot callback
3. Bot auto-saves token.pickle
"""
import os
import secrets
from asyncio import Event, wait_for, TimeoutError as AsyncTimeout
from urllib.parse import urlencode, urlparse, parse_qs

from aiohttp import web

from pyrogram.enums import ChatType
from pyrogram.filters import create, user, text, private
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from ..core.tg_client import TgClient
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_time
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    send_message,
    edit_message,
)

_STOP = "gentoken_stop"
_TIMEOUT = 300
SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = "/usr/src/app/credentials.json"
TOKEN_FILE = "/usr/src/app/token.pickle"
_state_store = {}


def _stop_filter(uid):
    async def _check(_, __, update):
        return update.data == _STOP and update.from_user.id == uid
    return create(_check)


def _stop_btns():
    btns = ButtonMaker()
    btns.data_button("Cancel", data=_STOP)
    return btns.build_menu(1)


def _header(user_name):
    return (
        "⌬ <u><i><b>Google Drive Token Generator</b></i></u>\n│\n"
        f"│ <b>User</b> → <b>{user_name}</b>"
    )


def _get_callback_url():
    base = Config.BASE_URL or ""
    if not base:
        return None
    # Ensure no trailing slash
    base = base.rstrip("/")
    return f"{base}/token_callback"


async def _token_callback_handler(request):
    """Handle Google OAuth callback → save token → notify user."""
    params = parse_qs(request.query_string)
    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    error = params.get("error", [None])[0]

    if error:
        return web.Response(
            text=f"<h2>❌ Authorization Failed</h2><p>{error}</p><p>Close this tab and try /token again.</p>",
            content_type="text/html",
        )

    if not code or state not in _state_store:
        return web.Response(
            text="<h2>❌ Invalid request</h2><p>Close this tab.</p>",
            content_type="text/html",
        )

    user_id = _state_store.pop(state)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        flow.fetch_token(code=code)
        creds = flow.credentials

        from pickle import dump as pdump
        with open(TOKEN_FILE, "wb") as f:
            pdump(creds, f)

        # Notify user
        await TgClient.bot.send_message(
            user_id,
            "✅ <b>token.pickle generated successfully!</b>\n\n"
            "Set config: <code>USE_SERVICE_ACCOUNTS=False</code>\n"
            "Then <code>/restart</code>",
        )
    except Exception as e:
        await TgClient.bot.send_message(
            user_id,
            f"❌ Token exchange failed: <code>{e}</code>",
        )

    return web.Response(
        text="<h2>✅ token.pickle saved!</h2><p>You can close this tab.</p>",
        content_type="text/html",
    )




@new_task
async def gen_gdrive_token(_, message):
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    btns = _stop_btns()
    h = _header(user_name)


    # Check credentials.json
    if not os.path.exists(CREDENTIALS_FILE):
        await send_message(
            message,
            f"{h}\n┃\n"
            "┖ <b>credentials.json not found!</b>\n\n"
            "Send your <code>credentials.json</code> file here first.",
        )
        return

    # Check if token already valid
    if os.path.exists(TOKEN_FILE):
        try:
            from pickle import load as pload
            with open(TOKEN_FILE, "rb") as f:
                creds = pload(f)
            if creds and creds.valid:
                await send_message(
                    message,
                    f"{h}\n┃\n"
                    "┖ <b>token.pickle already valid!</b>\n"
                    "Remove it first to regenerate.",
                )
                return
        except Exception:
            pass

    callback_url = _get_callback_url()
    if not callback_url:
        await send_message(
            message,
            f"{h}\n┃\n"
            "┖ <b>BASE_URL not set!</b>\n"
            "Set <code>BASE_URL</code> in config first.",
        )
        return

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        await send_message(message, f"{h}\n┃\n┖ <b>google-auth-oauthlib not installed.</b>")
        return

    # Generate state token for CSRF protection
    state = secrets.token_urlsafe(32)
    _state_store[state] = user_id

    try:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            state=state,
        )
    except Exception as e:
        await send_message(message, f"{h}\n┃\n┖ <b>Error:</b> <i>{e}</i>")
        return

    await send_message(
        message,
        f"{h}\n┃\n"
        "┠ <b>Step 1:</b> Click the link below:\n"
        f"┖ <a href='{auth_url}'>🔐 Authorize Google Drive</a>\n\n"
        "┃ <b>Step 2:</b> Login → Allow\n"
        "┃ Token will be saved automatically.\n\n"
        f"<i>Timeout: {get_readable_time(_TIMEOUT)}</i>",
        btns,
    )

    # Wait for callback (or timeout)
    event = Event()

    async def _check_done():
        while state in _state_store:
            await __import__("asyncio").sleep(1)
        event.set()

    try:
        await __import__("asyncio").create_task(_check_done())
        await wait_for(event.wait(), timeout=_TIMEOUT)
    except AsyncTimeout:
        _state_store.pop(state, None)
        await send_message(
            message,
            f"{h}\n┃\n┖ <b>Timed Out!</b>\n┖ <i>Process Stopped.</i>",
        )

from asyncio import (
    gather,
    sleep,
)
from ast import literal_eval
from pyrogram.enums import ButtonStyle
from functools import partial
from io import BytesIO
from os import getcwd, getenv
from shlex import quote as shlex_quote
from time import time

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from aiofiles.os import remove, rename
from aioshutil import rmtree
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler

from .. import (
    LOGGER,
    aria2_options,
    bot_loop,
    categories_dict,
    drives_ids,
    drives_names,
    index_urls,
    intervals,
    jd_listener_lock,
    nzb_options,
    qbit_options,
    sabnzbd_client,
    scheduler,
    task_dict,
    shortener_dict,
    excluded_extensions,
    auth_chats,
    sudo_users,
    var_list,
)
from ..helper.ext_utils.bot_utils import (
    SetInterval,
    cmd_exec,
    new_task,
)
from ..core.config_manager import Config
from ..core.tg_client import TgClient, db_partition_id
from ..core.torrent_manager import TorrentManager
from ..core.startup import update_qb_options, update_nzb_options, update_variables
from ..helper.ext_utils.db_handler import database
from ..core.jdownloader_booter import jdownloader
from ..helper.ext_utils.task_manager import start_from_queued
from ..helper.mirror_leech_utils.rclone_utils.serve import rclone_serve_booter
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
    update_status_message,
)
from .rss import add_job
from .search import initiate_search_tools

start = 0
state = "view"
handler_dict = {}
DEFAULT_VALUES = {
    "LEECH_SPLIT_SIZE": TgClient.MAX_SPLIT_SIZE,
    "RSS_DELAY": 600,
    "STATUS_UPDATE_INTERVAL": 15,
    "SEARCH_LIMIT": 0,
    "UPSTREAM_BRANCH": "master",
    "DEFAULT_UPLOAD": "rc",
    "BOT_MAX_TASKS": 0,
    "QUEUE_ALL": 0,
    "QUEUE_DOWNLOAD": 0,
    "QUEUE_UPLOAD": 0,
    "USER_MAX_TASKS": 0,
}

BOOL_VARS = [
    "AS_DOCUMENT",
    "BOT_PM",
    "COLORED_BTNS",
    "DELETE_LINKS",
    "DRIVE_CATEGORY_MODE",
    "DISABLE_BULK",
    "DISABLE_FF_MODE",
    "DISABLE_JD",
    "DISABLE_LEECH",
    "DISABLE_MIRROR",
    "DISABLE_MULTI",
    "DISABLE_NZB",
    "DISABLE_RSS",
    "DISABLE_SEARCH",
    "DISABLE_SEED",
    "DISABLE_STREAM",
    "DISABLE_TORRENTS",
    "DISABLE_YTDLP",
    "DISABLE_MEGA",
    "EQUAL_SPLITS",
    "INC_TASK_NOTIFY",
    "INC_TASK_RESUME",
    "IS_TEAM_DRIVE",
    "MEDIA_GROUP",
    "MEDIA_STORE",
    "SET_COMMANDS",
    "SHOW_CLOUD_LINK",
    "STOP_DUPLICATE",
    "USE_IMAGES",
    "USE_SERVICE_ACCOUNTS",
    "WEB_PINCODE",
]
# Config vars with preset choice buttons (rendered directly like BOOL_VARS).
CHOICE_VARS = {
    "SEARCH_RESULT_HOST": [("Telegraph", "telegraph"), ("Rentry", "rentry")],
    "STATUS_THEME": [("VJ (Flat)", "vj"), ("WZML (Tree)", "wzml")],
    "WZML_PROGRESS_STYLE": [("1 ■ Default", 1), ("2 ● Dots", 2), ("3 ⬤ Circles", 3), ("4 ∿ Waves", 4), ("5 ★ Stars", 5), ("6 ♥ Hearts", 6), ("7 ░ Shade", 7), ("8 ♪ Music", 8), ("9 ✿ Flowers", 9), ("10 ⠿ Braille", 10)],
}

NEW_ONOFF_VARS = [
    "DISABLE_IMAGES",
    "DISABLE_UPHOSTER",
    "DISABLE_PLUGINS",
    "DISABLE_SHELL",
    "DISABLE_IMDB",
    "DISABLE_MEDIAINFO",
    "DISABLE_LIMITS",
    "DISABLE_QUEUE",
    "DISABLE_RCLONE",
    "DISABLE_GOOGLE",
    "DISABLE_API",
    "DISABLE_SESSION",
    "DISABLE_HELPER",
    "DISABLE_HYPER",
    "DISABLE_THEMES",
]


DEFAULT_DESP = {
    "AS_DOCUMENT": "Send files as document instead of media. Default: False.",
    "DISABLE_THEMES": "Disable theme system entirely. Default: False.",
    "STATUS_THEME": "Status message theme: VJ (Flat) or WZML (Tree). Default: VJ.",
    "WZML_PROGRESS_STYLE": "Progress bar style (1-10). Works for both VJ and WZML themes. Default: 1.",
    "API_PIN": "API PIN for search API auth (X-API-Pin header). Empty = no auth.",
    "SEARCH_RESULT_HOST": "Search results publish destination: telegraph | rentry.",

    "AUTHORIZED_CHATS": "User/Chat IDs authorized to use the bot. Space-separated. Supports thread IDs with | separator.",
    "BASE_URL": "Public URL for torrent web file selection. Format: http://ip or http://ip:port.",
    "BOT_TOKEN": "Telegram Bot Token from @BotFather.",
    "HELPER_TOKENS": "Additional bot tokens for parallel task handling.",
    "STREAM_TOKENS": "Bot tokens dedicated to /stream and /dl. If set, streaming uses these and is isolated from mirror/leech load. Falls back to HELPER_TOKENS.",
    "BOT_MAX_TASKS": "Max tasks (including queued) the bot runs in parallel. 0 = unlimited.",
    "BOT_PM": "Send files/links to bot owner PM. Default: False.",
    "CMD_SUFFIX": "Text appended to all bot commands. Useful for running multiple bot instances.",
    "COLORED_BTNS": "Use colored inline buttons. Default: False.",
    "DEFAULT_LANG": "Default bot language code. Default: en.",
    "DATABASE_URL": "MongoDB connection string for persistent storage.",
    "DEFAULT_UPLOAD": "Default upload destination: gd (Google Drive) or rc (rclone). Default: rc.",
    "DELETE_LINKS": "Auto-delete source links/messages on task start. Default: False.",
    "DEBRID_LINK_API": "Debrid-link.com API key for premium hoster support.",
    "ALLDEBRID_API_KEY": "AllDebrid API key, used by the -ad flag to unlock links/magnets.",
    "DISABLE_TORRENTS": "Disable all torrent downloads. Default: False.",
    "DISABLE_LEECH": "Disable all leech (download to Telegram) tasks. Default: False.",
    "DISABLE_MIRROR": "Disable all mirror (upload to cloud) tasks. Default: False.",
    "DISABLE_BULK": "Disable bulk (zip/unzip) operations. Default: False.",
    "DISABLE_MULTI": "Disable multi-part splits. Default: False.",
    "DISABLE_SEED": "Disable seeding after torrent download. Default: False.",
    "DISABLE_FF_MODE": "Disable FFmpeg processing mode. Default: False.",
    "DISABLE_MEGA": "Disable Mega Processor for bot. Default: False.",
    "DISABLE_JD": "Disable JDownloader downloads. Saves ~256-500MB RAM. Default: False.",
    "DISABLE_NZB": "Disable SABnzbd/Usenet downloads. Saves ~100-200MB RAM. Default: False.",
    "DISABLE_RSS": "Disable RSS feed monitoring. Saves CPU cycles. Default: False.",
    "DISABLE_SEARCH": "Disable torrent search plugins. Saves network I/O. Default: False.",
    "DISABLE_STREAM": "Disable streaming. Stops /stream and the stream server. Default: False.",
    "DISABLE_YTDLP": "Disable YouTube/YT-DLP downloads. Default: False.",
    "EQUAL_SPLITS": "Split files into equal parts of LEECH_SPLIT_SIZE. Default: False.",
    "EXCLUDED_EXTENSIONS": "File extensions to exclude from upload/clone. Space-separated.",
    "FFMPEG_CMDS": "Custom FFmpeg command presets. Dict format.",
    "FILELION_API": "FileLion.cc API key for direct download support.",
    "MEDIA_STORE": "Store media metadata for re-upload. Default: True.",
    "FORCE_SUB_IDS": "Channel/Group IDs for force subscription. Space-separated.",
    "GOFILE_API": "Gofile.io API token for file uploads.",
    "GOFILE_FOLDER_ID": "Gofile.io folder ID for uploads.",
    "PIXELDRAIN_KEY": "PixelDrain API key for uploads.",
    "PROTECTED_API": "ProtectedFiles.cc API key.",
    "BUZZHEAVIER_API": "BuzzHeavier API key for uploads.",
    "DEVUPLOADS_KEY": "DevUploads API key.",
    "DEVUPLOADS_FOLDER": "DevUploads folder ID.",
    "VIKINGFILE_HASH": "VikingFile.to hash for uploads.",
    "VIKINGFILE_FOLDER": "VikingFile.to folder ID.",
    "GDRIVE_ID": "Google Drive folder/TeamDrive ID for uploads.",
    "GD_DESP": "Description for Google Drive uploads. Default: Uploaded with WZ Bot.",
    "AUTHOR_NAME": "Author name shown on Telegraph pages.",
    "AUTHOR_URL": "Author URL for Telegraph pages. Use channel URL for join button.",
    "INSTADL_API": "Instagram downloader API key.",
    "IMDB_TEMPLATE": "Optional HTML template for IMDB results. If empty, uses Rich Messages.",
    "IMAGES": "List of image URLs or file_ids for the gallery. Managed via /addimage command.",
    "IMG_SEARCH": "Comma-separated keywords to auto-fetch wallpaper images on startup. e.g. anime, nature, space",
    "IMG_PAGE": "Number of pages to search for each keyword in IMG_SEARCH. Each page has ~70 images. Default: 1",
    "USE_IMAGES": "Enable random photo backgrounds on bot messages. Requires IMAGES list. Default: False",
    "IMG_SOURCES": "List of image sources to fetch from. Options: wallpaperflare, peapix, wallhaven. Default: wallpaperflare",
    "INC_TASK_NOTIFY": "Notify about incomplete tasks after restart. Default: False.",
    "INC_TASK_RESUME": "Auto-resume incomplete tasks on restart. Default: False.",
    "INDEX_URL": "Google Drive Index URL for direct links.",
    "IS_TEAM_DRIVE": "Set True for TeamDrive uploads. Default: False.",
    "JD_EMAIL": "JDownloader account email for premium downloads.",
    "JD_PASS": "JDownloader account password.",
    "MEGA_EMAIL": "Mega.nz account email for premium.",
    "MEGA_PASSWORD": "Mega.nz account password.",
    "DIRECT_LIMIT": "Direct link download size limit in GB. 0 = unlimited.",
    "MEGA_LIMIT": "Mega download size limit in GB. 0 = unlimited.",
    "TORRENT_LIMIT": "Torrent download size limit in GB. 0 = unlimited.",
    "GD_DL_LIMIT": "Google Drive download size limit in GB. 0 = unlimited.",
    "RC_DL_LIMIT": "Rclone download size limit in GB. 0 = unlimited.",
    "CLONE_LIMIT": "Google Drive clone size limit in GB. 0 = unlimited.",
    "JD_LIMIT": "JDownloader download size limit in GB. 0 = unlimited.",
    "NZB_LIMIT": "Usenet download size limit in GB. 0 = unlimited.",
    "YTDLP_LIMIT": "yt-dlp download size limit in GB. 0 = unlimited.",
    "PLAYLIST_LIMIT": "Max items to download from a playlist. 0 = unlimited.",
    "LEECH_LIMIT": "Leech (Telegram upload) size limit in GB. 0 = unlimited.",
    "EXTRACT_LIMIT": "Extracted file size limit in GB. 0 = unlimited.",
    "ARCHIVE_LIMIT": "Archive (zip) size limit in GB. 0 = unlimited.",
    "STORAGE_LIMIT": "Minimum free storage to maintain in GB. Downloads cancelled if exceeded.",
    "LEECH_DUMP_CHAT": "Chat ID (integer) to dump all leeched files. Leave empty to disable.",
    "LINKS_LOG_ID": "Chat ID for link logging.",
    "MIRROR_LOG_ID": "Chat ID(s) for mirror logs. Space-separated for multiple.",
    "LEECH_PREFIX": "Prefix added to leeched file names.",
    "LEECH_CAPTION": "Custom caption for leeched files. Supports HTML.",
    "LEECH_SUFFIX": "Suffix added to leeched file names.",
    "LEECH_FONT": "Font style for captions: b, i, u, s, code, spoiler.",
    "LEECH_SPLIT_SIZE": "Split size for Telegram uploads in bytes. Default: 2GB (4GB for premium).",
    "MEDIA_GROUP": "Upload split parts as media group. Default: False.",
    "USE_HYPER": "Enable HyperDL/HyperUP for faster Telegram transfers. Default: True.",
    "HYPER_THREADS": "Number of parallel download parts (clients). 0 = auto.",
    "HYPER_PIPELINE": "Concurrent GetFile requests per HyperDL part. Default: 4.",
    "HYPER_CHUNK": "HyperDL working chunk size in bytes. Default: 512 * 1024 (512KB).",
    "STREAM_PIPELINE": "Concurrent GetFile requests for /dl downloads. Default: 8.",
    "STREAM_CHUNK": "Streaming chunk size in bytes, capped at 1 MiB. Default: 1048576.",
    "STREAM_PER_CLIENT": "Concurrent playback streams allowed per bot. Raise for more simultaneous viewers, lower if Telegram floods. Default: 6.",
    "STREAM_GATE": "Process-wide ceiling on concurrent GetFile calls. Default: 96.",
    "CPU_LIMIT": "CPU limit percentage for background services (SABnzbd, JDownloader). Default: 20.",
    "THROTTLE_SERVICES": "Pause services during heavy ops (FFmpeg). auto=low-end only, always, never.",
    "HYDRA_IP": "Hydra API IP address for search.",
    "HYDRA_API_KEY": "Hydra API key for search.",
    "NAME_SWAP": "Rename files using pattern. Format: old:new|old2:new2.",
    "OWNER_ID": "Telegram User ID of the bot owner.",
    "QUEUE_ALL": "Max parallel download+upload tasks. 0 = unlimited.",
    "QUEUE_DOWNLOAD": "Max parallel downloading tasks. 0 = unlimited.",
    "QUEUE_UPLOAD": "Max parallel uploading tasks. 0 = unlimited.",
    "RCLONE_FLAGS": "Rclone flags. Format: key:value|key|key:value.",
    "RCLONE_PATH": "Default rclone remote path for uploads.",
    "RCLONE_SERVE_URL": "Public URL for rclone serve. Format: http://ip.",
    "SHOW_CLOUD_LINK": "Show cloud link button on leeched files. Default: True.",
    "RCLONE_SERVE_USER": "Username for rclone serve authentication.",
    "RCLONE_SERVE_PASS": "Password for rclone serve authentication.",
    "RCLONE_SERVE_PORT": "Port for rclone serve. Default: 8081.",
    "RSS_CHAT": "Chat ID for RSS feed notifications.",
    "RSS_DELAY": "RSS feed check interval in seconds. Default: 600.",
    "RSS_SIZE_LIMIT": "RSS download size limit in GB. 0 = unlimited.",
    "SEARCH_API_LINK": "Search API app URL for multi-search.",
    "SEARCH_LIMIT": "Max search results per site. 0 = default API limit.",
    "SEARCH_PLUGINS": "qBittorrent search plugin URLs. List format.",
    "SET_COMMANDS": "Auto-set bot commands on start. Default: True.",
    "STATUS_LIMIT": "Number of status messages to show. Default: 10.",
    "STATUS_UPDATE_INTERVAL": "Status message refresh interval in seconds. Default: 15.",
    "STOP_DUPLICATE": "Stop if file/folder exists in GDrive. Default: False.",
    "STREAMWISH_API": "StreamWish API key for uploads.",
    "SUDO_USERS": "User IDs with sudo access. Space-separated.",
    "TELEGRAM_API": "Telegram API ID from my.telegram.org.",
    "TELEGRAM_HASH": "Telegram API Hash from my.telegram.org.",
    "TG_PROXY": "SOCKS5 proxy for Telegram connection. Format: socks5://user:pass@ip:port.",
    "THUMBNAIL_LAYOUT": "Thumbnail layout for uploads. Format: WxH (e.g., 1280x720).",
    "VERIFY_TIMEOUT": "Verification timeout in seconds. 0 = disabled.",
    "LOGIN_PASS": "Password to skip token system. Leave empty to disable.",
    "TORRENT_TIMEOUT": "Dead torrent timeout in seconds. 0 = disabled.",
    "TIMEZONE": "Timezone for messages. Default: Asia/Kolkata.",
    "USER_MAX_TASKS": "Max concurrent tasks per user. 0 = unlimited.",
    "USER_TIME_INTERVAL": "Cooldown between tasks per user in seconds. 0 = disabled.",
    "UPLOAD_PATHS": "Custom upload paths per extension. Dict format.",
    "UPSTREAM_REPO": "GitHub repo URL for bot updates.",
    "UPSTREAM_BRANCH": "Branch for updates. Default: wzv3.",
    "USENET_SERVERS": "Usenet server configurations. List of dicts.",
    "USER_SESSION_STRING": "Pyrogram session string for user account tasks.",
    "TRANSMISSION_MODE": "Transmission mode: bot, user, or both. Default: both.",
    "USE_SERVICE_ACCOUNTS": "Use Google Service Accounts. Default: False.",
    "WEB_ACCESS_PASSWORD": "Secret for deriving proxy passwords. Set once, use derived passwords in browser. Empty = auto-generated.",
    "WEB_PINCODE": "Ask for pincode in web file selection. Default: True.",
    "YT_DLP_OPTIONS": "Default yt-dlp options. Format: key:value|key:value.",
    "YT_DESP": "Description for YouTube uploads. Default: Uploaded with WZML-X bot.",
    "YT_TAGS": "Tags for YouTube uploads. List format.",
    "YT_CATEGORY_ID": "YouTube video category ID. Default: 22 (People & Blogs).",
    "YT_PRIVACY_STATUS": "YouTube upload privacy: public, unlisted, or private.",
}

PROTECTED_VARS = {
    "TELEGRAM_HASH",
    "TELEGRAM_API",
    "OWNER_ID",
    "BOT_TOKEN",
    "DATABASE_URL",
}
RESTART_VARS = {
    "STREAM_TOKENS",
    "CMD_SUFFIX",
    "OWNER_ID",
    "USER_SESSION_STRING",
    "TELEGRAM_HASH",
    "TELEGRAM_API",
    "BOT_TOKEN",
    "TG_PROXY",
    "AUTHORIZED_CHATS",
    "DATABASE_URL",
}

ONOFF_VARS = [
    "DISABLE_TORRENTS",
    "DISABLE_LEECH",
    "DISABLE_MIRROR",
    "DISABLE_BULK",
    "DISABLE_MULTI",
    "DISABLE_SEED",
    "DISABLE_FF_MODE",
    "DISABLE_MEGA",
    "DISABLE_JD",
    "DISABLE_NZB",
    "DISABLE_RSS",
    "DISABLE_SEARCH",
    "DISABLE_STREAM",
    "DISABLE_YTDLP",
]


async def get_buttons(key=None, edit_type=None, edit_mode=False):
    buttons = ButtonMaker()
    if key is None:
        buttons.data_button("Config Variables", "botset var")
        buttons.data_button("Module Settings", "botset setonoff")
        buttons.data_button("Private Files", "botset private open")
        if not Config.DISABLE_TORRENTS:
            buttons.data_button("Qbit Settings", "botset qbit")
        buttons.data_button("Aria2c Settings", "botset aria")
        if not Config.DISABLE_NZB:
            buttons.data_button("Sabnzbd Settings", "botset nzb")
        if not Config.DISABLE_JD:
            buttons.data_button("JDownloader Sync", "botset syncjd")
        buttons.data_button("Close", "botset close", style=ButtonStyle.DANGER)
        msg = "Bot Settings:"
    elif edit_type is not None:
        if edit_type == "ariavar":
            buttons.data_button("Back", "botset aria", style=ButtonStyle.PRIMARY)
            if key != "newkey":
                buttons.data_button("Empty String", f"botset emptyaria {key}")
            buttons.data_button("Close", "botset close", style=ButtonStyle.DANGER)
            msg = (
                "<i>Send a key with value.</i> Example: <code>https-proxy-user:value</code>\n┖ <b>Time Left :</b> <code>60 sec</code>"
                if key == "newkey"
                else f"<i>Send a valid value for <code>{key}</code>.</i> Current value is <code>{aria2_options[key]}</code>\n┖ <b>Time Left :</b> <code>60 sec</code>"
            )
        elif edit_type == "qbitvar":
            buttons.data_button("Back", "botset qbit", style=ButtonStyle.PRIMARY)
            buttons.data_button("Empty String", f"botset emptyqbit {key}")
            buttons.data_button("Close", "botset close", style=ButtonStyle.DANGER)
            msg = f"<i>Send a valid value for <code>{key}</code>.</i> Current value is <code>{qbit_options[key]}</code>\n┖ <b>Time Left :</b> <code>60 sec</code>"
        elif edit_type == "nzbvar":
            buttons.data_button("Back", "botset nzb", style=ButtonStyle.PRIMARY)
            buttons.data_button("Default", f"botset resetnzb {key}")
            buttons.data_button("Empty String", f"botset emptynzb {key}")
            buttons.data_button("Close", "botset close", style=ButtonStyle.DANGER)
            msg = f"<i>Send a valid value for <code>{key}</code>.</i> Current value is <code>{nzb_options[key]}</code>\nIf the value is list then separate them by space or ,\nExample: <code>.exe,info</code> or <code>.exe .info</code>\n┖ <b>Time Left :</b> <code>60 sec</code>"
        elif edit_type.startswith("nzbsevar"):
            index = 0 if key == "newser" else int(edit_type.replace("nzbsevar", ""))
            buttons.data_button(
                "Back", f"botset nzbser{index}", style=ButtonStyle.PRIMARY
            )
            if key != "newser":
                buttons.data_button("Empty", f"botset emptyserkey {index} {key}")
            buttons.data_button("Close", "botset close", style=ButtonStyle.DANGER)
            if key == "newser":
                msg = "<i>Send one server as dictionary <code>{}</code>, like in config.py without <code>[]</code>.</i>\n┖ <b>Time Left :</b> <code>60 sec</code>"
            else:
                msg = f"<i>Send a valid value for <code>{key}</code> in server <code>{Config.USENET_SERVERS[index]['name']}</code>.</i> Current value is <code>{Config.USENET_SERVERS[index][key]}</code>\n┖ <b>Time Left :</b> <code>60 sec</code>"
        elif edit_type == "editvar":
            msg = f"<b>Variable:</b> <code>{key}</code>\n\n"
            msg += f"<b>Description:</b> {DEFAULT_DESP.get(key, 'No Description Provided')}\n\n"
            value = Config.get(key)
            if value == "":
                value = "None"
            msg += f"<b>Current Value:</b> <code>{value}</code>\n\n"
            buttons.data_button(
                "View Value", f"botset showvar {key}", position="header"
            )
            buttons.data_button("Back", "botset back var", position="footer")
            if key in CHOICE_VARS:
                msg += "<i>Choose a value for the above Var</i>\n\n"
                for label, value in CHOICE_VARS[key]:
                    buttons.data_button(label, f"botset choicevar {key} {value}")
            elif key not in BOOL_VARS:
                if not edit_mode:
                    buttons.data_button(
                        "Edit Value",
                        f"botset editvar {key} edit",
                        style=ButtonStyle.PRIMARY,
                    )
                else:
                    buttons.data_button("Stop Edit", f"botset editvar {key}")
            else:
                msg += "<i>Choose a valid value for the above Var</i>\n\n"
                buttons.data_button("True", f"botset boolvar {key} on")
                buttons.data_button("False", f"botset boolvar {key} off")
            if key not in BOOL_VARS and key not in CHOICE_VARS and key not in PROTECTED_VARS:
                buttons.data_button("Reset", f"botset resetvar {key}")
            buttons.data_button(
                "Close", "botset close", position="footer", style=ButtonStyle.DANGER
            )
            if edit_mode and key in RESTART_VARS:
                msg += (
                    "\n<b>Note:</b> Restart required for this edit to take effect!\n\n"
                )
            if edit_mode and key not in BOOL_VARS and key not in CHOICE_VARS:
                msg += "<i>Send a valid value for the above Var.</i>\n┖ <b>Time Left :</b> <code>60 sec</code>"
    elif key == "var":
        hidden_vars = set()
        if Config.DISABLE_RSS:
            hidden_vars.update(["RSS_CHAT", "RSS_DELAY", "RSS_SIZE_LIMIT"])
        if Config.DISABLE_THEMES:
            hidden_vars.update(["STATUS_THEME", "WZML_PROGRESS_STYLE"])
        if Config.DISABLE_SEARCH:
            hidden_vars.update(
                [
                    "API_PIN",
                    "AUTHOR_NAME",
                    "AUTHOR_URL",
                    "SEARCH_API_LINK",
                    "SEARCH_LIMIT",
                    "SEARCH_PLUGINS",
                    "SEARCH_RESULT_HOST",
                ]
            )
        if Config.DISABLE_JD:
            hidden_vars.update(["JD_EMAIL", "JD_PASS", "JD_LIMIT"])
        if Config.DISABLE_NZB:
            hidden_vars.update(["HYDRA_API_KEY", "HYDRA_IP", "USENET_SERVERS", "NZB_LIMIT"])
        if Config.DISABLE_MEGA:
            hidden_vars.update(["MEGA_EMAIL", "MEGA_LIMIT", "MEGA_PASSWORD"])
        if Config.DISABLE_YTDLP:
            hidden_vars.update(
                [
                    "YT_CATEGORY_ID", "YT_DESP", "YT_DLP_OPTIONS",
                    "YT_PRIVACY_STATUS", "YT_TAGS", "YTDLP_LIMIT", "PLAYLIST_LIMIT",
                ]
            )
        if Config.DISABLE_TORRENTS:
            hidden_vars.update(["TORRENT_LIMIT", "TORRENT_TIMEOUT"])
        if Config.DISABLE_FF_MODE:
            hidden_vars.update(["ARCHIVE_LIMIT", "EXTRACT_LIMIT", "FFMPEG_CMDS"])
        if Config.DISABLE_QUEUE:
            hidden_vars.update(["QUEUE_ALL", "QUEUE_DOWNLOAD", "QUEUE_UPLOAD"])
        if Config.DISABLE_LIMITS:
            hidden_vars.update(
                [
                    "BOT_MAX_TASKS", "QUEUE_ALL", "QUEUE_DOWNLOAD", "QUEUE_UPLOAD",
                    "DIRECT_LIMIT", "TORRENT_LIMIT", "CLONE_LIMIT", "LEECH_LIMIT",
                    "GD_DL_LIMIT", "STORAGE_LIMIT", "MEGA_LIMIT", "LEECH_SPLIT_SIZE",
                    "RC_DL_LIMIT", "TORRENT_TIMEOUT",
                ]
            )
        if Config.DISABLE_RCLONE:
            hidden_vars.update(
                [
                    "RCLONE_PATH", "RCLONE_FLAGS",
                    "RCLONE_SERVE_URL", "RCLONE_SERVE_USER",
                    "RCLONE_SERVE_PASS", "RCLONE_SERVE_PORT",
                    "RC_DL_LIMIT",
                ]
            )
        if Config.DISABLE_LEECH:
            hidden_vars.update(
                [
                    "AS_DOCUMENT", "EQUAL_SPLITS",
                    "LEECH_CAPTION", "LEECH_DUMP_CHAT", "LINKS_LOG_ID",
                    "LEECH_FONT", "LEECH_LIMIT", "LEECH_PREFIX",
                    "LEECH_SPLIT_SIZE", "LEECH_SUFFIX",
                    "MEDIA_GROUP", "MEDIA_STORE", "EXCLUDED_EXTENSIONS",
                ]
            )
        if Config.DISABLE_STREAM:
            hidden_vars.update(["STREAM_TOKENS", "STREAM_PIPELINE", "STREAM_CHUNK", "STREAM_PER_CLIENT", "STREAM_GATE"])
        if Config.DISABLE_IMAGES:
            hidden_vars.update(["IMG_SEARCH", "IMG_PAGE", "IMG_SOURCES", "USE_IMAGES", "IMAGES"])
        if Config.DISABLE_API:
            hidden_vars.update(["STREAMWISH_API", "PROTECTED_API", "DEBRID_LINK_API", "INSTADL_API", "FILELION_API"])
        if Config.DISABLE_HELPER:
            hidden_vars.update(["HELPER_TOKENS", "HELPER_STRINGS", "HELPER_BOT_PROXIES", "HELPER_USER_PROXIES"])
        if Config.DISABLE_HYPER:
            hidden_vars.update(["USE_HYPER", "HYPER_CHUNK", "HYPER_PIPELINE", "HYPER_THREADS", "HYPER_LIMIT"])
        if Config.DISABLE_UPHOSTER:
            hidden_vars.update(
                [
                    "GOFILE_API", "GOFILE_FOLDER_ID", "GOFILE_AUTO_CREATE_FOLDER",
                    "PIXELDRAIN_KEY", "BUZZHEAVIER_API",
                    "DEVUPLOADS_KEY", "DEVUPLOADS_FOLDER",
                    "VIKINGFILE_HASH", "VIKINGFILE_FOLDER",
                ]
            )
        if Config.DISABLE_GOOGLE:
            hidden_vars.update(
                ["GDRIVE_ID", "GD_DESP", "GD_DL_LIMIT", "INDEX_URL",
                 "USE_SERVICE_ACCOUNTS", "DRIVE_CATEGORY_MODE", "DRIVE_CATEGORY_SA",
                 "IS_TEAM_DRIVE", "SHOW_CLOUD_LINK", "STOP_DUPLICATE"]
            )

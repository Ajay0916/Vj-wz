from asyncio import gather, iscoroutinefunction
from html import escape
from pyrogram.enums import ButtonStyle
from re import findall
from time import time

from psutil import cpu_percent, disk_usage, virtual_memory

from ... import (
    DOWNLOAD_DIR,
    bot_cache,
    bot_start_time,
    status_dict,
    task_dict,
    task_dict_lock,
)
from ...core.config_manager import Config
from ..telegram_helper.button_build import ButtonMaker

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


class MirrorStatus:
    STATUS_UPLOAD = "Upload"
    STATUS_DOWNLOAD = "Download"
    STATUS_CLONE = "Clone"
    STATUS_QUEUEDL = "QueueDl"
    STATUS_QUEUEUP = "QueueUp"
    STATUS_PAUSED = "Pause"
    STATUS_ARCHIVE = "Archive"
    STATUS_EXTRACT = "Extract"
    STATUS_SPLIT = "Split"
    STATUS_CHECK = "CheckUp"
    STATUS_SEED = "Seed"
    STATUS_SAMVID = "SamVid"
    STATUS_CONVERT = "Convert"
    STATUS_FFMPEG = "FFmpeg"
    STATUS_YT = "YouTube"
    STATUS_METADATA = "Metadata"
    STATUS_SEEDR = "Seedr"


class EngineStatus:
    def __init__(self):
        ver = bot_cache.get("eng_versions", {})
        self.STATUS_ARIA2 = f"Aria2 v{ver.get('aria2', 'N/A')}"
        self.STATUS_AIOHTTP = f"AioHttp v{ver.get('aiohttp', 'N/A')}"
        self.STATUS_GDAPI = f"Google-API v{ver.get('gapi', 'N/A')}"
        self.STATUS_QBIT = f"qBit v{ver.get('qBittorrent', 'N/A')}"
        self.STATUS_TGRAM = f"WzPyro v{ver.get('wzgram', 'N/A')}"
        self.STATUS_MEGA = f"MegaSDK v{ver.get('mega', 'N/A')}"
        self.STATUS_YTDLP = f"yt-dlp v{ver.get('yt-dlp', 'N/A')}"
        self.STATUS_FFMPEG = f"ffmpeg v{ver.get('ffmpeg', 'N/A')}"
        self.STATUS_7Z = f"7z v{ver.get('7z', 'N/A')}"
        self.STATUS_RCLONE = f"RClone v{ver.get('rclone', 'N/A')}"
        self.STATUS_SABNZBD = f"SABnzbd+ v{ver.get('SABnzbd+', 'N/A')}"
        self.STATUS_QUEUE = "QSystem v2"
        self.STATUS_JD = "JDownloader v2"
        self.STATUS_YT = "Youtube-Api"
        self.STATUS_METADATA = "Metadata"
        self.STATUS_UPHOSTER = "Uphoster"
        self.STATUS_SEEDR = "Seedr"


STATUSES = {
    "ALL": "All",
    "DL": MirrorStatus.STATUS_DOWNLOAD,
    "UP": MirrorStatus.STATUS_UPLOAD,
    "QD": MirrorStatus.STATUS_QUEUEDL,
    "QU": MirrorStatus.STATUS_QUEUEUP,
    "AR": MirrorStatus.STATUS_ARCHIVE,
    "EX": MirrorStatus.STATUS_EXTRACT,
    "SD": MirrorStatus.STATUS_SEED,
    "CL": MirrorStatus.STATUS_CLONE,
    "CM": MirrorStatus.STATUS_CONVERT,
    "SP": MirrorStatus.STATUS_SPLIT,
    "SV": MirrorStatus.STATUS_SAMVID,
    "FF": MirrorStatus.STATUS_FFMPEG,
    "PA": MirrorStatus.STATUS_PAUSED,
    "CK": MirrorStatus.STATUS_CHECK,
}


async def get_task_by_gid(gid: str):
    async with task_dict_lock:
        for tk in task_dict.values():
            if hasattr(tk, "seeding"):
                await tk.update()
            if tk.gid() == gid or tk.gid().startswith(gid):
                return tk
        return None


async def get_specific_tasks(status, user_id):
    if status == "All":
        if user_id:
            return [tk for tk in task_dict.values() if tk.listener.user_id == user_id]
        else:
            return list(task_dict.values())
    tasks_to_check = (
        [tk for tk in task_dict.values() if tk.listener.user_id == user_id]
        if user_id
        else list(task_dict.values())
    )
    coro_tasks = []
    coro_tasks.extend(tk for tk in tasks_to_check if iscoroutinefunction(tk.status))
    coro_statuses = await gather(*[tk.status() for tk in coro_tasks])
    result = []
    coro_index = 0
    for tk in tasks_to_check:
        if tk in coro_tasks:
            st = coro_statuses[coro_index]
            coro_index += 1
        else:
            st = tk.status()
        if (st == status) or (
            status == MirrorStatus.STATUS_DOWNLOAD and st not in STATUSES.values()
        ):
            result.append(tk)
    return result


async def get_all_tasks(req_status: str, user_id):
    async with task_dict_lock:
        return await get_specific_tasks(req_status, user_id)


def get_raw_file_size(size):
    num, unit = size.split()
    return int(float(num) * (1024 ** SIZE_UNITS.index(unit)))


def get_readable_file_size(size_in_bytes):
    if not size_in_bytes:
        return "0B"
    if size_in_bytes < 0:
        return "Unknown"

    index = 0
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1

    return f"{size_in_bytes:.2f}{SIZE_UNITS[index]}"


def get_readable_time(seconds: int):
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    result = ""
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f"{int(period_value)}{period_name}"
    return result


def get_raw_time(time_str: str) -> int:
    time_units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(
        int(value) * time_units[unit]
        for value, unit in findall(r"(\d+)([dhms])", time_str)
    )


def time_to_seconds(time_duration):
    try:
        parts = time_duration.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = map(float, parts)
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = map(float, parts)
        elif len(parts) == 1:
            hours = 0
            minutes = 0
            seconds = float(parts[0])
        else:
            return 0
        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return 0


def speed_string_to_bytes(size_text: str):
    size = 0
    size_text = size_text.lower()
    if "k" in size_text:
        size += float(size_text.split("k")[0]) * 1024
    elif "m" in size_text:
        size += float(size_text.split("m")[0]) * 1048576
    elif "g" in size_text:
        size += float(size_text.split("g")[0]) * 1073741824
    elif "t" in size_text:
        size += float(size_text.split("t")[0]) * 1099511627776
    elif "b" in size_text:
        size += float(size_text.split("b")[0])
    return size


def get_progress_bar_string(pct):
    pct = float(str(pct).strip("%"))
    p = min(max(pct, 0), 100)
    cFull = int(p // 8)
    cPart = int(p % 8 - 1)
    style_id = 1 if Config.DISABLE_THEMES else (Config.WZML_PROGRESS_STYLE or 1)
    if style_id in WZML_PROGRESS_STYLES:
        style = WZML_PROGRESS_STYLES[style_id]
        p_str = style["filled"] * cFull
        if cPart >= 0:
            p_str += style["multi"][min(cPart, len(style["multi"]) - 1)]
        p_str += style["empty"] * (12 - cFull)
    else:
        p_str = "■" * cFull
        if cPart >= 0:
            p_str += ["▤", "▥", "▦", "▧", "▨", "▩", "■"][cPart]
        p_str += "□" * (12 - cFull)
    return f"<code>[{p_str}]</code>"


# ═══════════════════════════════════════════════════════════════════
# WZML Theme — separate functions, does NOT affect VJ code
# ═══════════════════════════════════════════════════════════════════

WZML_MAX = 11

WZML_PROGRESS_STYLES = {
    1: {"name": "Default",   "filled": "\u25a0", "empty": "\u25a1", "multi": ["\u25a4", "\u25a4", "\u25a6", "\u25a6", "\u25a6", "\u25a9", "\u25a9"]},
    2: {"name": "Dots",      "filled": "\u2b24", "empty": "\u25cc", "multi": ["\u25d4", "\u25d4", "\u25d1", "\u25d1", "\u25d1", "\u25d5", "\u25d5"]},
    3: {"name": "Circles",   "filled": "\u2b24", "empty": "\u25cc", "multi": ["\u25cc", "\u25cc", "\u25ce", "\u25ce", "\u25ce", "\u25cd", "\u25cd"]},
    4: {"name": "Waves",     "filled": "\u224b", "empty": "\u223f", "multi": ["\u223f", "\u223c", "\u223c", "\u2248", "\u2248", "\u224b", "\u224b"]},
    5: {"name": "Stars",     "filled": "\u2605", "empty": "\u2606", "multi": ["\u2606", "\u2727", "\u272c", "\u272e", "\u2605", "\u2605", "\u2605"]},
    6: {"name": "Hearts",    "filled": "\u2665\ufe0e", "empty": "\u2661\ufe0e", "multi": ["\u2661\ufe0e", "\u2767\ufe0e", "\u2767\ufe0e", "\u2766\ufe0e", "\u2766\ufe0e", "\u2765\ufe0e", "\u2765\ufe0e"]},
    7: {"name": "Shade",     "filled": "\u2588", "empty": "\u2591", "multi": ["\u2591", "\u2591", "\u2592", "\u2592", "\u2592", "\u2593", "\u2593"]},
    8: {"name": "Music",     "filled": "\u266c", "empty": "\u2669", "multi": ["\u2669", "\u2669", "\u266a", "\u266a", "\u266b", "\u266b", "\u266c"]},
    9: {"name": "Flowers",   "filled": "\u273f", "empty": "\u273d", "multi": ["\u273d", "\u273d", "\u273e", "\u273e", "\u2740", "\u2740", "\u273f"]},
    10: {"name": "Braille",  "filled": "\u283f", "empty": "\u2804", "multi": ["\u2804", "\u2806", "\u2806", "\u280e", "\u280e", "\u283f", "\u283f"]},
}

_WZML_ENGINE_EMOJI = {
    "Aria2": "\U0001f4f6", "AioHttp": "\U0001f310", "Google": "\u267b\ufe0f",
    "qBit": "\U0001f9a0", "WzPyro": "\U0001f4a5", "MegaSDK": "\u2b55",
    "yt-dlp": "\U0001f31f", "ffmpeg": "\u2702\ufe0f", "7z": "\U0001f6e0\ufe0f",
    "RClone": "\U0001f504", "SABnzbd": "\U0001f4e6", "JDownloader": "\U0001f50c",
    "QSystem": "\U0001f504", "Youtube": "\U0001f3ac", "Metadata": "\U0001f3a8",
    "Uphoster": "\u2601\ufe0f",
}


def get_wzml_progress(pct):
    pct = float(str(pct).strip("%"))
    p = min(max(pct, 0), 100)
    cFull = int(p // 8)
    cPart = int(p % 8 - 1)
    style = WZML_PROGRESS_STYLES.get(Config.WZML_PROGRESS_STYLE or 1, WZML_PROGRESS_STYLES[1])
    p_str = style["filled"] * cFull
    if cPart >= 0:
        p_str += style["multi"][min(cPart, len(style["multi"]) - 1)]
    p_str += style["empty"] * (WZML_MAX - cFull)
    return f" <code>\u2827{p_str}\u2839</code>"


def _wzml_engine(engine_str):
    for key, emoji in _WZML_ENGINE_EMOJI.items():
        if key.lower() in engine_str.lower():
            return f"<b>{engine_str} {emoji}</b>"
    return f"<b>{engine_str}</b>\u200b"


async def _get_wzml_readable_message(sid, is_user, page_no=1, status="All", page_step=1):
    from ... import LOGGER
    from ..telegram_helper.bot_commands import BotCommands
    from pyrogram.enums import ChatType
    import traceback as _tb

    try:
        return await _wzml_inner(sid, is_user, page_no, status, page_step)
    except Exception as e:
        LOGGER.error(f"WZML theme error: {e}\n{_tb.format_exc()}")
        return None, None


async def _wzml_inner(sid, is_user, page_no=1, status="All", page_step=1):
    from ..telegram_helper.bot_commands import BotCommands
    from pyrogram.enums import ChatType

    msg = ""
    button = None

    tasks = await get_specific_tasks(status, sid if is_user else None)

    STATUS_LIMIT = int(Config.STATUS_LIMIT or 10)
    page_no = int(page_no or 1)
    page_step = int(page_step or 1)
    tasks_no = len(tasks)
    pages = (max(tasks_no, 1) + STATUS_LIMIT - 1) // STATUS_LIMIT
    if page_no > pages:
        page_no = (page_no - 1) % pages + 1
        status_dict[sid]["page_no"] = page_no
    elif page_no < 1:
        page_no = pages - (abs(page_no) % pages)
        status_dict[sid]["page_no"] = page_no
    start_position = (page_no - 1) * STATUS_LIMIT

    for index, task in enumerate(
        tasks[start_position : STATUS_LIMIT + start_position], start=1
    ):
        try:
            if status != "All":
                tstatus = status
            elif iscoroutinefunction(task.status):
                tstatus = await task.status()
            else:
                tstatus = task.status()

            # Header: \u256d Status: Name
            try:
                msg += f"<b>\u256d <a href='{task.listener.message.link}'>{tstatus}</a>: </b>"
            except Exception:
                msg += f"<b>\u256d {tstatus}: </b>"
            msg += f"<code>{escape(str(task.name()))}</code>"

            # Download / Pause / QueueDl
            if tstatus not in (MirrorStatus.STATUS_SEED, MirrorStatus.STATUS_SPLIT):
                if task.listener.progress:
                    progress = task.progress()
                    msg += f"\n<b>\u251c</b>{get_wzml_progress(progress)} {progress}"
                    msg += f"\n<b>\u251c\U0001f504 Process:</b> {task.processed_bytes()} of {task.size()}"
                    msg += f"\n<b>\u251c\u26a1 Speed:</b> {task.speed()}"
                    elapsed = time() - task.listener.message.date.timestamp()
                    msg += f"\n<b>\u251c\u23f3 ETA:</b> {task.eta()}"
                    msg += f"<b> | Elapsed: </b>{get_readable_time(elapsed)}"
                    msg += f"\n<b>\u251c\u26d3\ufe0f Engine :</b> {_wzml_engine(task.engine)}"
                    if task.listener.is_torrent or task.listener.is_qbit:
                        try:
                            msg += f"\n<b>\u251c\U0001f331 Seeders:</b> {task.seeders_num()} | <b>\U0001f40c Leechers:</b> {task.leechers_num()}"
                        except Exception:
                            pass
                    if task.listener.is_torrent or task.listener.is_qbit or task.listener.is_nzb:
                        msg += f"\n<b>\u251c\U0001f9ff Select:</b> <i>/{BotCommands.SelectCommand[1]}_{task.gid()[:12]}</i>"
                    # Source/User line
                    try:
                        chat = task.listener.message.chat
                        if chat.type != ChatType.PRIVATE:
                            chatid = str(chat.id)[4:]
                            msg += f'\n<b>\u251c\U0001f310 Source: </b><a href="https://t.me/c/{chatid}/{task.listener.message.message_id}">{task.listener.message.from_user.first_name}</a> | <b>Id :</b> <code>{task.listener.message.from_user.id}</code>'
                        else:
                            msg += f'\n<b>\u251c\U0001f464 User:</b> <code>{task.listener.message.from_user.first_name}</code> | <b>Id:</b> <code>{task.listener.message.from_user.id}</code>'
                    except Exception:
                        pass
                    msg += f"\n<b>\u2570\u274c </b><i>/{BotCommands.CancelTaskCommand[1]}_{task.gid()[:12]}</i>"

                elif tstatus == MirrorStatus.STATUS_SEED:
                    msg += f"\n<b>\u251c\U0001f4e6 Size: </b>{task.size()}"
                    msg += f"\n<b>\u251c\u26d3\ufe0f Engine:</b> {_wzml_engine(task.engine)}"
                    msg += f"\n<b>\u251c\u26a1 Speed: </b>{task.seed_speed()}"
                    msg += f"\n<b>\u251c\U0001f53a Uploaded: </b>{task.uploaded_bytes()}"
                    msg += f"\n<b>\u251c\U0001f4ce Ratio: </b>{task.ratio()}"
                    msg += f" | <b>\u23f2\ufe0f Time: </b>{task.seeding_time()}"
                    elapsed = time() - task.listener.message.date.timestamp()
                    msg += f"\n<b>\u251c\u23f3 Elapsed: </b>{get_readable_time(elapsed)}"
                    msg += f"\n<b>\u2570\u274c </b><i>/{BotCommands.CancelTaskCommand[1]}_{task.gid()[:12]}</i>"

                else:
                    msg += f"\n<b>\u251c\u26d3\ufe0f Engine :</b> {_wzml_engine(task.engine)}"
                    msg += f"\n<b>\u2570\U0001f4d0 Size: </b>{task.size()}"

            else:
                # Seeding
                msg += f"\n<b>\u251c\U0001f4e6 Size: </b>{task.size()}"
                msg += f"\n<b>\u251c\u26d3\ufe0f Engine:</b> {_wzml_engine(task.engine)}"
                msg += f"\n<b>\u251c\u26a1 Speed: </b>{task.seed_speed()}"
                msg += f"\n<b>\u251c\U0001f53a Uploaded: </b>{task.uploaded_bytes()}"
                msg += f"\n<b>\u251c\U0001f4ce Ratio: </b>{task.ratio()}"
                msg += f" | <b>\u23f2\ufe0f Time: </b>{task.seeding_time()}"
                elapsed = time() - task.listener.message.date.timestamp()
                msg += f"\n<b>\u251c\u23f3 Elapsed: </b>{get_readable_time(elapsed)}"
                msg += f"\n<b>\u2570\u274c </b><i>/{BotCommands.CancelTaskCommand[1]}_{task.gid()[:12]}</i>"

            msg += "\n<b>_________________________________</b>\n\n"

        except Exception:
            # If this task crashes, skip it and continue with others
            continue

    if len(msg) == 0:
        if status == "All":
            return None, None
        else:
            msg = f"No Active {status} Tasks!\n\n"

    # DL/UL speed (no lock — already held by caller send_status_message)
    dl_speed = 0
    up_speed = 0
    for tk in task_dict.values():
        try:
            spd = tk.speed()
            spd_bytes = speed_string_to_bytes(spd)
            if iscoroutinefunction(tk.status):
                st = await tk.status()
            else:
                st = tk.status()
            if st in (MirrorStatus.STATUS_DOWNLOAD, MirrorStatus.STATUS_QUEUEDL, MirrorStatus.STATUS_PAUSED):
                dl_speed += spd_bytes
            elif st in (MirrorStatus.STATUS_UPLOAD, MirrorStatus.STATUS_SEED):
                up_speed += spd_bytes
        except Exception:
            pass

    bmsg = f"<b>\U0001f5a5 CPU:</b> {cpu_percent()}% | <b>\U0001f4bf FREE:</b> {get_readable_file_size(disk_usage(DOWNLOAD_DIR).free)}"
    bmsg += f"\n<b>\U0001f3ae RAM:</b> {virtual_memory().percent}% | <b>\U0001f7e2 UPTIME:</b> {get_readable_time(time() - bot_start_time)}"
    bmsg += f"\n<b>\U0001f53b DL:</b> {get_readable_file_size(dl_speed)}/s | <b>\U0001f53a UL:</b> {get_readable_file_size(up_speed)}/s"

    # Buttons (3-col, WZML style)
    buttons = ButtonMaker()
    if not is_user:
        buttons.data_button("📊 Statistics", f"status {sid} stats", style=ButtonStyle.PRIMARY)
    if len(tasks) > STATUS_LIMIT:
        msg += f"<b>Tasks:</b> {tasks_no} | <b>Page:</b> {page_no}/{pages}\n"
        buttons.data_button("\u23ea Previous", f"status {sid} pre")
        buttons.data_button(f"{page_no}/{pages}", f"status {sid} ref")
        buttons.data_button("Next \u23e9", f"status {sid} nex")
        buttons.data_button("📊 Statistics", f"status {sid} stats", style=ButtonStyle.PRIMARY)
    buttons.data_button("\u267b\ufe0f Refresh", f"status {sid} ref", style=ButtonStyle.PRIMARY)
    buttons.data_button("\u274c Close", f"status {sid} close")
    button = buttons.build_menu(3)

    return msg + bmsg, button

async def get_readable_message(sid, is_user, page_no=1, status="All", page_step=1):
    if not Config.DISABLE_THEMES and (Config.get("STATUS_THEME") or "vj") == "wzml":
        return await _get_wzml_readable_message(sid, is_user, page_no, status, page_step)
    msg = ""
    button = None

    tasks = await get_specific_tasks(status, sid if is_user else None)

    STATUS_LIMIT = Config.STATUS_LIMIT
    tasks_no = len(tasks)
    pages = (max(tasks_no, 1) + STATUS_LIMIT - 1) // STATUS_LIMIT
    if page_no > pages:
        page_no = (page_no - 1) % pages + 1
        status_dict[sid]["page_no"] = page_no
    elif page_no < 1:
        page_no = pages - (abs(page_no) % pages)
        status_dict[sid]["page_no"] = page_no
    start_position = (page_no - 1) * STATUS_LIMIT

    for index, task in enumerate(
        tasks[start_position : STATUS_LIMIT + start_position], start=1
    ):
        if status != "All":
            tstatus = status
        elif iscoroutinefunction(task.status):
            tstatus = await task.status()
        else:
            tstatus = task.status()
        msg += f"<b>{index + start_position}.</b> "
        msg += f"<b><i>{escape(f'{task.name()}')}</i></b>"
        if task.listener.subname:
            msg += f"\n┖ <b>Sub Name</b> → <i>{task.listener.subname}</i>"
        elapsed = time() - task.listener.message.date.timestamp()

        msg += f"\n\n<b>Task By {task.listener.message.from_user.mention(style='html')} </b> ( #ID{task.listener.message.from_user.id} )"
        if task.listener.is_super_chat:
            msg += f" <i>[<a href='{task.listener.message.link}'>Link</a>]</i>"

        if (
            tstatus not in [MirrorStatus.STATUS_SEED, MirrorStatus.STATUS_QUEUEUP]
            and task.listener.progress
        ):
            progress = task.progress()
            msg += f"\n┟ {get_progress_bar_string(progress)} <i>{progress}</i>"
            if task.listener.subname:
                subsize = f" / {get_readable_file_size(task.listener.subsize)}"
                ac = len(task.listener.files_to_proceed)
                count = f"( {task.listener.proceed_count} / {ac or '?'} )"
            else:
                subsize = ""
                count = ""
            msg += f"\n┠ <b>Processed</b> → <i>{task.processed_bytes()}{subsize} of {task.size()}</i>"
            if count:
                msg += f"\n┠ <b>Count:</b> → <b>{count}</b>"
            msg += f"\n┠ <b>Status</b> → <b>{tstatus}</b>"
            msg += f"\n┠ <b>Speed</b> → <i>{task.speed()}</i>"
            msg += f"\n┠ <b>Time</b> → <i>{task.eta()} of {get_readable_time(elapsed + get_raw_time(task.eta()))} ( {get_readable_time(elapsed)} )</i>"
            if tstatus == MirrorStatus.STATUS_DOWNLOAD and (
                task.listener.is_torrent or task.listener.is_qbit
            ):
                try:
                    msg += f"\n┠ <b>Seeders</b> → {task.seeders_num()} | <b>Leechers</b> → {task.leechers_num()}"
                except Exception:
                    pass
            # TODO: Add Connected Peers
        elif tstatus == MirrorStatus.STATUS_SEED:
            msg += f"\n┠ <b>Size</b> → <i>{task.size()}</i> | <b>Uploaded</b>  → <i>{task.uploaded_bytes()}</i>"
            msg += f"\n┠ <b>Status</b> → <b>{tstatus}</b>"
            msg += f"\n┠ <b>Speed</b> → <i>{task.seed_speed()}</i>"
            msg += f"\n┠ <b>Ratio</b> → <i>{task.ratio()}</i>"
            msg += f"\n┠ <b>Time</b> → <i>{task.seeding_time()}</i> | <b>Elapsed</b> → <i>{get_readable_time(elapsed)}</i>"
        else:
            msg += f"\n┠ <b>Size</b> → <i>{task.size()}</i>"
        msg += f"\n┠ <b>Engine</b> → <i>{task.engine}</i>"
        msg += f"\n┠ <b>In Mode</b> → <i>{task.listener.mode[0]}</i>"
        msg += f"\n┠ <b>Out Mode</b> → <i>{task.listener.mode[1]}</i>"
        from ..telegram_helper.bot_commands import BotCommands

        if tstatus in [
            MirrorStatus.STATUS_DOWNLOAD,
            MirrorStatus.STATUS_PAUSED,
            MirrorStatus.STATUS_QUEUEDL,
        ]:
            if (
                task.listener.is_torrent
                or task.listener.is_qbit
                or task.listener.is_nzb
            ):
                msg += f"\n┠ <b>Select</b> → /{BotCommands.SelectCommand[1]}_{task.gid()[:8]}"

        msg += f"\n<b>┖ Stop</b> → <i>/{BotCommands.CancelTaskCommand[1]}_{task.gid()[:8]}</i>\n\n"

    if len(msg) == 0:
        if status == "All":
            return None, None
        else:
            msg = f"No Active {status} Tasks!\n\n"

    msg += "⌬ <b><u>Bot Stats</u></b>"
    buttons = ButtonMaker()
    if not is_user:
        buttons.data_button(
            "📜 TStats",
            f"status {sid} ov",
            position="header",
            style=ButtonStyle.PRIMARY,
        )
    if len(tasks) > STATUS_LIMIT:
        msg += f"<b>Page:</b> {page_no}/{pages} | <b>Tasks:</b> {tasks_no} | <b>Step:</b> {page_step}\n"
        buttons.data_button("<<", f"status {sid} pre", position="header")
        buttons.data_button(">>", f"status {sid} nex", position="header")
        if tasks_no > 30:
            for i in [1, 2, 4, 6, 8, 10, 15]:
                buttons.data_button(i, f"status {sid} ps {i}", position="footer")
    if status != "All" or tasks_no > 20:
        for label, status_value in list(STATUSES.items()):
            if status_value != status:
                buttons.data_button(label, f"status {sid} st {status_value}")
    buttons.data_button(
        "♻️ Refresh", f"status {sid} ref", position="header", style=ButtonStyle.PRIMARY
    )
    button = buttons.build_menu(8)
    msg += f"\n┟ <b>CPU</b> → {cpu_percent()}% | <b>F</b> → {get_readable_file_size(disk_usage(DOWNLOAD_DIR).free)} [{round(100 - disk_usage(DOWNLOAD_DIR).percent, 1)}%]"
    msg += f"\n┖ <b>RAM</b> → {virtual_memory().percent}% | <b>UP</b> → {get_readable_time(time() - bot_start_time)}"
    return msg, button

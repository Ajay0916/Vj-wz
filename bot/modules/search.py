from niquests import AsyncSession
from html import escape
from urllib.parse import quote
from pyrogram.enums import ButtonStyle

from .. import LOGGER
from ..core.config_manager import Config
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.ext_utils.telegraph_helper import telegraph
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import edit_message, send_message

PLUGINS = []
SITES = None
TELEGRAPH_LIMIT = 9999999


async def initiate_search_tools():
    if Config.DISABLE_TORRENTS or Config.DISABLE_SEARCH:
        LOGGER.warning("Torrents are disabled. Skipping search plugin initialization.")
        return
    qb_plugins = await TorrentManager.qbittorrent.search.plugins()
    if qb_plugins:
        names = [plugin.name for plugin in qb_plugins]
        await TorrentManager.qbittorrent.search.uninstall_plugin(names)
        PLUGINS.clear()
    if Config.SEARCH_PLUGINS:
        await TorrentManager.qbittorrent.search.install_plugin(Config.SEARCH_PLUGINS)

    if Config.SEARCH_API_LINK:
        global SITES
        try:
            async with AsyncSession() as client:
                response = await client.get(f"{Config.SEARCH_API_LINK}/api/v1/sites")
                data = response.json()
            SITES = {
                str(site): str(site).capitalize() for site in data["supported_sites"]
            }
            SITES["all"] = "All"
        except Exception as e:
            LOGGER.error(
                f"{e} Can't fetching sites from SEARCH_API_LINK make sure use latest version of API"
            )
            SITES = None


async def search(
    key, site, message, method, category="all", quality="", language="", format_="",
    size="all",
):
    if method.startswith("api"):
        if method == "apisearch":
            LOGGER.info(f"API Searching: {key} from {site}")
            if site == "all":
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/search?query={quote(key)}&limit={Config.SEARCH_LIMIT}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/search?site={quote(site)}&query={quote(key)}&limit={Config.SEARCH_LIMIT}"
            if category and category != "all":
                api += f"&category={quote(category)}"
            if quality and quality != "all":
                api += f"&quality={quote(quality)}"
            if language and language != "all":
                api += f"&language={quote(language)}"
            if format_ and format_ != "all":
                api += f"&format={quote(format_)}"
            if size == "small":
                api += "&max_size=1GB"
            elif size == "medium":
                api += "&min_size=1GB&max_size=3GB"
            elif size == "large":
                api += "&min_size=3GB"
        elif method == "apitrend":
            LOGGER.info(f"API Trending from {site}")
            if site == "all":
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/trending?limit={Config.SEARCH_LIMIT}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/trending?site={site}&limit={Config.SEARCH_LIMIT}"
        elif method == "apirecent":
            LOGGER.info(f"API Recent from {site}")
            if site == "all":
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/recent?limit={Config.SEARCH_LIMIT}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/recent?site={site}&limit={Config.SEARCH_LIMIT}"
        try:
            async with AsyncSession() as client:
                response = await client.get(api)
                search_results = response.json()
            if "error" in search_results or search_results["total"] == 0:
                await edit_message(
                    message,
                    f"No result found for <i>{key}</i>\nTorrent Site:- <i>{SITES.get(site)}</i>",
                )
                return
            msg = f"<b>Found {min(search_results['total'], TELEGRAPH_LIMIT)}</b>"
            if method == "apitrend":
                msg += f" <b>trending result(s)\nTorrent Site:- <i>{SITES.get(site)}</i></b>"
            elif method == "apirecent":
                msg += (
                    f" <b>recent result(s)\nTorrent Site:- <i>{SITES.get(site)}</i></b>"
                )
            else:
                msg += f" <b>result(s) for <i>{key}</i>\nTorrent Site:- <i>{SITES.get(site)}</i></b>"
            search_results = search_results["data"]
        except Exception as e:
            await edit_message(message, str(e))
            return
    else:
        LOGGER.info(f"PLUGINS Searching: {key} from {site}")
        search = await TorrentManager.qbittorrent.search.start(
            pattern=key, plugins=[site], category="all"
        )
        search_id = search.id
        while True:
            result_status = await TorrentManager.qbittorrent.search.status(search_id)
            status = result_status[0].status
            if status != "Running":
                break
        dict_search_results = await TorrentManager.qbittorrent.search.results(
            id=search_id, limit=TELEGRAPH_LIMIT
        )
        search_results = dict_search_results.results
        total_results = dict_search_results.total
        if total_results == 0:
            await edit_message(
                message,
                f"No result found for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i>",
            )
            return
        msg = f"<b>Found {min(total_results, TELEGRAPH_LIMIT)}</b>"
        msg += f" <b>result(s) for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i></b>"
        await TorrentManager.qbittorrent.search.delete(search_id)
    link = await get_result(search_results, key, message, method)
    buttons = ButtonMaker()
    buttons.url_button("🔎 VIEW", link, style=ButtonStyle.PRIMARY)
    button = buttons.build_menu(1)
    await edit_message(message, msg, button)


async def get_result(search_results, key, message, method):
    telegraph_content = []
    if method == "apirecent":
        msg = "<h4>API Recent Results</h4>"
    elif method == "apisearch":
        msg = f"<h4>API Search Result(s) For {key}</h4>"
    elif method == "apitrend":
        msg = "<h4>API Trending Results</h4>"
    else:
        msg = f"<h4>PLUGINS Search Result(s) For {key}</h4>"
    for index, result in enumerate(search_results, start=1):
        if method.startswith("api"):
            try:
                if result.get("name"):
                    msg += f"<code><a href='{result.get('url') or '#'}'>{escape(result['name'])}</a></code><br>"
                if "torrents" in result.keys():
                    for subres in result["torrents"]:
                        msg += f"<b>Quality: </b>{subres['quality']} | <b>Type: </b>{subres['type']} | "
                        msg += f"<b>Size: </b>{subres['size']}<br>"
                        if subres.get("torrent"):
                            msg += "<a href='{}/api/v1/torrent_file?url={}'>Direct Link</a>".format(
                                Config.SEARCH_API_LINK, quote(subres["torrent"])
                            )
                        if subres.get("torrent") and subres.get("magnet"):
                            msg += " | "
                        if subres.get("magnet"):
                            msg += "<b>Share Magnet to</b> "
                            msg += f"<a href='http://t.me/share/url?url={quote(subres['magnet'])}'>Telegram</a>"
                        msg += "<br>"
                    msg += "<br>"
                else:
                    if result.get("size"):
                        msg += f"<b>Size: </b>{result['size']}<br>"
                    try:
                        msg += f"<b>Seeders: </b>{result['seeders']} | <b>Leechers: </b>{result['leechers']}<br>"
                    except Exception:
                        pass
                    tags = []
                    if result.get("site"):
                        tags.append(f"Site: {escape(str(result['site']))}")
                    if result.get("category"):
                        tags.append(f"Category: {escape(str(result['category']))}")
                    if result.get("quality"):
                        tags.append(f"Quality: {escape(str(result['quality']))}")
                    if result.get("language"):
                        tags.append(f"Language: {escape(str(result['language']))}")
                    if result.get("format"):
                        tags.append(f"Format: {escape(str(result['format']))}")
                    if result.get("date"):
                        tags.append(f"Date: {escape(str(result['date']))}")
                    if result.get("uploader"):
                        tags.append(f"Uploader: {escape(str(result['uploader']))}")
                    if tags:
                        msg += "<b>" + " | ".join(tags) + "</b><br>"
                    files = result.get("files")
                    if isinstance(files, list) and files:
                        msg += "<b>Files:</b><br>"
                        for f in files[:3]:
                            msg += f"• {escape(str(f))[:90]}<br>"
                        if len(files) > 3:
                            msg += f"<i>+{len(files) - 3} more</i><br>"
                    if result.get("torrent"):
                        msg += "<a href='{}/api/v1/torrent_file?url={}'>Direct Link</a>".format(
                            Config.SEARCH_API_LINK, quote(result["torrent"])
                        )
                    if result.get("torrent") and result.get("magnet"):
                        msg += " | "
                    if result.get("magnet"):
                        msg += "<b>Share Magnet to</b> "
                        msg += f"<a href='http://t.me/share/url?url={quote(result['magnet'])}'>Telegram</a>"
                    if result.get("torrent") or result.get("magnet"):
                        msg += "<br><br>"
                    else:
                        msg += "<br>"
            except Exception:
                continue
        else:
            msg += f"<a href='{result.descrLink}'>{escape(result.fileName)}</a><br>"
            msg += f"<b>Size: </b>{get_readable_file_size(result.fileSize)}<br>"
            msg += f"<b>Seeders: </b>{result.nbSeeders} | <b>Leechers: </b>{result.nbLeechers}<br>"
            link = result.fileUrl
            if link.startswith("magnet:"):
                msg += f"<b>Share Magnet to</b> <a href='http://t.me/share/url?url={quote(link)}'>Telegram</a><br><br>"
            else:
                msg += f"<a href='{link}'>Direct Link</a><br><br>"

        if len(msg.encode("utf-8")) > 39000:
            telegraph_content.append(msg)
            msg = ""

        if index == TELEGRAPH_LIMIT:
            break

    if msg != "":
        telegraph_content.append(msg)

    await edit_message(
        message, f"<b>Creating</b> {len(telegraph_content)} <b>Telegraph pages.</b>"
    )
    path = [
        (
            await telegraph.create_page(
                title="Mirror-leech-bot Torrent Search", content=content
            )
        )["path"]
        for content in telegraph_content
    ]
    if len(path) > 1:
        await edit_message(
            message, f"<b>Editing</b> {len(telegraph_content)} <b>Telegraph pages.</b>"
        )
        await telegraph.edit_telegraph(path, telegraph_content)
    return f"https://telegra.ph/{path[0]}"


API_PAGE_SIZE = 13


SEARCH_CATEGORIES = ["all", "movies", "tv", "anime", "books", "courses", "apps", "games", "music"]

FILTER_QUALITY = ["all", "480", "720", "1080", "4k"]
FILTER_LANGUAGE = ["all", "hindi", "english", "tamil", "telugu", "dual"]
FILTER_FORMAT = ["all", "pdf", "epub", "mobi"]
FILTER_SIZES = ["all", "small", "medium", "large"]

# In-memory filter state (user_id -> dict) so the second filter page can use
# short callbacks (Telegram callback_data is limited to 64 bytes).
FILTER_STATE = {}


def _filter_label(value, kind):
    if not value or value == "all":
        return "All"
    if kind == "quality":
        return "4K" if value == "4k" else f"{value}p"
    return value.capitalize()


def _filter_summary(quality, language, format_):
    parts = []
    if quality and quality != "all":
        parts.append(f"Quality: {_filter_label(quality, 'quality')}")
    if language and language != "all":
        parts.append(f"Language: {_filter_label(language, 'language')}")
    if format_ and format_ != "all":
        parts.append(f"Format: {_filter_label(format_, 'format')}")
    return " | ".join(parts) if parts else "None"


def filter_buttons(user_id, site, category, quality, language, format_):
    buttons = ButtonMaker()
    for v in FILTER_QUALITY:
        name = _filter_label(v, "quality")
        if v == quality:
            name = f"✅ {name}"
        buttons.data_button(
            name,
            f"torser {user_id} fq {site} {category} {v} {language} {format_}",
        )
    for v in FILTER_LANGUAGE:
        name = _filter_label(v, "language")
        if v == language:
            name = f"✅ {name}"
        buttons.data_button(
            name,
            f"torser {user_id} fl {site} {category} {quality} {v} {format_}",
            position="header",
        )
    for v in FILTER_FORMAT:
        name = _filter_label(v, "format")
        if v == format_:
            name = f"✅ {name}"
        buttons.data_button(
            name,
            f"torser {user_id} ff {site} {category} {quality} {language} {v}",
            position="f_body",
        )
    buttons.data_button(
        "✅ Size & More",
        f"torser {user_id} fmore {site} {category} {quality} {language} {format_}",
        position="footer",
    )
    buttons.data_button(
        "◀ Back",
        f"torser {user_id} filtback {site} {category}",
        position="footer",
    )
    return buttons.build_menu(b_cols=5, h_cols=6, fb_cols=4, f_cols=2)


def filter_menu_text(key, site, category, quality, language, format_):
    return (
        f"<b>Filter results for <i>{key}</i></b>\n"
        f"Site:- <i>{SITES.get(site)}</i>\n"
        f"Category:- <i>{category.capitalize()}</i>\n"
        f"Filters:- <i>{_filter_summary(quality, language, format_)}</i>"
    )


def _size_label(value):
    return {"all": "All", "small": "<1GB", "medium": "1-3GB", "large": ">3GB"}.get(
        value, value
    )


def _size_summary(size):
    parts = []
    if size and size != "all":
        parts.append(f"Size: {_size_label(size)}")
    return " | ".join(parts) if parts else "None"


def filter_size_buttons(user_id, size):
    buttons = ButtonMaker()
    for v in FILTER_SIZES:
        name = _size_label(v)
        if v == size:
            name = f"✅ {name}"
        buttons.data_button(name, f"torser {user_id} sz {v}")
    buttons.data_button("✅ Search", f"torser {user_id} fgs", position="footer")
    buttons.data_button("◀ Back", f"torser {user_id} fback1", position="footer")
    return buttons.build_menu(b_cols=4, f_cols=2)


def filter_size_text(key, size):
    return (
        f"<b>More filters for <i>{key}</i></b>\n"
        f"Filters:- <i>{_size_summary(size)}</i>"
    )


def api_categories(user_id, site):
    buttons = ButtonMaker()
    for cat in SEARCH_CATEGORIES:
        name = "All" if cat == "all" else cat.capitalize()
        buttons.data_button(name, f"torser {user_id} catsel {site} {cat}")
    buttons.data_button("◀ Back", f"torser {user_id} backcat", position="footer")
    return buttons.build_menu(2)


def api_buttons(user_id, method, page=1):
    buttons = ButtonMaker()
    # Keep "all" first for quick combo search; the rest keep the API's
    # site order (important sites come first from our API).
    sites = sorted(SITES.items(), key=lambda kv: kv[0] != "all")
    total_pages = max(1, (len(sites) + API_PAGE_SIZE - 1) // API_PAGE_SIZE)
    page = max(1, min(int(page), total_pages))
    start = (page - 1) * API_PAGE_SIZE
    for data, name in sites[start : start + API_PAGE_SIZE]:
        buttons.data_button(name, f"torser {user_id} {data} {method}")
    nav = []
    if page > 1:
        nav.append(("◀", f"torser {user_id} apipage {page - 1} {method}"))
    nav.append((f"{page}/{total_pages}", f"torser {user_id} apipage {page} {method}"))
    if page < total_pages:
        nav.append(("▶", f"torser {user_id} apipage {page + 1} {method}"))
    for text, callback in nav:
        buttons.data_button(text, callback, position="footer")
    buttons.data_button("Cancel", f"torser {user_id} cancel", position="footer")
    return buttons.build_menu(2)


async def plugin_buttons(user_id):
    buttons = ButtonMaker()
    if not PLUGINS:
        pl = await TorrentManager.qbittorrent.search.plugins()
        for i in pl:
            PLUGINS.append(i.name)
    for siteName in PLUGINS:
        buttons.data_button(
            siteName.capitalize(), f"torser {user_id} {siteName} plugin"
        )
    buttons.data_button("All", f"torser {user_id} all plugin")
    buttons.data_button("Cancel", f"torser {user_id} cancel")
    return buttons.build_menu(2)


@new_task
async def torrent_search(_, message):
    if Config.DISABLE_SEARCH:
        await send_message(
            message, "Torrent search is currently disabled by the Bot Owner."
        )
        return
    user_id = message.from_user.id
    buttons = ButtonMaker()
    key = message.text.split()
    if SITES is None and not Config.SEARCH_PLUGINS:
        await send_message(
            message, "No API link or search PLUGINS added for this function"
        )
    elif len(key) == 1 and SITES is None:
        await send_message(message, "Send a search key along with command")
    elif len(key) == 1:
        buttons.data_button("Trending", f"torser {user_id} apitrend")
        buttons.data_button("Recent", f"torser {user_id} apirecent")
        buttons.data_button("Cancel", f"torser {user_id} cancel")
        button = buttons.build_menu(2)
        await send_message(message, "Send a search key along with command", button)
    elif SITES is not None and Config.SEARCH_PLUGINS:
        buttons.data_button("Api", f"torser {user_id} apisearch")
        buttons.data_button("Plugins", f"torser {user_id} plugin")
        buttons.data_button("Cancel", f"torser {user_id} cancel")
        button = buttons.build_menu(2)
        await send_message(message, "Choose tool to search:", button)
    elif SITES is not None:
        button = api_buttons(user_id, "apisearch")
        await send_message(message, "Choose site to search | API:", button)
    else:
        button = await plugin_buttons(user_id)
        await send_message(message, "Choose site to search | Plugins:", button)


@new_task
async def torrent_search_update(_, query):
    user_id = query.from_user.id
    message = query.message
    key = message.reply_to_message.text.split(maxsplit=1)
    key = key[1].strip() if len(key) > 1 else None
    data = query.data.split()
    if user_id != int(data[1]):
        await query.answer("Not Yours!", show_alert=True)
    elif data[2] == "apipage":
        await query.answer()
        page = int(data[3]) if len(data) > 3 and data[3].isdigit() else 1
        method = data[4] if len(data) > 4 else "apisearch"
        button = api_buttons(user_id, method, page)
        await edit_message(message, "Choose site:", button)
    elif data[2].startswith("api"):
        await query.answer()
        button = api_buttons(user_id, data[2])
        await edit_message(message, "Choose site:", button)
    elif data[2] == "plugin":
        await query.answer()
        button = await plugin_buttons(user_id)
        await edit_message(message, "Choose site:", button)
    elif data[2] == "catsel":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        category = data[4] if len(data) > 4 else "all"
        button = filter_buttons(user_id, site, category, "all", "all", "all")
        await edit_message(
            message,
            filter_menu_text(key, site, category, "all", "all", "all"),
            button,
        )
    elif data[2] in ("fq", "fl", "ff"):
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        category = data[4] if len(data) > 4 else "all"
        quality = data[5] if len(data) > 5 else "all"
        language = data[6] if len(data) > 6 else "all"
        format_ = data[7] if len(data) > 7 else "all"
        button = filter_buttons(user_id, site, category, quality, language, format_)
        await edit_message(message, filter_menu_text(key, site, category, quality, language, format_), button)
    elif data[2] == "fmore":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        category = data[4] if len(data) > 4 else "all"
        quality = data[5] if len(data) > 5 else "all"
        language = data[6] if len(data) > 6 else "all"
        format_ = data[7] if len(data) > 7 else "all"
        prev = FILTER_STATE.get(user_id) or {}
        FILTER_STATE[user_id] = {
            "site": site,
            "category": category,
            "quality": quality,
            "language": language,
            "format": format_,
            "size": prev.get("size", "all"),
        }
        button = filter_size_buttons(user_id, FILTER_STATE[user_id]["size"])
        await edit_message(
            message,
            filter_size_text(key, FILTER_STATE[user_id]["size"]),
            button,
        )
    elif data[2] == "sz":
        await query.answer()
        state = FILTER_STATE.get(user_id)
        if not state:
            button = api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
            return
        state["size"] = data[3] if len(data) > 3 else "all"
        button = filter_size_buttons(user_id, state["size"])
        await edit_message(message, filter_size_text(key, state["size"]), button)
    elif data[2] == "fback1":
        await query.answer()
        state = FILTER_STATE.get(user_id) or {}
        site = state.get("site", "all")
        category = state.get("category", "all")
        quality = state.get("quality", "all")
        language = state.get("language", "all")
        format_ = state.get("format", "all")
        button = filter_buttons(user_id, site, category, quality, language, format_)
        await edit_message(message, filter_menu_text(key, site, category, quality, language, format_), button)
    elif data[2] == "fgs":
        await query.answer()
        state = FILTER_STATE.get(user_id)
        if not state:
            button = api_buttons(user_id, "apisearch")
            await edit_message(message, "Choose site:", button)
            return
        site = state["site"]
        category = state["category"]
        quality = state["quality"]
        language = state["language"]
        format_ = state["format"]
        size = state["size"]
        summary = " | ".join(
            p
            for p in (
                _filter_summary(quality, language, format_),
                _size_summary(size),
            )
            if p and p != "None"
        ) or "None"
        await edit_message(
            message,
            f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{SITES.get(site)}</i>\nCategory:- <i>{category.capitalize()}</i>\nFilters:- <i>{summary}</i></b>",
        )
        await search(key, site, message, "apisearch", category, quality, language, format_, size)
    elif data[2] == "filtback":
        await query.answer()
        site = data[3] if len(data) > 3 else "all"
        button = api_categories(user_id, site)
        await edit_message(message, "Choose category:", button)
    elif data[2] == "backcat":
        await query.answer()
        button = api_buttons(user_id, "apisearch")
        await edit_message(message, "Choose site:", button)
    elif data[2] != "cancel":
        await query.answer()
        site = data[2]
        method = data[3]
        if method == "apisearch":
            button = api_categories(user_id, site)
            await edit_message(message, "Choose category:", button)
        elif method.startswith("api"):
            if key is None:
                if method == "apirecent":
                    endpoint = "Recent"
                elif method == "apitrend":
                    endpoint = "Trending"
                await edit_message(
                    message,
                    f"<b>Listing {endpoint} Items...\nTorrent Site:- <i>{SITES.get(site)}</i></b>",
                )
                await search(key, site, message, method)
            else:
                await edit_message(
                    message,
                    f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{SITES.get(site)}</i></b>",
                )
                await search(key, site, message, method)
        else:
            await edit_message(
                message,
                f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i></b>",
            )
            await search(key, site, message, method)
    else:
        await query.answer()
        await edit_message(message, "Search has been canceled!")

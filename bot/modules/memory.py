import asyncio
from asyncio import all_tasks
from time import time

_LAST_ANSWER_TS = 0.0


async def _safe_answer(query, text=None, show_alert=False):
    """Answer a callback query. Always ack instantly (clears Telegram loading
    spinner so taps feel immediate); only the visible alert text is throttled
    to avoid FloodWait on rapid presses. Non-alert acks carry no text."""
    global _LAST_ANSWER_TS
    now = time()
    await query.answer()
    if not show_alert:
        return
    if now - _LAST_ANSWER_TS >= 0.4:
        _LAST_ANSWER_TS = now
        await query.answer(text, show_alert=True)
    # else: silent (spinner already cleared, no alert popup)

from pyrogram.enums import ButtonStyle

from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.mem_guard import (
    budget,
    disk_snapshot,
    disk_breakdown,
    docker_disk_breakdown,
    limit_bytes,
    monitor,
    profiler,
    ram_breakdown,
    readable,
    snapshot,
    trim_caches,
    vps_guard,
    vps_snapshot,
    vps_get,
    vps_set,
    vps_schema,
    SAFE_CLEANUPS,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)


def _wz(title, rows, note=""):
    lines = [f"⌬ <b><u>{title}</u></b>", "│"]
    for index, (key, value) in enumerate(rows):
        edge = "┟" if index == 0 else ("┖" if index == len(rows) - 1 else "┠")
        lines.append(f"{edge} <b>{key}</b> → {value}")
    if note:
        lines.append("")
        lines.append(f"<i>{note}</i>")
    return "\n".join(lines)


def _trend():
    points = monitor.samples[-6:]
    if len(points) < 2:
        return "not enough samples yet"
    first, last = points[0][1], points[-1][1]
    span = max(1, points[-1][0] - points[0][0])
    delta = last - first
    arrow = "steady" if abs(delta) < 2 * 1024 * 1024 else ("rising" if delta > 0 else "falling")
    return f"{arrow} {readable(abs(delta))} over {span}s"


def _overview():
    snap = snapshot()
    rows = [
        ("Resident", readable(snap["rss"])),
        ("Instance", readable(snap["limit"])),
        ("Pressure", f"{snap['pressure'] * 100:.0f}%"),
        ("Peak Seen", readable(snap["peak"])),
        ("Free", readable(snap["available"])),
        ("Trend", _trend()),
        (
            "Transfers",
            f"{readable(snap['budget']['used'])} of "
            f"{readable(snap['budget']['limit'])}"
            f" (peak {readable(snap['budget']['peak'])}, "
            f"{snap['budget']['waits']} wait(s))",
        ),
        ("Caches", readable(snap["cache_total"])),
        ("Auto Trims", str(snap["trims"])),
        ("Profiler", "on" if snap["profiling"] else "off"),
    ]
    return snap, rows


def _vps_overview():
    vsnap = vps_snapshot()
    pressure = vsnap["pressure"]
    services = vsnap["services"]
    rows = [
        ("VPS Pressure", f"{pressure * 100:.0f}%"),
        ("VPS Free", readable(vsnap["total"] - vsnap["used"])),
        ("Services", str(len(services))),
        ("Criticals", str(vsnap["critical"])),
        ("Trims", str(vsnap["trims"])),
    ]
    note = ""
    if pressure >= 0.95:
        note = "CRITICAL — restarts may be triggered"
    elif pressure >= 0.80:
        note = "High pressure — monitoring closely"
    return vsnap, rows, note


def _vps_services_rows():
    vsnap = vps_snapshot()
    services = vsnap["services"]
    rows = []
    for s in services:
        name = s["name"]
        used = s["used"]
        limit = s["limit"]
        ratio = s["ratio"]
        cont = s["cont"]
        tag = "🐳" if cont else "⚙️"
        lim_str = f" / {readable(limit)}" if limit else ""
        pct_str = f" ({ratio * 100:.0f}%)" if ratio else ""
        rows.append((f"{tag} {name}", f"{readable(used)}{lim_str}{pct_str}"))
    if not rows:
        rows = [("Services", "none tracked")]
    top = vsnap["top"]
    if top:
        rows.append(("", ""))
        rows.append(("--- Top Processes ---", ""))
        for p in top[:5]:
            rows.append((f"PID {p['pid']}", f"{readable(p['rss'])} — {p['cmd'][:60]}"))
    return rows


def _menu(user_id, view="main", data=None):
    buttons = ButtonMaker()
    if view == "main":
        snap, rows = _overview()
        buttons.data_button("VPS Guard", f"mem {user_id} vps", position="header")
        buttons.data_button("Disk Guard", f"mem {user_id} disk")
        buttons.data_button("Refresh", f"mem {user_id} main", position="header")
        buttons.data_button("Breakdown", f"mem {user_id} detail")
        if profiler.running:
            buttons.data_button("Top Allocations", f"mem {user_id} top")
            buttons.data_button(
                "Stop Profiler", f"mem {user_id} proff", style=ButtonStyle.DANGER
            )
        else:
            buttons.data_button(
                "Start Profiler", f"mem {user_id} pron", style=ButtonStyle.PRIMARY
            )
        buttons.data_button("Free Memory", f"mem {user_id} trim")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        note = ""
        if snap["pressure"] >= 0.85:
            note = "Under pressure. Caches are being trimmed automatically."
        elif not snap["profiling"]:
            note = "Start the profiler, reproduce the load, then read the top allocations."
        return _wz("Memory", rows, note), buttons.build_menu(2)

    if view == "vps":
        vsnap, rows, note = _vps_overview()
        buttons.data_button("Services", f"mem {user_id} vps_svc")
        buttons.data_button("Settings", f"mem {user_id} vsettings")
        buttons.data_button("Refresh", f"mem {user_id} vps")
        buttons.data_button("VPS Trim", f"mem {user_id} vpstrim")
        buttons.data_button("Back", f"mem {user_id} main", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        return _wz("VPS Guard", rows, note), buttons.build_menu(2)

    if view == "vps_svc":
        rows = _vps_services_rows()
        buttons.data_button("Refresh", f"mem {user_id} vps_svc")
        vsnap = vps_snapshot()
        for s in vsnap["services"]:
            if s["cont"]:
                buttons.data_button(
                    f"restart {s['name']}",
                    f"mem {user_id} rst {s['name']}",
                    style=ButtonStyle.DANGER,
                )
        buttons.data_button("Back", f"mem {user_id} vps", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        note = "Restart buttons: docker containers only (FlareSolverr/tunnels/...)."
        return _wz("VPS Services", rows, note), buttons.build_menu(2)

    if view == "disk":
        ds = disk_snapshot()
        d = ds["disk"]
        rows = [
            ("Total", readable(d["total"])),
            ("Used", readable(d["used"])),
            ("Free", readable(d["free"])),
            ("Usage", f"{d['pct']:.1f}%"),
        ]
        dock = ds["docker"]
        if any(dock.values()):
            for k, v in dock.items():
                if v > 0:
                    rows.append((f"Docker {k}", readable(v)))
        note = ""
        if d["pct"] >= 90:
            note = "CRITICAL — prune Docker to reclaim space"
        elif d["pct"] >= 80:
            note = "High usage — consider pruning"
        buttons.data_button("Refresh", f"mem {user_id} disk")
        buttons.data_button("Disk Details", f"mem {user_id} disk_detail")
        buttons.data_button("Cleanup", f"mem {user_id} dclean")
        buttons.data_button("Back", f"mem {user_id} main", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        return _wz("Disk Guard", rows, note), buttons.build_menu(2)

    if view == "disk_detail":
        ds = disk_snapshot()
        d = ds["disk"]
        rows = [("Free Space", f"{readable(d['free'])} / {readable(d['total'])}")]
        rows.append(("", ""))
        rows.append(("--- Top Dirs (disk used) ---", ""))
        for p, used in disk_breakdown().items():
            rows.append((p, readable(used)))
        rows.append(("", ""))
        rows.append(("--- Docker Containers (disk) ---", ""))
        ctrs = docker_disk_breakdown()
        if ctrs:
            for c in ctrs:
                rows.append((f"{c['name']} ({c['state']})", readable(c["used"])))
        else:
            rows.append(("Containers", "no data"))
        buttons.data_button("Refresh", f"mem {user_id} disk_detail")
        buttons.data_button("Back", f"mem {user_id} disk", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        return _wz("Disk Usage Details", rows, ""), buttons.build_menu(2)

    if view == "vsettings":
        rows = [("Variable", "Value")]
        rows.append(("", ""))
        for item in vps_schema():
            key = item["key"]
            val = vps_get(key)
            rows.append((item["label"], str(val)))
        rows.append(("", ""))
        rows.append(("Note", "Tap a var to change. Changes persist (survive /restart)."))
        buttons = ButtonMaker()
        for item in vps_schema():
            buttons.data_button(item["key"], f"mem {user_id} vsed {item['key']}", style=ButtonStyle.PRIMARY)
        buttons.data_button("Back", f"mem {user_id} vps", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        return _wz("VPS Settings", rows, ""), buttons.build_menu(2)

    if view == "vset":
        key = data[3] if data and len(data) > 3 else None
        item = next((s for s in vps_schema() if s["key"] == key), None)
        if item is None:
            return "<i>Unknown setting</i>", ButtonMaker().build_menu(1)
        val = vps_get(key)
        if item["type"] == "bool":
            btns = ButtonMaker()
            btns.data_button("ON", f"mem {user_id} vsetdo {key} 1", style=ButtonStyle.SUCCESS)
            btns.data_button("OFF", f"mem {user_id} vsetdo {key} 0", style=ButtonStyle.DANGER)
            btns.data_button("Back", f"mem {user_id} vsettings", position="footer")
            return _wz(f"Set {item['label']}", [("Current", str(val))], ""), btns.build_menu(2)
        # numeric: show -step / +step / halve / double quick buttons
        step = item.get("step", 1)
        lo, hi = item.get("min"), item.get("max")
        btns = ButtonMaker()
        for label, delta in [("-", -step), ("+", +step)]:
            nv = val + delta
            if lo is not None:
                nv = max(lo, nv)
            if hi is not None:
                nv = min(hi, nv)
            btns.data_button(f"{label}{step}", f"mem {user_id} vsetdo {key} {nv}", style=ButtonStyle.PRIMARY)
        btns.data_button("Back", f"mem {user_id} vsettings", position="footer")
        return _wz(
            f"Set {item['label']}",
            [("Current", str(val)), ("Range", f"{lo}-{hi} (step {step})" if lo is not None else "any")],
            "Tap +/- to adjust. Value saves immediately.",
        ), btns.build_menu(2)

    if view == "detail":
        snap = snapshot()
        rows = []
        # top RAM consumers (whole VPS process view)
        rows.append(("--- Top RAM Processes ---", ""))
        for p in ram_breakdown()[:8]:
            rows.append((f"PID {p['pid']} {p['name']}", f"{readable(p['rss_kb'] * 1024)}"))
        if not rows[1:]:
            rows.append(("Procs", "no data"))
        rows.append(("", ""))
        for name, size in sorted(snap["caches"].items(), key=lambda kv: -kv[1]):
            rows.append((name, readable(size)))
        if not any("caches" in r[0] for r in rows):
            rows.append(("Caches", "none registered"))
        try:
            rows.append(("Async Tasks", str(len(all_tasks()))))
        except RuntimeError:
            pass
        rows.append(("GC Counts", " / ".join(str(c) for c in snap["gc"]["counts"])))
        if Config.MEM_DEEP_STATS:
            rows.append(("Tracked Objects", f"{snap['gc']['objects']:,}"))
        buttons.data_button("Back", f"mem {user_id} main", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        note = "" if Config.MEM_DEEP_STATS else "Set MEM_DEEP_STATS for object counts."
        return _wz("Memory Breakdown", rows, note), buttons.build_menu(2)

    if view == "top":
        rows = []
        for row in profiler.top(12):
            rows.append((row["where"], f"{readable(row['size'])} / {row['count']}"))
        if not rows:
            rows = [("Profiler", "not running")]
        buttons.data_button("Refresh", f"mem {user_id} top", position="header")
        buttons.data_button("Back", f"mem {user_id} main", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        seen = f"since {int(time() - profiler.started_at)}s ago" if profiler.started_at else ""
        return (
            _wz("Top Allocations", rows, f"size / blocks, {seen}".strip(", ")),
            buttons.build_menu(1),
        )

    return "<i>Unknown view.</i>", buttons.build_menu(1)


@new_task
async def memory_stats(_, message):
    if getattr(Config, 'DISABLE_MEMORY', False):
        return

    text, markup = _menu(message.from_user.id)
    await send_message(message, text, markup)


@new_task
async def memory_callback(_, query):
    user_id = query.from_user.id
    data = query.data.split()
    if len(data) < 3 or user_id != int(data[1]):
        return await _safe_answer(query, "Not yours!", show_alert=True)

    action = data[2]
    if action == "close":
        await _safe_answer(query)
        await delete_message(query.message.reply_to_message)
        return await delete_message(query.message)

    if action == "pron":
        started = profiler.start()
        await _safe_answer(
            query,
            "Profiler on. Reproduce the load, then read Top Allocations."
            if started
            else "Already running.",
            show_alert=started,
        )
    elif action == "proff":
        profiler.stop()
        await _safe_answer(query, "Profiler off.")
    elif action == "trim":
        before = snapshot()["rss"]
        freed = trim_caches(aggressive=True)
        from gc import collect
        collected = collect()
        after = snapshot()["rss"]
        await _safe_answer(
            query,
            f"Freed {readable(freed)} of caches and {collected} objects. "
            f"Resident {readable(before)} to {readable(after)}.",
            show_alert=True,
        )
    elif action == "vpstrim":
        btns = ButtonMaker()
        btns.data_button("Confirm Trim", f"mem {user_id} vpstrimdo")
        btns.data_button("Cancel", f"mem {user_id} vps")
        await _safe_answer(query)
        await edit_message(
            query.message,
            "⚠️ <b>Confirm VPS trim?</b>\n\n"
            "FlareSolverr ko SIGHUP bhejega — purane Chrome sessions clean honge.\n"
            "Koi data loss nahi, full restart se light action.",
            btns.build_menu(2),
        )
        return
    elif action == "vpstrimdo":
        await _safe_answer(query, "Trimming...")
        flags = await asyncio.to_thread(vps_guard.trim)
        btns = ButtonMaker()
        btns.data_button("Back", f"mem {user_id} vps")
        await edit_message(
            query.message,
            (
                f"✅ VPS trim done — SIGHUP sent to: {', '.join(flags) or 'no services'}"
                if flags
                else "⚠️ VPS trim: no services trimmed (docker socket unavailable?)"
            ),
            btns.build_menu(1),
        )
        return
    elif action == "vsed":
        await _safe_answer(query)
        view = "vset"
    elif action == "vsetdo":
        if len(data) < 5:
            await _safe_answer(query, "Bad data", show_alert=True)
        else:
            key = data[3]
            raw = data[4]
            item = next((s for s in vps_schema() if s["key"] == key), None)
            if item is None:
                await _safe_answer(query, "Unknown key", show_alert=True)
            else:
                try:
                    if item["type"] == "int":
                        nv = int(raw)
                    elif item["type"] == "float":
                        nv = float(raw)
                    else:
                        nv = raw == "1"
                    vps_set(key, nv)
                    await _safe_answer(query, f"{key} → {nv}", show_alert=True)
                except Exception as err:
                    await _safe_answer(query, f"Error: {err}", show_alert=True)
        view = "vsettings"
    elif action == "dclean":
        rows = [("Clean All (Safe)", "Run all safe cleanups sequentially"), ("", "")]
        for key, (label, _) in SAFE_CLEANUPS.items():
            rows.append((label, f"Run safely"))
        buttons = ButtonMaker()
        buttons.data_button("Run Clean All", f"mem {user_id} dcrun all", style=ButtonStyle.PRIMARY)
        for key, (label, _) in SAFE_CLEANUPS.items():
            buttons.data_button(label, f"mem {user_id} dcrun {key}", style=ButtonStyle.DANGER)
        buttons.data_button("Back", f"mem {user_id} disk", position="footer")
        buttons.data_button(
            "Close", f"mem {user_id} close", position="footer", style=ButtonStyle.DANGER
        )
        note = "All actions are safe — temp/cache/logs only. No data loss possible."
        await _safe_answer(query)
        await edit_message(query.message, _wz("Safe Cleanup", rows, note), buttons.build_menu(2))
        return
    elif action == "dcrun":
        target = data[3] if len(data) > 3 else None
        if target not in SAFE_CLEANUPS and target != "all":
            await _safe_answer(query, "Invalid cleanup target", show_alert=True)
            return
        # confirm dialog
        if target == "all":
            label = "ALL safe cleanups (Docker + APT + logs + pip)"
        else:
            label = SAFE_CLEANUPS[target][0]
        btns = ButtonMaker()
        btns.data_button("Confirm", f"mem {user_id} dcrundo {target}")
        btns.data_button("Cancel", f"mem {user_id} dclean")
        await _safe_answer(query)
        await edit_message(
            query.message,
            f"⚠️ <b>Confirm cleanup?</b>\n\n<code>{label}</code>\n\n"
            f"Temp/cache/logs sirf hatenge. Containers/volumes/images safe.",
            btns.build_menu(2),
        )
        return
    elif action == "dcrundo":
        target = data[3] if len(data) > 3 else "all"
        await _safe_answer(query, "Cleaning...")
        results = []
        if target == "all":
            for key in SAFE_CLEANUPS:
                _, func = SAFE_CLEANUPS[key]
                try:
                    ok, msg = await asyncio.to_thread(func)
                    results.append((key, ok, msg))
                except Exception as err:
                    results.append((key, False, str(err)))
        else:
            _, func = SAFE_CLEANUPS[target]
            ok, msg = await asyncio.to_thread(func)
            results.append((target, ok, msg))
        # build result summary
        lines = []
        for key, ok, msg in results:
            icon = "✅" if ok else "❌"
            name = SAFE_CLEANUPS[key][0] if key in SAFE_CLEANUPS else key
            lines.append(f"{icon} {name}: {msg[:80]}")
        summary = "\n".join(lines)
        btns = ButtonMaker()
        btns.data_button("Back to Cleanup", f"mem {user_id} dclean")
        btns.data_button("Disk View", f"mem {user_id} disk", position="footer")
        await edit_message(query.message, f"<b>Cleanup Results</b>\n\n{summary}", btns.build_menu(2))
        return
    elif action == "rst":
        if len(data) < 4 or not data[3].isalnum():
            await _safe_answer(query, "Bad service name", show_alert=True)
        else:
            name = data[3]
            btns = ButtonMaker()
            btns.data_button("Confirm Restart", f"mem {user_id} rcst {name}", style=ButtonStyle.DANGER)
            btns.data_button("Cancel", f"mem {user_id} vps_svc")
            await _safe_answer(query)
            await edit_message(
                query.message,
                f"⚠️ <b>Confirm restart?</b>\n\n"
                f"<code>{name}</code> will be docker restarted.\n"
                f"Data in flight may be lost. Continue?",
                btns.build_menu(2),
            )
            return
    elif action == "rcst":
        if len(data) < 4 or not data[3].isalnum():
            await _safe_answer(query, "Bad service name", show_alert=True)
        else:
            name = data[3]
            await _safe_answer(query, f"Restarting {name}...")
            ok, msg = await asyncio.to_thread(vps_guard.restart, name)
            if ok:
                status_text = "restart successful" if msg == "restart successful" else "restart issued, checking..."
                await edit_message(
                    query.message,
                    f"✅ <b>{name}</b> {status_text}",
                )
            else:
                btns = ButtonMaker()
                btns.data_button("Retry", f"mem {user_id} rst {name}", style=ButtonStyle.DANGER)
                btns.data_button("Back", f"mem {user_id} vps_svc")
                await edit_message(
                    query.message,
                    f"❌ <b>Restart failed</b> for <code>{name}</code>. ({msg})",
                    btns.build_menu(2),
                )
            return
    else:
        await _safe_answer(query)

    view = action if action in ("main", "detail", "top", "vps", "vps_svc", "vsettings", "vset", "disk", "disk_detail", "rcst") else "main"
    if action == "vsed":
        view = "vset"
    elif action == "vsetdo":
        view = "vsettings"
    try:
        text, markup = await asyncio.to_thread(_menu, user_id, view, data)
        await edit_message(query.message, text, markup)
    except Exception as err:
        await _safe_answer(query, f"Error: {err}", show_alert=True)
        from ..helper.ext_utils.mem_guard import LOGGER as mg_logger
        mg_logger.error(f"memory menu {view}: {err}")


def memory_report():
    snap = snapshot()
    vsnap = vps_snapshot()
    return (
        f"resident {readable(snap['rss'])} of {readable(limit_bytes())} "
        f"({snap['pressure'] * 100:.0f}%), transfers "
        f"{readable(snap['budget']['used'])}/{readable(budget.limit)}, "
        f"caches {readable(snap['cache_total'])}, "
        f"vps {vsnap['pressure'] * 100:.0f}% pressure"
    )

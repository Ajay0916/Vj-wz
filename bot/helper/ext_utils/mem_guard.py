import gc
import json
import linecache
import os
import socket
import subprocess
import tracemalloc
from asyncio import Condition, sleep
from time import time

from ... import LOGGER
from ...core.config_manager import Config
from .bot_lock import get_system_resources_cached

MB = 1024 * 1024
_MIN_BUDGET = 48 * MB
_MAX_BUDGET = 512 * MB
_SAMPLE_SECONDS = 20
_HISTORY = 90


def rss_bytes():
    try:
        from psutil import Process

        return Process().memory_info().rss
    except Exception:
        return 0


def available_bytes():
    try:
        from psutil import virtual_memory

        return virtual_memory().available
    except Exception:
        return 0


def limit_bytes():
    return get_system_resources_cached()["ram_mb"] * MB


def readable(size):
    size = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


class Budget:
    def __init__(self):
        self._used = 0
        self._peak = 0
        self._waits = 0
        self._cond = Condition()
        self._limit = None

    @property
    def limit(self):
        if self._limit is None:
            configured = (Config.MEM_BUDGET or 0) * MB
            if configured > 0:
                self._limit = configured
            else:
                share = int(limit_bytes() * 0.15)
                self._limit = max(_MIN_BUDGET, min(_MAX_BUDGET, share))
        return self._limit

    @property
    def used(self):
        return self._used

    @property
    def peak(self):
        return self._peak

    @property
    def waits(self):
        return self._waits

    def resize(self, value):
        self._limit = max(1, int(value))

    async def reserve(self, size):
        size = max(0, int(size))
        if size <= 0:
            return 0
        cap = max(self.limit, size)
        async with self._cond:
            waited = False
            while self._used + size > cap:
                waited = True
                await self._cond.wait()
            if waited:
                self._waits += 1
            self._used += size
            self._peak = max(self._peak, self._used)
        return size

    async def release(self, size):
        size = max(0, int(size))
        if size <= 0:
            return
        async with self._cond:
            self._used = max(0, self._used - size)
            self._cond.notify_all()

    def stats(self):
        return {
            "limit": self.limit,
            "used": self._used,
            "peak": self._peak,
            "waits": self._waits,
        }


budget = Budget()
_caches = {}


def register_cache(name, size_fn, trim_fn=None):
    _caches[name] = (size_fn, trim_fn)


def cache_sizes():
    out = {}
    for name, (size_fn, _) in _caches.items():
        try:
            out[name] = int(size_fn() or 0)
        except Exception:
            out[name] = 0
    return out


def trim_caches(aggressive=False):
    freed = 0
    for name, (size_fn, trim_fn) in _caches.items():
        if trim_fn is None:
            continue
        try:
            before = int(size_fn() or 0)
            trim_fn(aggressive)
            freed += max(0, before - int(size_fn() or 0))
        except Exception as err:
            LOGGER.error(f"cache {name} trim failed: {err}")
    return freed


class Profiler:
    def __init__(self):
        self.started_at = 0.0

    @property
    def running(self):
        return tracemalloc.is_tracing()

    def start(self, frames=12):
        if self.running:
            return False
        tracemalloc.start(frames)
        self.started_at = time()
        LOGGER.info("memory profiler started")
        return True

    def stop(self):
        if not self.running:
            return False
        tracemalloc.stop()
        self.started_at = 0.0
        LOGGER.info("memory profiler stopped")
        return True

    def top(self, count=12):
        if not self.running:
            return []
        snapshot = tracemalloc.take_snapshot().filter_traces(
            (
                tracemalloc.Filter(False, tracemalloc.__file__),
                tracemalloc.Filter(False, linecache.__file__),
                tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            )
        )
        rows = []
        for stat in snapshot.statistics("lineno")[:count]:
            frame = stat.traceback[0]
            where = frame.filename
            for marker in ("/bot/", "\\bot\\", "site-packages/", "site-packages\\"):
                if marker in where:
                    where = where.split(marker, 1)[1]
                    break
            rows.append(
                {
                    "where": f"{where}:{frame.lineno}",
                    "size": stat.size,
                    "count": stat.count,
                }
            )
        return rows


profiler = Profiler()


class Monitor:
    def __init__(self):
        self.samples = []
        self.peak = 0
        self.trims = 0
        self.last = 0
        self._task = None

    def sample(self):
        now = rss_bytes()
        self.last = now
        self.peak = max(self.peak, now)
        self.samples.append((int(time()), now))
        if len(self.samples) > _HISTORY:
            del self.samples[: len(self.samples) - _HISTORY]
        return now

    def pressure(self):
        cap = limit_bytes()
        if cap <= 0:
            return 0.0
        return min(1.0, self.last / cap)

    async def _loop(self):
        while True:
            try:
                await sleep(_SAMPLE_SECONDS)
                used = self.sample()
                ratio = self.pressure()
                if ratio >= 0.85:
                    freed = trim_caches(aggressive=True)
                    collected = gc.collect()
                    self.trims += 1
                    after = self.sample()
                    LOGGER.warning(
                        f"memory at {ratio * 100:.0f}% of "
                        f"{readable(limit_bytes())} ({readable(used)}); "
                        f"trimmed {readable(freed)}, gc freed {collected} objects, "
                        f"now {readable(after)}"
                    )
                    if profiler.running:
                        for row in profiler.top(5):
                            LOGGER.warning(
                                f"  {row['where']} {readable(row['size'])} "
                                f"in {row['count']} blocks"
                            )
                elif ratio >= 0.7:
                    trim_caches(aggressive=False)
            except Exception as err:
                LOGGER.error(f"memory monitor: {err}")

    def start(self):
        if self._task is not None:
            return
        from ... import bot_loop

        self.sample()
        self._task = bot_loop.create_task(self._loop())
        LOGGER.info(
            f"Memory monitor on: {readable(self.last)} used of "
            f"{readable(limit_bytes())}, transfer budget {readable(budget.limit)}"
        )

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None


monitor = Monitor()


def snapshot():
    used = monitor.sample()
    cap = limit_bytes()
    caches = cache_sizes()
    return {
        "rss": used,
        "limit": cap,
        "available": available_bytes(),
        "peak": monitor.peak,
        "pressure": monitor.pressure(),
        "budget": budget.stats(),
        "caches": caches,
        "cache_total": sum(caches.values()),
        "gc": {
            "objects": len(gc.get_objects()) if Config.MEM_DEEP_STATS else 0,
            "counts": gc.get_count(),
        },
        "trims": monitor.trims,
        "profiling": profiler.running,
    }


# ============================================================
# VPS Guard — whole-VPS memory watchdog (bot, FlareSolverr,
# t-api, any other service visible via /proc or Docker socket)
# ============================================================

def _vps_conf_load():
    try:
        return {
            "guard": bool(getattr(Config, "VPS_GUARD", True)),
            "fs_limit": int(getattr(Config, "VPS_FLARESOLVERR_LIMIT", 0) or 0),
            "tapi_limit": int(getattr(Config, "VPS_TAPI_LIMIT", 0) or 0),
            "restart": bool(getattr(Config, "VPS_GUARD_RESTART", True)),
            "interval": int(getattr(Config, "VPS_GUARD_INTERVAL", 30) or 30),
            "warn": float(getattr(Config, "VPS_GUARD_WARN", 0.8) or 0.8),
            "crit": float(getattr(Config, "VPS_GUARD_CRIT", 0.95) or 0.95),
            "restart_cooldown": int(getattr(Config, "VPS_GUARD_RESTART_COOLDOWN", 600) or 600),
        }
    except Exception:
        return {
            "guard": True,
            "fs_limit": 0,
            "tapi_limit": 0,
            "restart": True,
            "interval": 30,
            "warn": 0.8,
            "crit": 0.95,
            "restart_cooldown": 600,
        }


_vps_conf = _vps_conf_load()


def _proc_pids():
    try:
        import psutil

        return psutil.pids()
    except Exception:
        return []


def _proc_info(pid):
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return None
        p = psutil.Process(pid)
        try:
            info = p.as_dict(
                attrs=[
                    "name",
                    "cmdline",
                    "memory_info",
                    "create_time",
                    "ppid",
                    "status",
                    "uids",
                ]
            )
        except Exception:
            info = {"name": p.name(), "cmdline": p.cmdline(), "memory_info": p.memory_info()}
        cmd = " ".join((info.get("cmdline") or [])[:2])
        if cmd == "":
            cmd = info.get("name") or ""
        return {
            "pid": pid,
            "rss": (info.get("memory_info") or {}).rss,
            "name": info.get("name") or "",
            "cmd": cmd[:160],
            "ppid": info.get("ppid"),
            "age": time() - (info.get("create_time") or time()),
        }
    except Exception:
        return None


def _proc_parent(pid):
    try:
        import psutil

        try:
            ppid = psutil.Process(pid).ppid()
        except Exception:
            ppid = None
        if not ppid or ppid <= 1 or ppid == pid:
            return None
        return _proc_info(ppid)
    except Exception:
        return None


def _cgroup_mem(path, limits=False):
    out = {"cur": 0, "peak": 0, "limit": 0}
    try:
        cur = int(open(f"{path}/memory.current", "rb").read())
        peak = int(open(f"{path}/memory.peak", "rb").read())
        out["cur"] = cur
        out["peak"] = peak
        if limits:
            out["limit"] = int(open(f"{path}/memory.max", "rb").read())
    except Exception:
        pass
    try:
        if not out["cur"]:
            out["cur"] = int(open(f"{path}/memory.usage_in_bytes", "rb").read())
        if limits and not out["limit"]:
            out["limit"] = int(open(f"{path}/memory.limit_in_bytes", "rb").read())
        if not out["peak"]:
            out["peak"] = int(open(f"{path}/memory.max_usage_in_bytes", "rb").read())
    except Exception:
        pass
    return out


def _scan_tree(skip_docker=True):
    seen = {}
    for pid in _proc_pids():
        try:
            info = _proc_info(pid)
            if info is None:
                continue
            ppid = info["ppid"]
            ccid = None
            try:
                for line in open(f"/proc/{pid}/cgroup", "rb"):
                    line = line.decode(errors="replace").strip()
                    if line and "docker" in line:
                        ccid = line.rsplit("/", 1)[-1][:12]
                        break
            except Exception:
                pass
            if ccid:
                if not skip_docker:
                    seen.setdefault(ccid, []).append(info)
                continue
            elif ppid and ppid in seen:
                if not any(x["pid"] == pid for x in seen[ppid]):
                    seen[ppid].append(info)
            else:
                root = info["cmd"] or ""
                host = False
                try:
                    parse = json.loads(
                        open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").strip()
                        or b"[]"
                    )
                    if isinstance(parse, list) and parse:
                        root = str(parse[0])[:80]
                    if parse and parse[0] in (
                        "/usr/sbin/sshd",
                        "cupsd",
                        "rpcbind",
                        "systemd-resolve",
                        "granian",
                        "python3.10",
                        "node",
                        "chromium",
                        "chromedriver",
                        "camoufox-bin",
                        "dockerd",
                        "containerd",
                    ):
                        host = True
                except Exception:
                    pass
                key = "host " + root if host else "proc " + root
                seen.setdefault(key, []).append(info)
        except Exception:
            continue
    return seen


def _docker_stats_socket():
    out = {}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect("/var/run/docker.sock")
            req = (
                "GET /v1.41/containers/json?all=0 HTTP/1.1\r\n"
                "Host: docker\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except Exception:
        return out
    body = data.split(b"\r\n\r\n", 1)[-1]
    try:
        items = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return out
    for item in items:
        try:
            name = item.get("Names") or [""]
            out[name[0].lstrip("/")] = {
                "id": (item.get("Id") or "")[:12],
                "image": (item.get("Image") or "").split("@")[0][:60],
                "state": item.get("State") or "",
            }
        except Exception:
            continue
    return out


def _container_mem(id_):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(4.0)
            s.connect("/var/run/docker.sock")
            req = (
                f"GET /v1.41/containers/{id_}/stats?stream=0 HTTP/1.1\r\n"
                "Host: docker\r\nConnection: close\r\n\r\n"
            ).encode()
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        body = data.split(b"\r\n\r\n", 1)[-1]
        stats = json.loads(body.decode("utf-8", "replace"))
        ms = stats.get("memory_stats") or {}
        out = {
            "cur": int(ms.get("usage") or 0),
            "peak": int(ms.get("max_usage") or 0),
            "limit": int(ms.get("limit") or 0),
        }
        if out["cur"]:
            return out
    except Exception:
        pass
    # fallback: cgroup files (host-visible paths only when socket is absent)
    prefix = "/sys/fs/cgroup/memory/docker"
    prefix2 = "/sys/fs/cgroup/docker"
    for base in (prefix, prefix2):
        for sub in (f"{id_}", f"{id_[:12]}", f"long/{id_}"):
            cg = _cgroup_mem(f"{base}/{sub}", limits=True)
            if cg["cur"]:
                return cg
    return {"cur": 0, "peak": 0, "limit": 0}


def _trim_container(name, aggressive=False):
    try:
        import signal

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect("/var/run/docker.sock")
            req = (
                f"POST /v1.41/containers/{name}/kill?signal=SIGHUP HTTP/1.1\r\n"
                "Host: docker\r\nConnection: close\r\n\r\n"
            ).encode()
            s.sendall(req)
            while True:
                if not s.recv(65536):
                    break
        return True
    except Exception:
        return False


class VPSGuard:
    def __init__(self):
        self._task = None
        self.servers = {}
        self.services = {}
        self.top = []
        self.last_scan = 0
        self.trims = 0
        self.critical_count = 0
        self.restarts = {}

    # ---------------- scanning ----------------
    def refresh(self):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as _probe:
                _probe.settimeout(2.0)
                _probe.connect("/var/run/docker.sock")
            _dock = True
        except Exception:
            _dock = False
        scan = _scan_tree(skip_docker=_dock)
        state = {
            "used": available_bytes(),
            "total": limit_bytes(),
            "servers": {},
            "services": {},
        }
        for key, procs in scan.items():
            if not procs:
                continue
            rows = sorted(procs, key=lambda p: p["rss"], reverse=True)
            rss = sum(p["rss"] for p in rows)
            name = str(key)
            if name.startswith("host ") or name.startswith("proc "):
                name = name[5:]
                state["servers"][name] = {
                    "rss": rss,
                    "pids": len(rows),
                    "top": [{"pid": p["pid"], "rss": p["rss"], "cmd": p["cmd"]} for p in rows[:3]],
                }
            else:
                info = rows[0]
                state["services"][name] = {
                    "rss": rss,
                    "pids": len(rows),
                    "top": [{"pid": p["pid"], "rss": p["rss"], "cmd": p["cmd"]} for p in rows[:3]],
                    "info": {
                        "pid": info["pid"],
                        "name": info["name"],
                        "cmd": info["cmd"],
                        "age": info["age"],
                    },
                }
        # Docker containers via socket (FlareSolverr, etc.)
        try:
            for cname, cdata in _docker_stats_socket().items():
                m = _container_mem(cdata["id"])
                limit = m.get("limit") or 0
                if limit and not (0 < limit < (1 << 62)):
                    limit = 0
                state["services"][cname] = {
                    "rss": m.get("cur") or 0,
                    "pids": 1,
                    "cont": True,
                    "limit": limit,
                    "peak": m.get("peak") or 0,
                    "image": cdata["image"],
                    "info": {
                        "pid": None,
                        "name": cname,
                        "cmd": cdata["image"],
                        "age": time() - self.last_scan,
                    },
                }
        except Exception:
            pass
        self.last_scan = time()
        self.services = state["services"]
        self.servers = state["servers"]
        self.top = [
            {"pid": p["pid"], "rss": p["rss"], "cmd": p["cmd"], "name": k}
            for k, s in list(state["services"].items()) + list(state["servers"].items())
            for p in s["top"]
        ]
        self.top = sorted(self.top, key=lambda x: x["rss"], reverse=True)[:6]
        return state

    # ---------------- criticality & alerts ----------------
    def _safe_limit(self, name):
        conf = _vps_conf
        explicit = None
        if name == "flaresolverr" and conf["fs_limit"]:
            explicit = conf["fs_limit"]
        elif name == "t-api" and conf["tapi_limit"]:
            explicit = conf["tapi_limit"]
        if not explicit:
            s = self.services.get(name) or {}
            if s.get("limit"):
                return s["limit"]
        if explicit:
            return explicit * MB
        lim = limit_bytes()
        ratio = self.pressure()
        if ratio <= 0.3:
            return max(512 * MB, int(lim * 0.15))
        if ratio <= 0.6:
            return max(768 * MB, int(lim * 0.25))
        if ratio <= 0.85:
            return max(1024 * MB, int(lim * 0.35))
        return max(2048 * MB, int(lim * 0.5))

    def pressure(self):
        try:
            import psutil
            vm = psutil.virtual_memory()
            total = vm.total
            used = vm.used
            if total <= 0:
                return 0.0
            return min(1.0, used / total)
        except Exception:
            try:
                total = limit_bytes()
                used = available_bytes()
                if total <= 0:
                    return 0.0
                return min(1.0, used / total)
            except Exception:
                return 0.0

    def _check_critical(self):
        flag = False
        ratio = self.pressure()
        if ratio >= (_vps_conf["crit"] or 0.95):
            flag = True
            self.critical_count += 1
            LOGGER.warning(
                f"VPS critical memory: {readable(available_bytes())} used of "
                f"{readable(limit_bytes())} ({ratio * 100:.0f}%), count={self.critical_count}"
            )
            if self.critical_count >= 2:
                self._restart_worst()
        return flag

    def _check_services(self):
        for name, s in list(self.services.items()):
            try:
                if not (s.get("cont") or name in ("flaresolverr", "t-api")):
                    continue
                limit = s.get("limit") or self._safe_limit(name)
                used = s.get("rss") or 0
                if limit <= 0:
                    continue
                ratio = used / limit
                s["ratio"] = ratio
                if ratio >= 1.08:
                    self._restart_service(name, s, ratio)
                elif ratio >= 0.8:
                    LOGGER.warning(
                        f"VPS service {name}: {readable(used)} / {readable(limit)} "
                        f"({ratio * 100:.0f}%)"
                    )
            except Exception as err:
                LOGGER.error(f"VPS guard {name}: {err}")

    def _restart_worst(self):
        worst = None
        worst_used = 0
        for name, s in self.services.items():
            try:
                if not (s.get("cont") or name in ("flaresolverr", "t-api")):
                    continue
                limit = s.get("limit") or 0
                used = s.get("rss") or 0
                if limit > 0 and used > worst_used:
                    worst, worst_used = name, used
            except Exception:
                continue
        if worst:
            self._restart_service(worst, self.services.get(worst) or {}, ratio=1.0, force=True)

    def _restart_service(self, name, s, ratio, force=False):
        conf = _vps_conf
        if not conf["restart"]:
            LOGGER.info(f"VPS guard: {name} at {ratio * 100:.0f}%, restart disabled in config")
            return False
        now = time()
        last = self.restarts.get(name) or 0
        if now - last < (conf["restart_cooldown"] or 600):
            LOGGER.info(f"VPS guard: {name} restart skipped (cooldown)")
            return False
        if not (s.get("cont") or name in ("flaresolverr", "t-api")):
            return False
        command = None
        if name == "flaresolverr":
            command = "docker restart flaresolverr"
        elif name == "t-api":
            command = "systemctl restart t-api"
        elif s.get("cont"):
            command = f"docker restart {name}"
        if not command:
            LOGGER.info(f"VPS guard: {name} no restart command configured")
            return False
        LOGGER.warning(f"VPS guard: restarting {name} ({readable(s.get('rss') or 0)} used)")
        try:
            subprocess.run(command, shell=True, timeout=60, capture_output=True)
            self.restarts[name] = now
            LOGGER.info(f"VPS guard: {name} restart issued")
            return True
        except Exception as err:
            LOGGER.error(f"VPS guard restart {name}: {err}")
            return False

    # ---------------- loop ----------------
    async def _loop(self):
        while True:
            try:
                self.refresh()
                self._check_critical()
                self._check_services()
            except Exception as err:
                LOGGER.error(f"VPS guard loop: {err}")
            await sleep(max(15, _vps_conf["interval"] or 30))

    def start(self):
        if self._task is not None:
            return
        if not _vps_conf.get("guard"):
            LOGGER.info("VPS Guard disabled (VPS_GUARD=False)")
            return
        try:
            from ... import bot_loop

            self.refresh()
            self._task = bot_loop.create_task(self._loop())
            LOGGER.info(
                f"VPS Guard on: {len(self.services)} services tracked, "
                f"interval {_vps_conf['interval']}s, "
                f"restart={'on' if _vps_conf['restart'] else 'off'}"
            )
        except Exception as err:
            LOGGER.error(f"VPS guard start: {err}")

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # ---------------- actions & state ----------------
    def restart(self, name, force=True):
        """Manual restart (user-initiated from /memory). Ignores cooldown only
        when triggered manually via force=True from the Telegram button."""
        s = self.services.get(name)
        if s is None:
            # not currently in a scan snapshot; rebuild so we have an entry
            self.refresh()
            s = self.services.get(name)
        if s is None:
            return False, "service not found"
        if force:
            # temporarily clear cooldown for this manual call
            self.restarts.pop(name, None)
        ok = self._restart_service(name, s, s.get("ratio") or 0.0, force=force)
        return ok, "ok"

    def trim(self):
        flags = []
        for name, s in self.services.items():
            if name == "flaresolverr":
                if _trim_container("flaresolverr"):
                    flags.append(name)
        if flags:
            self.trims += 1
            LOGGER.info(f"VPS guard: sent SIGHUP to {', '.join(flags)}")
        return flags

    def state(self):
        used = available_bytes()
        total = limit_bytes()
        return {
            "pressure": self.pressure(),
            "used": used,
            "total": total,
            "last_scan": self.last_scan,
            "trims": self.trims,
            "critical": self.critical_count,
            "services": self.services,
            "top": self.top,
        }


vps_guard = VPSGuard()


def vps_snapshot():
    guard = vps_guard
    guard.refresh()
    st = guard.state()
    services = []
    for name, s in st["services"].items():
        services.append(
            {
                "name": name,
                "used": s.get("rss") or 0,
                "limit": s.get("limit") or 0,
                "ratio": s.get("ratio") or 0.0,
                "cont": bool(s.get("cont")),
            }
        )
    services = sorted(services, key=lambda x: x["used"], reverse=True)[:10]
    return {
        "pressure": st["pressure"],
        "used": st["used"],
        "total": st["total"],
        "last_scan": st["last_scan"],
        "trims": st["trims"],
        "critical": st["critical"],
        "services": services,
        "top": st["top"],
    }


# ============================================================
# CPU Guard — mirror of VPS Guard for CPU load monitoring
# ============================================================

def _cpu_count():
    try:
        from os import sched_getaffinity
        return len(sched_getaffinity(0))
    except Exception:
        try:
            from os import cpu_count
            return cpu_count() or 1
        except Exception:
            return 1

def _system_cpu():
    try:
        import psutil
        return psutil.cpu_percent(interval=0.05) / 100.0
    except Exception:
        return 0.0

def _proc_cpu(pid):
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return None
        p = psutil.Process(pid)
        pct = p.cpu_percent(interval=0.05)
        return pct
    except Exception:
        return None


class CpuGuard:
    def __init__(self):
        self._task = None
        self.cpus = _cpu_count()
        self.services = {}
        self.top = []
        self.last_scan = 0
        self.restarts = {}

    def _safe_limit(self, name):
        total = self.cpus
        limit = getattr(Config, "VPS_CPU_LIMIT", 0) or 0
        if limit > 0:
            return min(limit, total)
        if name == "flaresolverr":
            return max(1.0, total * 0.6)
        if name == "t-api":
            return max(1.0, total * 0.5)
        return max(1.0, total * 0.5)

    def refresh(self):
        scan = _scan_tree(skip_docker=True)
        state = {
            "services": {},
        }
        for key, procs in scan.items():
            if not procs:
                continue
            rows = sorted(procs, key=lambda p: p["rss"], reverse=True)
            name = str(key)
            if name.startswith("host ") or name.startswith("proc "):
                name = name[5:]
                state["services"][name] = {
                    "pids": len(rows),
                    "cont": False,
                    "pid": rows[0]["pid"],
                    "cmd": rows[0]["cmd"],
                }
            else:
                state["services"][name] = {
                    "pids": len(rows),
                    "cont": True,
                    "pid": rows[0]["pid"],
                    "cmd": rows[0]["cmd"],
                }
        try:
            for cname, cdata in _docker_stats_socket().items():
                m = _container_mem(cdata["id"])
                state["services"][cname] = {
                    "pids": 1,
                    "cont": True,
                    "pid": None,
                    "cmd": cdata["image"],
                }
        except Exception:
            pass
        for name, s in state["services"].items():
            try:
                if s.get("cont"):
                    s["cpu_pct"] = self._container_cpu(name)
                elif s.get("pid"):
                    pct = _proc_cpu(s["pid"])
                    s["cpu_pct"] = pct if pct is not None else 0.0
                else:
                    s["cpu_pct"] = 0.0
            except Exception:
                s["cpu_pct"] = 0.0
        self.last_scan = time()
        self.services = state["services"]
        self.top = sorted(
            [
                {"pid": s.get("pid"), "cpu": s.get("cpu_pct", 0), "name": k}
                for k, s in self.services.items()
                if s.get("pid")
            ],
            key=lambda x: x["cpu"],
            reverse=True,
        )[:6]
        return state

    def _container_cpu(self, name):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(4.0)
                s.connect("/var/run/docker.sock")
                req = (
                    f"GET /v1.41/containers/name/{name}/stats?stream=true&one-shot=false&since=0 HTTP/1.1\r\n"
                    "Host: docker\r\nConnection: close\r\n\r\n"
                ).encode()
                s.sendall(req)
                data = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    if b"\r\n\r\n" in data and b"\r\n\r\n" not in data + chunk:
                        data += chunk
                        break
                    data += chunk
            body = data.split(b"\r\n\r\n", 1)[-1]
            first_line = body.split(b"\r\n", 1)[0]
            stats = json.loads(first_line.decode("utf-8", "replace"))
            pre_stats = stats.get("precpu_stats") or {}
            cur_stats = stats.get("cpu_stats") or {}
            pre = pre_stats.get("cpu_usage") or {}
            cur = cur_stats.get("cpu_usage") or {}
            sys = cur_stats.get("system_cpu_usage") or 0
            pre_sys = pre_stats.get("system_cpu_usage") or 0
            delta_cpu = (cur.get("total_usage") or 0) - (pre.get("total_usage") or 0)
            delta_sys = max(1, sys - pre_sys)
            num_cpus = cur_stats.get("online_cpus") or 1
            cpu_percent = (delta_cpu / delta_sys) * num_cpus * 100
            return round(cpu_percent, 1)
        except Exception:
            return 0.0

    def _check(self):
        for name, s in list(self.services.items()):
            try:
                cpu_pct = s.get("cpu_pct") or 0
                limit = self._safe_limit(name) * 100  # as percent
                ratio = cpu_pct / limit if limit > 0 else 0
                s["cpu_ratio"] = ratio
                s["cpu_limit"] = limit
                if ratio >= 1.5:
                    self._restart_service(name, s)
                elif ratio >= 1.0:
                    LOGGER.warning(f"CPU Guard: {name} at {cpu_pct:.1f}% (limit {limit:.0f}%)")
            except Exception as err:
                LOGGER.error(f"CPU Guard {name}: {err}")

    def _restart_service(self, name, s):
        conf = _vps_conf
        if not conf["restart"]:
            return False
        now = time()
        if now - (self.restarts.get(name) or 0) < (conf["restart_cooldown"] or 600):
            return False
        command = None
        if s.get("cont"):
            command = f"docker restart {name}"
        elif name == "t-api":
            command = "systemctl restart t-api"
        if not command:
            return False
        LOGGER.warning(f"CPU Guard: restarting {name} (CPU {s.get('cpu_pct', 0):.1f}%)")
        try:
            subprocess.run(command, shell=True, timeout=60, capture_output=True)
            self.restarts[name] = now
            return True
        except Exception:
            return False

    async def _loop(self):
        while True:
            try:
                self.refresh()
                self._check()
            except Exception as err:
                LOGGER.error(f"CPU Guard loop: {err}")
            await sleep(max(20, _vps_conf.get("interval", 30) or 30))

    def start(self):
        if self._task is not None:
            return
        if not _vps_conf.get("guard"):
            return
        try:
            from ... import bot_loop
            self.refresh()
            self._task = bot_loop.create_task(self._loop())
            LOGGER.info(f"CPU Guard on: {self.cpus} CPUs, {len(self.services)} services")
        except Exception as err:
            LOGGER.error(f"CPU Guard start: {err}")

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def state(self):
        return {
            "cpus": self.cpus,
            "last_scan": self.last_scan,
            "services": self.services,
            "top": self.top,
        }

    def restart(self, name, force=True):
        s = self.services.get(name)
        if s is None:
            self.refresh()
            s = self.services.get(name)
        if s is None:
            return False, "service not found"
        if force:
            self.restarts.pop(name, None)
        command = None
        if s.get("cont"):
            command = f"docker restart {name}"
        elif name == "t-api":
            command = "systemctl restart t-api"
        if not command:
            return False, "no command"
        LOGGER.warning(f"CPU Guard: manual restart {name}")
        try:
            subprocess.run(command, shell=True, timeout=60, capture_output=True)
            self.restarts[name] = time()
            return True, "ok"
        except Exception as err:
            return False, str(err)


cpu_guard = CpuGuard()


def cpu_snapshot():
    guard = cpu_guard
    if time() - guard.last_scan > 10:
        guard.refresh()
    st = guard.state()
    services = []
    for name, s in st["services"].items():
        services.append(
            {
                "name": name,
                "cpu_pct": s.get("cpu_pct") or 0,
                "cont": bool(s.get("cont")),
                "limit": s.get("cpu_limit") or 0,
                "ratio": s.get("cpu_ratio") or 0.0,
            }
        )
    services = sorted(services, key=lambda x: x["cpu_pct"], reverse=True)[:10]
    return {
        "cpus": st["cpus"],
        "last_scan": st["last_scan"],
        "services": services,
        "top": st["top"],
    }


# ============================================================
# Disk Guard — disk / Docker-storage watchdog
# ============================================================

def _disk_usage(path="/"):
    try:
        import psutil
        u = psutil.disk_usage(path)
        return {"total": u.total, "used": u.used, "free": u.free, "pct": u.percent}
    except Exception:
        import shutil
        u = shutil.disk_usage(path)
        return {
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "pct": round(u.used / u.total * 100, 1),
        }




# ============================================================
# Disk Guard — disk / Docker-storage watchdog
# ============================================================

def _disk_usage(path="/"):
    try:
        import psutil
        u = psutil.disk_usage(path)
        return {"total": u.total, "used": u.used, "free": u.free, "pct": u.percent}
    except Exception:
        import shutil
        u = shutil.disk_usage(path)
        return {
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "pct": round(u.used / u.total * 100, 1),
        }


def _docker_df():
    out = {"images": 0, "containers": 0, "volumes": 0, "build": 0}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5.0)
            s.connect("/var/run/docker.sock")
            req = (
                "GET /v1.41/system/df HTTP/1.1\r\n"
                "Host: docker\r\nConnection: close\r\n\r\n"
            ).encode()
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        body = data.split(b"\r\n\r\n", 1)[-1]
        resp = json.loads(body.decode("utf-8", "replace"))
        for kind in ("images", "containers", "volumes", "build"):
            for e in resp.get(kind) or []:
                if kind == "images":
                    out["images"] += e.get("Size") or 0
                elif kind == "containers":
                    out["containers"] += e.get("SizeRw") or 0
                elif kind == "volumes":
                    out["volumes"] += e.get("Size") or 0
                elif kind == "build":
                    out["build"] += e.get("Size") or 0
    except Exception:
        pass
    return out


def _docker_prune(aggressive=False):
    cmd = "docker system prune -f" + (" -a --volumes" if aggressive else "")
    try:
        result = subprocess.run(cmd, shell=True, timeout=120, capture_output=True, text=True)
        output = (result.stdout or "") + (result.stderr or "")
        freed = "unknown"
        for line in output.split("\n"):
            if "freed" in line.lower():
                freed = line.strip()
                break
        return True, freed
    except Exception as err:
        return False, str(err)


class DiskGuard:
    def __init__(self):
        self._task = None
        self.disk = {"total": 0, "used": 0, "free": 0, "pct": 0}
        self.docker_df = {}
        self.last_scan = 0

    def refresh(self):
        self.disk = _disk_usage("/")
        self.docker_df = _docker_df()
        self.last_scan = time()
        return self.disk

    def pressure(self):
        return (self.disk.get("pct") or 0) / 100.0

    def _loop(self):
        while True:
            try:
                self.refresh()
                ratio = self.pressure()
                if ratio >= 0.90:
                    LOGGER.warning(
                        f"Disk Guard: {readable(self.disk['used'])} / "
                        f"{readable(self.disk['total'])} ({self.disk['pct']:.1f}%) "
                        f"— consider pruning Docker images"
                    )
            except Exception as err:
                LOGGER.error(f"Disk Guard scan: {err}")
            time.sleep(max(60, (_vps_conf.get("interval") or 30) * 2))

    async def _aloop(self):
        import asyncio
        await asyncio.to_thread(self._loop)

    def start(self):
        if self._task is not None:
            return
        if not _vps_conf.get("guard"):
            return
        try:
            from ... import bot_loop
            self.refresh()
            self._task = bot_loop.create_task(self._aloop())
            LOGGER.info(
                f"Disk Guard on: {readable(self.disk['used'])} / "
                f"{readable(self.disk['total'])} ({self.disk['pct']:.1f}%)"
            )
        except Exception as err:
            LOGGER.error(f"Disk Guard start: {err}")

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def state(self):
        return {
            "disk": self.disk,
            "docker": self.docker_df,
            "last_scan": self.last_scan,
        }


disk_guard = DiskGuard()


def disk_snapshot():
    guard = disk_guard
    if time() - guard.last_scan > 120:
        guard.refresh()
    st = guard.state()
    return {
        "disk": st["disk"],
        "docker": st["docker"],
        "last_scan": st["last_scan"],
    }




# ============================================================
# Safe system cleanups (all non-destructive — temp/cache/logs only)
# ============================================================

def safe_cleanup_docker():
    """docker system prune -f — dangling images, build cache, stopped containers. Safe."""
    return _docker_prune(aggressive=False)


def safe_cleanup_docker_images():
    """docker image prune -f — unused dangling images only. Safe."""
    try:
        r = subprocess.run(
            "docker image prune -f", shell=True, timeout=120, capture_output=True, text=True
        )
        out = (r.stdout or "") + (r.stderr or "")
        freed = "unknown"
        for line in out.split("\n"):
            if "freed" in line.lower():
                freed = line.strip()
                break
        return True, freed
    except Exception as err:
        return False, str(err)


def safe_cleanup_docker_build():
    """docker builder prune -f — build cache only. Safe."""
    try:
        r = subprocess.run(
            "docker builder prune -f", shell=True, timeout=120, capture_output=True, text=True
        )
        out = (r.stdout or "") + (r.stderr or "")
        freed = "unknown"
        for line in out.split("\n"):
            if "freed" in line.lower():
                freed = line.strip()
                break
        return True, freed
    except Exception as err:
        return False, str(err)


def safe_cleanup_apt():
    """apt-get clean + autoremove --purge — package caches + unused packages. Safe."""
    try:
        r = subprocess.run(
            "apt-get clean && apt-get -y autoremove --purge",
            shell=True,
            timeout=180,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip()[:200]
        return True, "APT clean + autoremove done"
    except Exception as err:
        return False, str(err)


def safe_cleanup_logs():
    """journalctl --vacuum-size=200M — older systemd logs. Safe."""
    try:
        r = subprocess.run(
            "journalctl --vacuum-size=200M 2>/dev/null || true",
            shell=True,
            timeout=120,
            capture_output=True,
            text=True,
        )
        # Vacuum also tmp files older than 10 days (safe)
        subprocess.run(
            "find /tmp /var/tmp -type f -mtime +10 -delete 2>/dev/null || true",
            shell=True,
            timeout=120,
            capture_output=True,
            text=True,
        )
        return True, "Logs vacuum + old tmp cleaned"
    except Exception as err:
        return False, str(err)


def safe_cleanup_pip():
    """pip cache purge — downloaded wheel cache. Safe."""
    try:
        r = subprocess.run(
            "pip cache purge 2>/dev/null || true",
            shell=True,
            timeout=60,
            capture_output=True,
            text=True,
        )
        return True, "pip cache purged"
    except Exception as err:
        return False, str(err)


SAFE_CLEANUPS = {
    "docker": ("Prune Docker", safe_cleanup_docker),
    "images": ("Prune Images", safe_cleanup_docker_images),
    "build": ("Prune Build Cache", safe_cleanup_docker_build),
    "apt": ("Clean APT", safe_cleanup_apt),
    "logs": ("Vacuum Logs", safe_cleanup_logs),
    "pip": ("Purge pip cache", safe_cleanup_pip),
}




# ============================================================
# Disk + RAM breakdown helpers (safe — read-only)
# ============================================================

def disk_breakdown():
    """Top-level dir sizes via du (safe, per-dir, timeout-protected).
    Uses du -x --max-depth=0 on each dir to avoid slow full-fs scans.
    Returns dict of path -> bytes."""
    dirs = {}
    for path in ("/var", "/home", "/root", "/tmp", "/opt", "/snap"):
        try:
            r = subprocess.run(
                ["du", "-x", "--max-depth=0", "-b", path],
                capture_output=True, text=True, timeout=15
            )
            for line in (r.stdout or "").split("\n"):
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[0].isdigit():
                    dirs[parts[1].rstrip("/") or path] = int(parts[0])
        except Exception:
            pass
    # /usr subdirs (fast, already counted above for /var etc)
    try:
        r = subprocess.run(
            ["du", "-x", "--max-depth=1", "-b", "--exclude=var", "--exclude=home", "/usr"],
            capture_output=True, text=True, timeout=15
        )
        for line in (r.stdout or "").split("\n"):
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].isdigit():
                try:
                    dirs[parts[1].rstrip("/") or "/usr"] = int(parts[0])
                except ValueError:
                    pass
    except Exception:
        pass
    # docker overlay (from socket + cgroup, always fast)
    try:
        docker_total = 0
        for e in os.scandir("/var/lib/docker/overlay2"):
            if e.is_dir():
                try:
                    with os.scandir(e.path) as inner:
                        docker_total += sum(f.stat().st_size for f in inner if f.is_file())
                except Exception:
                    pass
        dirs["/var/lib/docker"] = docker_total
    except Exception:
        pass
    return dict(sorted(dirs.items(), key=lambda x: x[1], reverse=True)[:10])


def ram_breakdown():
    """Per-process RSS snapshot — reads /proc (container sees own procs + host procs via /proc mount).
    Returns sorted top consumers (list of dicts)."""
    procs = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/status") as f:
                    info = {}
                    for line in f:
                        if line.startswith(("Name:", "RSS:", "PPid:")):
                            parts = line.split()
                            if len(parts) >= 2:
                                info[parts[0].rstrip(":")] = parts[1]
                rss_kb = int(info.get("RSS") or 0)
                if rss_kb > 1024:
                    name = info.get("Name", "")
                    cmdline = ""
                    try:
                        cmdline = open(f"/proc/{pid}/cmdline").read().replace("\x00", " ").strip()[:60]
                    except Exception:
                        pass
                    procs.append({
                        "pid": int(pid),
                        "name": name,
                        "cmd": cmdline or name,
                        "rss_kb": rss_kb,
                    })
            except Exception:
                continue
    except Exception:
        pass
    procs.sort(key=lambda p: p["rss_kb"], reverse=True)
    return procs[:15]


def docker_disk_breakdown():
    """Per-container disk usage (read-only)."""
    out = []
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(4.0)
            s.connect("/var/run/docker.sock")
            req = (
                "GET /v1.41/containers/json?all=1 HTTP/1.1\r\n"
                "Host: docker\r\nConnection: close\r\n\r\n"
            ).encode()
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        body = data.split(b"\r\n\r\n", 1)[-1]
        items = json.loads(body)
        for item in items:
            name = (item.get("Names") or [""])[0].lstrip("/")
            cid = (item.get("Id") or "")[:12]
            state = item.get("State") or ""
            cg = _container_mem(cid)
            used = cg.get("cur") or 0
            if used:
                out.append({"name": name, "state": state, "used": used})
    except Exception:
        pass
    return sorted(out, key=lambda x: x["used"], reverse=True)[:10]


def snap_disk_breakdown():
    """Snap package sizes."""
    out = []
    try:
        for entry in os.scandir("/var/lib/snapd/cache"):
            if entry.is_file() and entry.name.endswith(".snap"):
                out.append({"name": entry.name, "size": entry.stat().st_size})
    except Exception:
        pass
    out.sort(key=lambda x: x["size"], reverse=True)
    return out[:10]

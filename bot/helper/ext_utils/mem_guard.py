import gc
import json
import linecache
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

#!/usr/bin/env python3
"""Realtime hardware monitor for this Ubuntu server.

Serves a dashboard at http://<host>:PORT/ and streams metrics once per second
over Server-Sent Events at /api/stream. Only dependency: psutil.
"""
import glob
import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

PORT = int(os.environ.get("MONITOR_PORT", "4400"))
BIND = os.environ.get("MONITOR_BIND", "0.0.0.0")
INTERVAL = float(os.environ.get("MONITOR_INTERVAL", "1.0"))
HERE = os.path.dirname(os.path.abspath(__file__))

SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup2", "nsfs"}
SKIP_NIC_PREFIX = ("lo", "veth", "br-", "docker", "virbr")


def _read(path, cast=str):
    try:
        with open(path) as f:
            return cast(f.read().strip())
    except Exception:
        return None


def _gpu_names():
    names = {}
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        dev = os.path.realpath(os.path.join(card, "device"))
        pci = os.path.basename(dev)
        name = None
        try:
            out = subprocess.run(["lspci", "-s", pci], capture_output=True, text=True, timeout=2).stdout
            name = out.split(": ", 1)[1].strip() if ": " in out else None
            if name and "[" in name and "]" in name:
                # "Navi 22 [Radeon RX 6700/6700 XT ...]" -> bracket part reads better
                name = name[name.rfind("[") + 1:name.rfind("]")] or name
        except Exception:
            pass
        names[card] = name or os.path.basename(card)
    return names


GPU_NAMES = _gpu_names()


def gpu_stats():
    gpus = []
    for card, name in GPU_NAMES.items():
        d = os.path.join(card, "device")
        busy = _read(f"{d}/gpu_busy_percent", int)
        if busy is None:
            continue
        hw = glob.glob(f"{d}/hwmon/hwmon*")
        temp = _read(f"{hw[0]}/temp1_input", int) if hw else None
        power = _read(f"{hw[0]}/power1_average", int) if hw else None
        if power is None and hw:
            power = _read(f"{hw[0]}/power1_input", int)
        sclk = _read(f"{hw[0]}/freq1_input", int) if hw else None
        fan = _read(f"{hw[0]}/fan1_input", int) if hw else None
        fan_max = _read(f"{hw[0]}/fan1_max", int) if hw else None
        levels = [int(x) for x in re.findall(r"(\d+)Mhz", _read(f"{d}/pp_dpm_sclk") or "")]
        vram_total = _read(f"{d}/mem_info_vram_total", int)
        gpus.append({
            # APUs only get a small VRAM carve-out; their power reading is the whole CPU+iGPU package
            "integrated": bool(vram_total and vram_total <= 2 * 1024**3),
            "clock": sclk / 1e6 if sclk is not None else None,
            "clock_max": max(levels) if levels else None,
            "fan": fan,
            "fan_max": fan_max,
            "name": name,
            "card": os.path.basename(card),
            "busy": busy,
            "vram_used": _read(f"{d}/mem_info_vram_used", int),
            "vram_total": _read(f"{d}/mem_info_vram_total", int),
            "temp": temp / 1000 if temp else None,
            "power": power / 1e6 if power else None,
        })
    return gpus


def disks():
    seen, out = set(), []
    for p in psutil.disk_partitions(all=False):
        if p.fstype in SKIP_FS or p.device in seen or p.device.startswith("/dev/loop"):
            continue
        seen.add(p.device)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        out.append({"device": p.device, "mount": p.mountpoint, "fstype": p.fstype,
                    "total": u.total, "used": u.used, "percent": u.percent})
    return out


def temps():
    out = []
    try:
        for chip, entries in psutil.sensors_temperatures().items():
            for i, e in enumerate(entries):
                out.append({"chip": chip, "label": e.label or f"#{i}", "current": e.current,
                            "high": e.high, "critical": e.critical})
    except Exception:
        pass
    return out


class CpuPower:
    """CPU package power in watts.

    Prefers RAPL energy counters (precise, but root-only by default on modern kernels);
    falls back to the AMD APU's PPT sensor, which reports the whole package (CPU + iGPU).
    """

    RAPL = "/sys/class/powercap/intel-rapl:0/energy_uj"
    RAPL_MAX = "/sys/class/powercap/intel-rapl:0/max_energy_range_uj"

    def __init__(self):
        self.last = None
        self.wrap = _read(self.RAPL_MAX, int) or 2**32

    def read(self, gpus, now):
        e = _read(self.RAPL, int)
        if e is not None:
            prev, self.last = self.last, (now, e)
            if prev:
                de = e - prev[1]
                if de < 0:
                    de += self.wrap
                return de / 1e6 / max(now - prev[0], 1e-3), "RAPL package"
            return None, "RAPL package"
        for g in gpus:
            if g["integrated"] and g["power"] is not None:
                return g["power"], "APU package (PPT)"
        return None, None


CPU_POWER = CpuPower()


def power_stats(gpus, now):
    cpu, src = CPU_POWER.read(gpus, now)
    dgpus = [{"name": g["name"], "card": g["card"], "watts": g["power"]}
             for g in gpus if not g["integrated"] and g["power"] is not None]
    gpu = sum(g["watts"] for g in dgpus) if dgpus else None
    parts = [v for v in (cpu, gpu) if v is not None]
    return {"cpu": cpu, "cpu_source": src, "gpu": gpu, "gpus": dgpus,
            "total": sum(parts) if parts else None}


CPU_TEMP_CHIPS = ("k10temp", "zenpower", "coretemp", "cpu_thermal")


def cpu_temp(temp_list):
    for chip in CPU_TEMP_CHIPS:
        for t in temp_list:
            if t["chip"] == chip:
                return t["current"]
    return None


def cpu_fan():
    """First non-GPU fan (needs a motherboard sensor driver such as nct6775/it87)."""
    try:
        for chip, entries in psutil.sensors_fans().items():
            if chip in ("amdgpu", "nouveau", "radeon"):
                continue
            for e in entries:
                return {"rpm": e.current, "label": e.label or chip}
    except Exception:
        pass
    return None


def top_procs(n=8):
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_info"]):
        info = p.info
        if info["memory_info"] is None:
            continue
        procs.append({"pid": info["pid"], "name": info["name"], "user": info["username"],
                      "cpu": info["cpu_percent"] or 0.0, "rss": info["memory_info"].rss})
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    return procs[:n]


class Sampler(threading.Thread):
    """Collects one snapshot per INTERVAL; all clients share the latest one."""

    def __init__(self):
        super().__init__(daemon=True)
        self.cond = threading.Condition()
        self.snapshot = None
        self.seq = 0
        self.static = {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "os": self._os_name(),
            "cpu_model": self._cpu_model(),
            "cores": psutil.cpu_count(logical=False),
            "threads": psutil.cpu_count(logical=True),
            "boot_time": psutil.boot_time(),
            "cpu_max_freq": (psutil.cpu_freq().max if psutil.cpu_freq() else None),
        }

    @staticmethod
    def _os_name():
        try:
            return platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        except Exception:
            return "Linux"

    @staticmethod
    def _cpu_model():
        for line in (_read("/proc/cpuinfo") or "").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
        return platform.processor()

    def run(self):
        psutil.cpu_percent(percpu=True)
        for p in psutil.process_iter():
            try:
                p.cpu_percent()
            except Exception:
                pass
        last_t = time.time()
        last_net = psutil.net_io_counters(pernic=True)
        last_disk = psutil.disk_io_counters()
        procs, tick = [], 0
        while True:
            time.sleep(INTERVAL)
            now = time.time()
            dt = max(now - last_t, 1e-3)
            per_core = psutil.cpu_percent(percpu=True)
            freq = psutil.cpu_freq()
            vm, sw = psutil.virtual_memory(), psutil.swap_memory()

            net_now = psutil.net_io_counters(pernic=True)
            stats = psutil.net_if_stats()
            nics = []
            for name, c in net_now.items():
                if name.startswith(SKIP_NIC_PREFIX) or name not in last_net:
                    continue
                if name in stats and not stats[name].isup:
                    continue
                prev = last_net[name]
                nics.append({"name": name,
                             "rx": (c.bytes_recv - prev.bytes_recv) / dt,
                             "tx": (c.bytes_sent - prev.bytes_sent) / dt,
                             "rx_total": c.bytes_recv, "tx_total": c.bytes_sent})
            disk_now = psutil.disk_io_counters()
            dio = {"read": 0, "write": 0}
            if disk_now and last_disk:
                dio = {"read": (disk_now.read_bytes - last_disk.read_bytes) / dt,
                       "write": (disk_now.write_bytes - last_disk.write_bytes) / dt}

            if tick % 3 == 0:  # process scan is the costliest part; refresh every 3 ticks
                procs = top_procs()
            tick += 1
            tlist = temps()
            gpu_list = gpu_stats()
            snap = {
                "ts": now,
                "cpu": {"total": sum(per_core) / len(per_core), "per_core": per_core,
                        "freq": freq.current if freq else None,
                        "load": os.getloadavg(),
                        "temp": cpu_temp(tlist), "fan": cpu_fan()},
                "mem": {"total": vm.total, "used": vm.total - vm.available,
                        "cached": getattr(vm, "cached", 0) + getattr(vm, "buffers", 0),
                        "percent": vm.percent},
                "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
                "gpus": gpu_list,
                "power": power_stats(gpu_list, now),
                "disks": disks(),
                "disk_io": dio,
                "net": nics,
                "temps": tlist,
                "procs": procs,
                "uptime": now - self.static["boot_time"],
            }
            last_t, last_net, last_disk = now, net_now, disk_now
            with self.cond:
                self.snapshot = snap
                self.seq += 1
                self.cond.notify_all()


SAMPLER = Sampler()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/info":
            self._send(200, json.dumps(SAMPLER.static).encode(), "application/json")
        elif path == "/api/snapshot":
            self._send(200, json.dumps(SAMPLER.snapshot).encode(), "application/json")
        elif path == "/api/stream":
            self.stream()
        else:
            self._send(404, b"not found", "text/plain")

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = -1
        try:
            while True:
                with SAMPLER.cond:
                    SAMPLER.cond.wait_for(lambda: SAMPLER.seq != seen, timeout=15)
                    seq, snap = SAMPLER.seq, SAMPLER.snapshot
                if seq == seen or snap is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    seen = seq
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    SAMPLER.start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print(f"Server monitor on http://{BIND}:{PORT}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

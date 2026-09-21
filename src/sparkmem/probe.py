"""Standalone, read-only Linux probe. Streamed over SSH; stdlib only, Python 3.8+."""

import argparse
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def read(path):
    try:
        return Path(path).read_text(errors="replace")
    except (OSError, ValueError):
        return ""


def kib_fields(raw):
    fields = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            parts = value.split()
            if parts and parts[0].isdigit():
                fields[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return fields


def number(raw):
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", raw or "")
    return float(match[1]) if match else None


def parse_nvidia(raw):
    devices, processes = [], {}
    for index, gpu in enumerate(ET.fromstring(raw).findall("gpu")):
        device = {
            "id": gpu.findtext("uuid") or str(index),
            "name": gpu.findtext("product_name") or "NVIDIA GPU",
            "util": number(gpu.findtext("utilization/gpu_util")),
            "temperature": number(gpu.findtext("temperature/gpu_temp")),
            "memory_total": number(gpu.findtext("fb_memory_usage/total")),
            "memory_used": number(gpu.findtext("fb_memory_usage/used")),
        }
        for field in ("memory_total", "memory_used"):
            if device[field] is not None:
                device[field] = int(device[field] * 1024**2)
        devices.append(device)
        # Includes graphics, unlike --query-compute-apps. De-duplicate MIG/context rows.
        allocations = {}
        for process in gpu.findall("processes/process_info"):
            pid = number(process.findtext("pid"))
            if pid is None:
                continue
            pid = int(pid)
            used = number(process.findtext("used_memory"))
            used = int(used * 1024**2) if used is not None else None
            entry = allocations.setdefault(pid, {"memory": None, "types": set(), "name": ""})
            if used is not None:
                entry["memory"] = max(entry["memory"] or 0, used)
            entry["types"].add(process.findtext("type") or "?")
            entry["name"] = process.findtext("process_name") or ""
        for pid, allocation in allocations.items():
            entry = processes.setdefault(
                pid,
                {"gpu_bytes": 0, "gpu_known": True, "gpu_devices": [], "gpu_type": [], "name": ""},
            )
            if allocation["memory"] is None:
                entry["gpu_known"] = False
            else:
                entry["gpu_bytes"] += allocation["memory"]
            entry["gpu_devices"].append(device["id"])
            entry["gpu_type"].extend(sorted(allocation["types"]))
            entry["name"] = allocation["name"]
    for process in processes.values():
        if not process.pop("gpu_known"):
            process["gpu_bytes"] = None
        process["gpu_type"] = "+".join(sorted(set(process["gpu_type"])))
    return devices, processes


def nvidia():
    try:
        result = subprocess.run(
            ["nvidia-smi", "-q", "-x"], capture_output=True, text=True, timeout=6
        )
        if result.returncode:
            return [], {}, "NVIDIA query failed: " + (result.stderr or result.stdout).strip()[:240]
        devices, processes = parse_nvidia(result.stdout)
        return devices, processes, ""
    except FileNotFoundError:
        return [], {}, "nvidia-smi absent (CPU-only monitoring)"
    except (OSError, subprocess.TimeoutExpired, ET.ParseError) as error:
        return [], {}, "NVIDIA telemetry unavailable: " + str(error)[:240]


def link(path):
    try:
        return os.readlink(path)
    except OSError:
        return ""


def cgroup_path(raw):
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[0] == "0" or "memory" in parts[1].split(",")):
            return parts[2]
    return ""


def process_info(path, users):
    status = read(path / "status")
    if not status:
        return None
    fields = kib_fields(status)
    name = next(
        (line.split(":", 1)[1].strip() for line in status.splitlines() if line.startswith("Name:")),
        "?",
    )
    uid = fields.get("Uid", -1)
    if uid not in users:
        try:
            users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            users[uid] = str(uid)
    stat = read(path / "stat").rpartition(") ")[2].split()
    if len(stat) < 20:
        return None
    return {
        "pid": int(path.name),
        "ppid": fields.get("PPid"),
        "name": name,
        "user": users[uid],
        "rss": fields.get("VmRSS"),
        "pss": None,
        "uss": None,
        "rss_anon": fields.get("RssAnon"),
        "rss_file": fields.get("RssFile"),
        "rss_shmem": fields.get("RssShmem"),
        "swap": fields.get("VmSwap"),
        "pss_status": "not sampled",
        "command": read(path / "cmdline").replace("\0", " ").strip(),
        "exe": link(path / "exe"),
        "cwd": link(path / "cwd"),
        "cgroup": cgroup_path(read(path / "cgroup")),
        "start_ticks": int(stat[19]),
        "cpu_ticks": int(stat[11]) + int(stat[12]),
        "gpu_bytes": None,
        "gpu_devices": [],
        "gpu_type": "",
        "cpu_percent": None,
    }


def block_kind(path, seen=None):
    """Follow dm/RAID/partition layers without guessing that every SSD is NVMe."""
    path = Path(path).resolve()
    seen = set() if seen is None else seen
    if str(path) in seen or len(seen) >= 16:
        return "unknown"
    seen = seen | {str(path)}
    if re.fullmatch(r"nvme\d+(?:c\d+)?n\d+(?:p\d+)?", path.name):
        return "nvme"
    if re.fullmatch(r"zram\d+", path.name):
        return "zram"
    try:
        slaves = list((path / "slaves").iterdir())
    except OSError:
        slaves = []
    if slaves:
        kinds = {block_kind(slave, seen) for slave in slaves}
        return kinds.pop() if len(kinds) == 1 else "unknown"
    if (path / "partition").exists():
        return block_kind(path.parent, seen)
    if re.fullmatch(r"(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)", path.name):
        return "disk"
    # Loop devices/filesystems can hide arbitrary backing storage. Stay unknown.
    return "unknown"


def swap_kind(filename):
    try:
        info = os.stat(filename)
        device = info.st_rdev if stat.S_ISBLK(info.st_mode) else info.st_dev
        return block_kind(Path("/sys/dev/block") / f"{os.major(device)}:{os.minor(device)}")
    except OSError:
        return "unknown"


def swap_storage(swaps_path="/proc/swaps", block_root="/sys/block"):
    raw = read(swaps_path)
    devices = []
    for line in raw.splitlines()[1:]:
        parts = line.rsplit(None, 4)
        if len(parts) != 5 or not parts[2].isdigit() or not parts[3].isdigit():
            continue
        filename = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), parts[0])
        devices.append(
            {
                "path": filename,
                "kind": swap_kind(filename),
                "size": int(parts[2]) * 1024,
                "used": int(parts[3]) * 1024,
            }
        )
    zram = []
    for path in Path(block_root).glob("zram*"):
        values = read(path / "mm_stat").split()
        if len(values) < 3 or not all(value.isdigit() for value in values[:3]):
            continue
        backing = read(path / "backing_dev").strip()
        bd = read(path / "bd_stat").split()
        zram.append(
            {
                "name": path.name,
                "original": int(values[0]),
                "compressed": int(values[1]),
                "physical": int(values[2]),
                "backing_bytes": int(bd[0]) * 4096 if bd and bd[0].isdigit() else None,
                "backing_kind": swap_kind(backing) if backing and backing != "none" else "none",
                "active_swap": any(Path(d["path"]).name == path.name for d in devices),
            }
        )
    return {"available": raw.startswith("Filename"), "devices": devices, "zram": zram}


def collect_cgroups(root="/sys/fs/cgroup"):
    root = Path(root)
    if not (root / "cgroup.controllers").exists():
        return [], "cgroup v2 unavailable; service memory.current unavailable"
    groups, visited = [], 0
    for directory, children, _ in os.walk(root):
        visited += 1
        path = Path(directory)
        relative = path.relative_to(root)
        if len(relative.parts) >= 12:
            children[:] = []
        if visited > 5000:
            return groups, "cgroup scan limited to 5000 directories"
        current = read(path / "memory.current").strip()
        if not relative.parts or not current.isdigit() or int(current) == 0:
            continue
        stats = dict(
            line.split()
            for line in read(path / "memory.stat").splitlines()
            if len(line.split()) == 2
        )
        limit = read(path / "memory.max").strip()
        swap = read(path / "memory.swap.current").strip()
        zswap = read(path / "memory.zswap.current").strip()
        groups.append(
            {
                "path": "/" + str(relative),
                "name": path.name,
                "current": int(current),
                "limit": int(limit) if limit.isdigit() else None,
                "swap": int(swap) if swap.isdigit() else None,
                "zswap": int(zswap) if zswap.isdigit() else None,
                "zswapped": int(stats["zswapped"]) if "zswapped" in stats else None,
                "anon": int(stats["anon"]) if "anon" in stats else None,
                "file": int(stats["file"]) if "file" in stats else None,
            }
        )
    return groups, ""


def collect(pss_limit=80, gpu=True):
    started = time.monotonic()
    memory = kib_fields(read("/proc/meminfo"))
    if not memory.get("MemTotal"):
        raise RuntimeError("Linux /proc/meminfo is required")
    devices, allocations, gpu_note = nvidia() if gpu else ([], {}, "NVIDIA collection disabled")
    processes, users = {}, {}
    skipped = 0
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            process = process_info(path, users)
            if process is not None:
                processes[process["pid"]] = process
            else:
                skipped += 1
        except (OSError, ValueError, IndexError):
            skipped += 1
    for pid, allocation in allocations.items():
        if pid not in processes:
            processes[pid] = {
                "pid": pid,
                "name": allocation["name"],
                "command": "",
                "user": "?",
                "rss": None,
                "pss": None,
                "uss": None,
                "swap": None,
                "cgroup": "",
                "start_ticks": 0,
                "cpu_ticks": 0,
                "cpu_percent": None,
                "pss_status": "process inaccessible or exited",
            }
        processes[pid].update({k: v for k, v in allocation.items() if k != "name"})
    ordered = sorted(processes.values(), key=lambda p: p.get("rss") or 0, reverse=True)
    selected = {p["pid"] for p in (ordered if pss_limit == 0 else ordered[:pss_limit])}
    selected.update(allocations)
    for pid in selected:
        process = processes[pid]
        rollup = kib_fields(read(Path("/proc") / str(pid) / "smaps_rollup"))
        if "Pss" in rollup:
            process.update(
                pss=rollup["Pss"],
                uss=rollup.get("Private_Clean", 0) + rollup.get("Private_Dirty", 0),
                pss_status="sampled",
            )
        else:
            process["pss_status"] = "permission denied, unsupported, or exited"
    groups, group_note = collect_cgroups()
    pressure = {}
    for line in read("/proc/pressure/memory").splitlines():
        parts = line.split()
        if parts:
            pressure[parts[0]] = {
                k: float(v) for k, v in (part.split("=", 1) for part in parts[1:])
            }
    return {
        "schema": 1,
        "hostname": socket.gethostname(),
        "time": time.time(),
        "monotonic": time.monotonic(),
        "boot_id": read("/proc/sys/kernel/random/boot_id").strip(),
        "clock_ticks": os.sysconf("SC_CLK_TCK"),
        "memory": memory,
        "pressure": pressure,
        "gpus": devices,
        "processes": list(processes.values()),
        "cgroups": groups,
        "swap_storage": swap_storage(),
        "notes": [note for note in (gpu_note, group_note) if note],
        "skipped_processes": skipped,
        "duration": time.monotonic() - started,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pss-limit", type=int, default=80)
    parser.add_argument("--no-gpu", action="store_true")
    options = parser.parse_args()
    print(json.dumps(collect(options.pss_limit, not options.no_gpu), separators=(",", ":")))

"""Concurrent, bounded SSH collection and derived memory views."""

import asyncio
import json
import os
import re
import shlex
import signal
import sys
import time
from dataclasses import dataclass, field
from importlib.resources import files

from .config import Host, Settings
from .memory import annotate


@dataclass
class HostState:
    host: Host
    snapshot: dict | None = None
    error: str = ""
    updated: float = 0.0
    history: list[float] = field(default_factory=list)

    def accept(self, snapshot):
        previous = self.snapshot
        if previous and previous.get("boot_id") == snapshot.get("boot_id"):
            elapsed = snapshot["monotonic"] - previous["monotonic"]
            old = {(p["pid"], p["start_ticks"]): p for p in previous["processes"]}
            for process in snapshot["processes"]:
                before = old.get((process["pid"], process["start_ticks"]))
                if before and elapsed > 0 and process["start_ticks"]:
                    delta = process["cpu_ticks"] - before["cpu_ticks"]
                    process["cpu_percent"] = max(0, 100 * delta / snapshot["clock_ticks"] / elapsed)
        self.snapshot, self.error, self.updated = snapshot, "", time.monotonic()
        annotate(snapshot, is_uma(self))
        summary = memory_summary(snapshot)
        self.history = (self.history + [summary["used_percent"]])[-60:]


def memory_summary(snapshot):
    mem = snapshot["memory"]
    total = mem["MemTotal"]
    available = mem.get("MemAvailable")
    used = max(0, total - available) if available is not None else None
    gpu_values = [p["gpu_bytes"] for p in snapshot["processes"] if p.get("gpu_devices")]
    return {
        "total": total,
        "available": available,
        "used": used,
        "used_percent": used / total * 100 if used is not None else 0,
        "cache": max(
            0,
            mem.get("Cached", 0)
            + mem.get("Buffers", 0)
            + mem.get("SReclaimable", 0)
            - mem.get("Shmem", 0),
        ),
        "swap": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
        "gpu": sum(v for v in gpu_values if v is not None) if gpu_values else None,
        "gpu_partial": any(v is None for v in gpu_values),
    }


def is_uma(state):
    if state.host.uma is not None:
        return state.host.uma
    return any("GB10" in gpu["name"] for gpu in (state.snapshot or {}).get("gpus", []))


async def sample(host: Host, settings: Settings):
    source = files("sparkmem").joinpath("probe.py").read_bytes()
    arguments = ["-", "--pss-limit", str(settings.pss_limit)]
    if not host.gpu:
        arguments.append("--no-gpu")
    if host.local:
        command = [sys.executable, *arguments]
    else:
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=6",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "--",
            host.target or host.name,
            shlex.join([host.python, *arguments]),
        ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(source), settings.timeout)
    except (TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    if process.returncode:
        message = (stderr or stdout).decode(errors="replace").strip()
        raise RuntimeError(message[-600:] or f"Collector exited {process.returncode}")
    try:
        snapshot = json.loads(stdout)
        if snapshot.get("schema") != 1 or not snapshot.get("memory", {}).get("MemTotal"):
            raise ValueError("Unsupported or incomplete probe response")
        return snapshot
    except (ValueError, AttributeError) as error:
        raise RuntimeError(
            f"Invalid probe JSON (check remote shell startup output): {error}"
        ) from error


async def refresh(state, settings):
    try:
        state.accept(await sample(state.host, settings))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        state.error = (
            f"Timed out after {settings.timeout:g}s"
            if isinstance(error, TimeoutError)
            else str(error)
        )


def label_for(process, labels):
    haystack = " ".join(str(process.get(key) or "") for key in ("command", "name", "exe", "cgroup"))
    for label in labels:
        if re.search(label["pattern"], haystack, re.IGNORECASE):
            return label["name"]
    cgroup = process.get("cgroup", "")
    services = [
        part
        for part in cgroup.split("/")
        if part.endswith(".service") or part.startswith("docker-")
    ]
    return services[-1] if services else process.get("name", "?")


def process_rows(states, settings, query="", host_filter="", gpu_only=False, sort=None):
    rows = []
    for name, state in states.items():
        if not state.snapshot or (host_filter and host_filter != name):
            continue
        for process in state.snapshot["processes"]:
            if gpu_only and not process.get("gpu_devices"):
                continue
            row = dict(process, host=name, label=label_for(process, settings.labels))
            if query.casefold() not in " ".join(str(v) for v in row.values()).casefold():
                continue
            row["key"] = f"{name}:{row['pid']}:{row['start_ticks']}"
            rows.append(row)
    key = {
        "gpu": "gpu_bytes",
        "cpu": "cpu_percent",
        "ram": "ram_estimate",
        "nvme": "nvme_estimate",
    }.get(sort or settings.sort, sort or settings.sort)

    def value(row):
        if key == "impact":
            return max(
                row["pss"] if row.get("pss") is not None else row.get("rss") or 0,
                row.get("gpu_bytes") or 0,
            )
        return row.get(key) if row.get(key) is not None else -1

    return sorted(rows, key=lambda row: (-value(row), row["host"], row["pid"]))

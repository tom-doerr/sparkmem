"""Synthetic snapshots for offline exploration, UI tests and public screenshots."""

import time


def snapshots(count=4):
    gib = 1024**3
    result = []
    workloads = [
        (
            "vLLM inference",
            "python -m vllm.entrypoints.openai.api_server --model example/Model-70B",
            65,
            72,
        ),
        ("Embeddings", "python -m infinity_emb --model-id example/embeddings", 3, 4),
        ("Training", "python train.py --model example/vision --batch-size 4", 9, 12),
        ("PostgreSQL", "postgres -D /var/lib/postgresql/data", 4, 0),
    ]
    for index in range(count):
        nas = index == 3
        total, available = (32, 9) if nas else (120, (24, 12, 8)[index % 3])
        processes = []
        host_workloads = (
            workloads
            if not nas
            else [
                ("PostgreSQL", "postgres -D /var/lib/postgresql/data", 4, 0),
                ("Redis", "redis-server *:6379", 1, 0),
                ("Syncthing", "syncthing serve --no-browser", 2, 0),
                ("Backup", "python backup.py --repository /srv/backup", 3, 0),
            ]
        )
        for offset, (name, command, rss, gpu) in enumerate(host_workloads):
            gpu = 0 if nas else gpu
            processes.append(
                {
                    "pid": 2400 + index * 100 + offset,
                    "ppid": 1,
                    "name": name,
                    "user": "demo",
                    "command": command,
                    "exe": "/opt/venv/bin/python",
                    "cwd": "/srv/models",
                    "rss": rss * gib,
                    "pss": int(rss * 0.9 * gib),
                    "uss": int(rss * 0.8 * gib),
                    "pss_status": "sampled",
                    "swap": offset * gib // 4,
                    "start_ticks": 1234,
                    "cpu_ticks": 0,
                    "cpu_percent": float(offset * 12),
                    "gpu_bytes": gpu * gib if gpu else None,
                    "gpu_devices": ["GPU-DEMO"] if gpu else [],
                    "gpu_type": "C" if gpu else "",
                    "cgroup": f"/system.slice/{name.lower().replace(' ', '-')}.service",
                }
            )
        result.append(
            {
                "schema": 1,
                "hostname": f"demo-{index}",
                "time": time.time(),
                "monotonic": time.monotonic(),
                "boot_id": "demo",
                "clock_ticks": 100,
                "memory": {
                    "MemTotal": total * gib,
                    "MemAvailable": available * gib,
                    "MemFree": 2 * gib,
                    "Cached": 5 * gib,
                    "SwapTotal": 16 * gib,
                    "SwapFree": 12 * gib,
                    "SReclaimable": gib,
                    "Shmem": gib,
                },
                "pressure": {"some": {"avg10": 2.4 if index == 2 else 0}},
                "gpus": []
                if nas
                else [
                    {
                        "name": "NVIDIA GB10",
                        "util": 96,
                        "temperature": 68,
                        "id": "GPU-DEMO",
                        "memory_total": None,
                        "memory_used": None,
                    }
                ],
                "processes": processes,
                "cgroups": [
                    {
                        "name": "postgres.service",
                        "path": "/system.slice/postgres.service",
                        "current": 5 * gib,
                        "anon": 3 * gib,
                        "file": 2 * gib,
                        "swap": 0,
                        "limit": None,
                    }
                ],
                "notes": [],
                "duration": 0.25,
                "skipped_processes": 0,
            }
        )
    return result

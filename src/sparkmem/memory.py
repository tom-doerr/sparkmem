"""Explicitly approximate per-process physical placement; measured source counters stay intact.

Linux exposes neither per-PID compressed sizes nor CPU/GPU overlap. Ratios below
are attribution heuristics, never measurements, and are not additive fleet totals.
"""


def host_compression(snapshot):
    memory = snapshot["memory"]
    storage = snapshot.get("swap_storage", {})
    zram = [z for z in storage.get("zram", []) if z.get("active_swap")]
    expected = {
        d["path"].rsplit("/", 1)[-1] for d in storage.get("devices", []) if d["kind"] == "zram"
    }
    physical = memory.get("Zswap")
    if physical is not None and storage.get("available") and expected <= {z["name"] for z in zram}:
        physical += sum(z["physical"] for z in zram)
    else:
        physical = None
    return {
        "physical": physical,
        "zram": zram,
        "zswap_physical": memory.get("Zswap"),
        "zswap_logical": memory.get("Zswapped"),
    }


class MemoryAccounting:
    def __init__(self, snapshot, uma):
        self.snapshot = snapshot
        self.uma = uma
        self.memory = snapshot["memory"]
        self.storage = snapshot.get("swap_storage", {})
        self.groups = {g["path"]: g for g in snapshot.get("cgroups", [])}
        self.compression = host_compression(snapshot)
        self.swap_used = max(0, self.memory.get("SwapTotal", 0) - self.memory.get("SwapFree", 0))
        self.active = [d for d in self.storage.get("devices", []) if d["used"] > 0]

    def compression_scope(self, process):
        path = process.get("cgroup", "")
        while path and path != "/":
            group = self.groups.get(path)
            if (
                group
                and (group.get("swap") or 0) > 0
                and group.get("zswap") is not None
                and group.get("zswapped") is not None
            ):
                return group["swap"], group["zswap"], group["zswapped"], "cgroup " + path
            path = path.rpartition("/")[0]
        return (
            self.swap_used,
            self.memory.get("Zswap"),
            self.memory.get("Zswapped"),
            "host average (no usable cgroup compression counters)",
        )

    def swapped_placement(self, process):
        swap = process.get("swap")
        if swap is None:
            return None, None, "unavailable: process swap is unreadable"
        if swap == 0:
            return 0, 0, "no private anonymous swap reported (shared/tmpfs swap excluded)"
        if not self.storage.get("available"):
            return None, None, "unavailable: swap device inventory is unreadable"
        if not self.active or self.swap_used == 0:
            return None, None, "unavailable: process and host swap samples disagree"
        active_zram = [d for d in self.active if d["kind"] == "zram"]
        if active_zram:
            # zswap in front of zram is a second compression layer. No exported
            # per-device zswap split lets us safely undo both layers.
            if self.memory.get("Zswapped") != 0:
                return None, None, "unavailable: mixed zswap/zram placement is not exposed"
            names = {d["path"].rsplit("/", 1)[-1] for d in active_zram}
            zrams = [z for z in self.compression["zram"] if z["name"] in names]
            if len(zrams) != len(active_zram):
                return None, None, "unavailable: active zram statistics are incomplete"
            physical = sum(z["physical"] for z in zrams)
            fraction = swap / self.swap_used
            nvme = sum(d["used"] for d in self.active if d["kind"] == "nvme")
            known = all(d["kind"] in ("nvme", "disk", "zram") for d in self.active)
            for zram in zrams:
                backing = zram.get("backing_bytes")
                if backing is None:
                    known = False
                elif backing and zram["backing_kind"] == "nvme":
                    nvme += backing
                elif backing and zram["backing_kind"] not in ("disk", "none"):
                    known = False
            return (
                round(fraction * physical),
                round(fraction * nvme) if known else None,
                "host average: swap share × zram physical pool / NVMe payload; "
                "includes zram allocator overhead, not a per-process measurement",
            )
        scope_swap, compressed, logical, basis = self.compression_scope(process)
        if compressed is None or logical is None or not scope_swap:
            return None, None, "unavailable: compressed swap counters are unreadable"
        fraction = swap / scope_swap
        compressed_est = round(fraction * compressed)
        disk_est = round(swap * (1 - min(1, logical / scope_swap)))
        kinds = {d["kind"] for d in self.active}
        if kinds == {"nvme"}:
            nvme = disk_est
        elif kinds == {"disk"}:
            nvme = 0
        else:
            nvme = None
            basis += "; NVMe unknown: mixed or unidentified backing devices"
        return compressed_est, nvme, basis

    def process(self, process):
        compressed, nvme, basis = self.swapped_placement(process)
        resident = process.get("pss")
        resident_source = "PSS"
        if resident is None:
            resident, resident_source = (
                process.get("rss"),
                "RSS fallback (shared pages counted in full)",
            )
        gpu = process.get("gpu_bytes")
        if self.uma and process.get("gpu_devices"):
            resident = max(resident, gpu) if resident is not None and gpu is not None else None
            resident_source = "max(" + resident_source + ", GPU allocation) on UMA"
        ram = resident + compressed if resident is not None and compressed is not None else None
        return {
            "ram_estimate": ram,
            "nvme_estimate": nvme,
            "compressed_ram_estimate": compressed,
            "estimate_basis": basis,
            "resident_basis": resident_source,
        }


def annotate(snapshot, uma):
    accounting = MemoryAccounting(snapshot, uma)
    for process in snapshot["processes"]:
        process.update(accounting.process(process))

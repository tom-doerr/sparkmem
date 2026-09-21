import copy

from sparkmem.collector import HostState, is_uma, memory_summary, process_rows
from sparkmem.config import Host, Settings
from sparkmem.demo import snapshots


def test_uma_never_adds_gpu_to_physical_memory():
    snapshot = snapshots(1)[0]
    snapshot["memory"] = {
        "MemTotal": 1000,
        "MemAvailable": 300,
        "Cached": 80,
        "Buffers": 10,
        "SReclaimable": 20,
        "Shmem": 40,
    }
    summary = memory_summary(snapshot)
    assert summary["used"] == 700
    assert summary["available"] == 300
    assert summary["cache"] == 70
    assert summary["gpu"] > summary["total"]  # Independent driver counters, not added.
    state = HostState(Host("spark"), snapshot)
    assert is_uma(state)
    state.host.uma = False
    assert not is_uma(state)


def test_unknown_gpu_is_not_silently_zero():
    snapshot = snapshots(1)[0]
    snapshot["processes"][0]["gpu_bytes"] = None
    assert memory_summary(snapshot)["gpu_partial"]
    snapshot["memory"].pop("MemAvailable")
    assert memory_summary(snapshot)["used"] is None


def test_cpu_deltas_skip_reused_pids_and_reboots():
    state = HostState(Host("test"))
    before = snapshots(1)[0]
    before["monotonic"] = 100
    state.accept(before)
    after = copy.deepcopy(before)
    after["monotonic"] = 102
    after["processes"][0]["cpu_ticks"] += 300
    after["processes"][1]["start_ticks"] += 1
    after["processes"][1]["cpu_percent"] = None
    state.accept(after)
    assert state.snapshot["processes"][0]["cpu_percent"] == 150
    assert state.snapshot["processes"][1]["cpu_percent"] is None
    reboot = copy.deepcopy(after)
    reboot["boot_id"] = "reboot"
    reboot["processes"][0]["cpu_percent"] = None
    state.accept(reboot)
    assert state.snapshot["processes"][0]["cpu_percent"] is None


def test_filter_label_and_numeric_sort():
    settings = Settings(labels=[{"pattern": "Model-70B", "name": "Big model"}])
    state = HostState(Host("spark"), snapshots(1)[0])
    states = {"spark": state}
    rows = process_rows(states, settings, query="big MODEL", gpu_only=True)
    assert len(rows) == 1
    assert rows[0]["label"] == "Big model"
    assert not process_rows(states, settings, host_filter="missing")
    rows = process_rows(states, settings, sort="gpu")
    assert rows[0]["gpu_bytes"] > rows[1]["gpu_bytes"]
    assert rows[-1]["gpu_bytes"] is None
    state.snapshot["processes"][0]["pss"] = None
    assert process_rows(states, settings)[0]["pid"] == 2400

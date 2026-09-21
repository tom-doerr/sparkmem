import copy

from sparkmem.collector import HostState, process_rows
from sparkmem.config import Host, Settings
from sparkmem.demo import snapshots
from sparkmem.memory import MemoryAccounting, annotate, host_compression


def fixture():
    process = {
        "pid": 42,
        "pss": 100,
        "rss": 200,
        "gpu_bytes": 300,
        "gpu_devices": ["gpu"],
        "swap": 100,
        "cgroup": "/service/child",
    }
    snapshot = {
        "memory": {
            "MemTotal": 5000,
            "SwapTotal": 2000,
            "SwapFree": 1000,
            "Zswap": 80,
            "Zswapped": 400,
        },
        "swap_storage": {
            "available": True,
            "devices": [{"path": "/swap.img", "kind": "nvme", "used": 1000}],
            "zram": [],
        },
        "cgroups": [{"path": "/service", "swap": 400, "zswap": 40, "zswapped": 200}],
        "processes": [process],
    }
    return snapshot, process


def test_cgroup_compression_and_uma_overlap_are_not_added_twice():
    snapshot, process = fixture()
    result = MemoryAccounting(snapshot, True).process(process)
    assert result["compressed_ram_estimate"] == 10
    assert result["ram_estimate"] == 310  # max(PSS=100, GPU=300) + compressed=10
    assert result["nvme_estimate"] == 50  # 100 swap × (1 − 200 / 400)
    assert result["estimate_basis"] == "cgroup /service"
    assert "max(PSS" in result["resident_basis"]
    annotate(snapshot, True)
    first = copy.deepcopy(process)
    annotate(snapshot, True)
    assert process == first


def test_discrete_gpu_not_counted_as_ram_and_rss_fallback():
    snapshot, process = fixture()
    assert MemoryAccounting(snapshot, False).process(process)["ram_estimate"] == 110
    process["pss"] = None
    result = MemoryAccounting(snapshot, False).process(process)
    assert result["ram_estimate"] == 210
    assert "RSS fallback" in result["resident_basis"]


def test_host_average_is_explicit_fallback():
    snapshot, process = fixture()
    snapshot["cgroups"] = []
    result = MemoryAccounting(snapshot, True).process(process)
    assert result["compressed_ram_estimate"] == 8
    assert result["nvme_estimate"] == 60
    assert "host average" in result["estimate_basis"]


def test_missing_counters_are_unknown_not_zero():
    snapshot, process = fixture()
    snapshot["swap_storage"]["available"] = False
    result = MemoryAccounting(snapshot, True).process(process)
    assert result["ram_estimate"] is None
    assert result["nvme_estimate"] is None
    assert host_compression(snapshot)["physical"] is None
    snapshot["swap_storage"]["available"] = True
    process["gpu_bytes"] = None
    assert MemoryAccounting(snapshot, True).process(process)["ram_estimate"] is None


def test_mixed_or_unidentified_disks_do_not_get_called_nvme():
    snapshot, process = fixture()
    snapshot["swap_storage"]["devices"].append({"path": "/dev/sda", "kind": "disk", "used": 100})
    result = MemoryAccounting(snapshot, True).process(process)
    assert result["nvme_estimate"] is None
    assert result["ram_estimate"] == 310
    snapshot["swap_storage"]["devices"] = [{"path": "/dev/sda", "kind": "disk", "used": 1000}]
    assert MemoryAccounting(snapshot, True).process(process)["nvme_estimate"] == 0


def test_zram_uses_physical_pool_including_overhead_and_writeback():
    snapshot, process = fixture()
    snapshot["memory"].update(Zswap=0, Zswapped=0)
    snapshot["swap_storage"] = {
        "available": True,
        "devices": [
            {"path": "/dev/zram0", "kind": "zram", "used": 800},
            {"path": "/swap.img", "kind": "nvme", "used": 200},
        ],
        "zram": [
            {
                "name": "zram0",
                "physical": 120,
                "compressed": 90,
                "original": 500,
                "active_swap": True,
                "backing_bytes": 40,
                "backing_kind": "nvme",
            }
        ],
    }
    result = MemoryAccounting(snapshot, True).process(process)
    assert result["compressed_ram_estimate"] == 12  # physical=120, not payload=90
    assert result["ram_estimate"] == 312
    assert result["nvme_estimate"] == 24  # 10% × (200 NVMe + 40 zram writeback)
    assert host_compression(snapshot)["physical"] == 120
    snapshot["memory"]["Zswapped"] = 40
    assert MemoryAccounting(snapshot, True).process(process)["ram_estimate"] is None
    snapshot["swap_storage"]["zram"] = []
    assert host_compression(snapshot)["physical"] is None


def test_no_private_swap_needs_no_compression_guess():
    snapshot, process = fixture()
    process["swap"] = 0
    result = MemoryAccounting(snapshot, True).process(process)
    assert result["ram_estimate"] == 300
    assert result["nvme_estimate"] == 0
    process["swap"] = None
    assert MemoryAccounting(snapshot, True).process(process)["nvme_estimate"] is None


def test_estimated_columns_sort_numerically_with_unknowns_last():
    state = HostState(Host("spark", uma=True))
    state.accept(snapshots(1)[0])
    state.snapshot["processes"][0]["nvme_estimate"] = None
    rows = process_rows({"spark": state}, Settings(), sort="nvme")
    assert rows[0]["nvme_estimate"] > rows[1]["nvme_estimate"]
    assert rows[-1]["nvme_estimate"] is None
    rows = process_rows({"spark": state}, Settings(), sort="ram")
    assert rows[0]["ram_estimate"] >= rows[1]["ram_estimate"]

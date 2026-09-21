import ast
from pathlib import Path

from sparkmem import probe

XML = """<nvidia_smi_log><gpu><uuid>GPU-0</uuid><product_name>NVIDIA GB10</product_name>
<fb_memory_usage><total>N/A</total><used>N/A</used></fb_memory_usage>
<utilization><gpu_util>96 %</gpu_util></utilization>
<processes>
<process_info><pid>100</pid><type>C</type><process_name>python</process_name><used_memory>20434 MiB</used_memory></process_info>
<process_info><pid>100</pid><type>G</type><process_name>python</process_name><used_memory>20434 MiB</used_memory></process_info>
<process_info><pid>200</pid><type>G</type><process_name>Xorg</process_name><used_memory>18 MiB</used_memory></process_info>
<process_info><pid>300</pid><type>C</type><process_name>unknown</process_name><used_memory>N/A</used_memory></process_info>
</processes></gpu></nvidia_smi_log>"""


def test_nvidia_uma_graphics_deduplication_and_unknowns():
    devices, processes = probe.parse_nvidia(XML)
    assert devices[0]["memory_total"] is None
    assert devices[0]["util"] == 96
    assert processes[100]["gpu_bytes"] == 20434 * 1024**2
    assert processes[100]["gpu_type"] == "C+G"
    assert processes[200]["gpu_bytes"] == 18 * 1024**2
    assert processes[300]["gpu_bytes"] is None


def test_multiple_devices_sum_independent_allocations():
    gpu = XML.split("<gpu>")[1].split("</gpu>")[0]
    raw = f"<nvidia_smi_log><gpu>{gpu}</gpu><gpu>{gpu.replace('GPU-0', 'GPU-1')}</gpu></nvidia_smi_log>"
    _, processes = probe.parse_nvidia(raw)
    assert processes[100]["gpu_bytes"] == 2 * 20434 * 1024**2
    assert processes[100]["gpu_devices"] == ["GPU-0", "GPU-1"]


def test_proc_units_and_cgroup_versions():
    assert probe.kib_fields("Name: x\nVmRSS: 12 kB\nUid: 1000 1000 1000 1000\n") == {
        "VmRSS": 12288,
        "Uid": 1000,
    }
    assert (
        probe.cgroup_path("0::/system.slice/docker-abc.scope\n") == "/system.slice/docker-abc.scope"
    )
    assert probe.cgroup_path("3:cpu:/one\n5:memory:/two\n") == "/two"
    assert probe.read("/definitely/not/a/real/path") == ""


def test_cgroup_hierarchy_remains_separate(tmp_path):
    (tmp_path / "cgroup.controllers").write_text("memory")
    for path, value in ((tmp_path / "parent", 100), (tmp_path / "parent/child", 60)):
        path.mkdir(parents=True)
        (path / "memory.current").write_text(str(value))
        (path / "memory.max").write_text("max")
        (path / "memory.stat").write_text("anon 40\nfile 20\n")
    groups, note = probe.collect_cgroups(tmp_path)
    assert note == ""
    assert sorted(group["current"] for group in groups) == [60, 100]
    assert all(group["limit"] is None for group in groups)


def test_partial_permissions_keep_gpu_only_process(monkeypatch):
    real_read = probe.read

    def limited_read(path):
        if str(path).endswith("smaps_rollup"):
            return ""
        return real_read(path)

    monkeypatch.setattr(probe, "read", limited_read)
    monkeypatch.setattr(
        probe,
        "nvidia",
        lambda: (
            [],
            {
                99999999: {
                    "name": "hidden",
                    "gpu_bytes": 123,
                    "gpu_devices": ["gpu"],
                    "gpu_type": "C",
                }
            },
            "",
        ),
    )
    snapshot = probe.collect(pss_limit=1)
    hidden = next(p for p in snapshot["processes"] if p["pid"] == 99999999)
    assert hidden["gpu_bytes"] == 123
    assert hidden["rss"] is None and hidden["pss"] is None
    assert snapshot["memory"]["MemTotal"] > 0


def test_remote_probe_parses_on_python38():
    ast.parse(Path(probe.__file__).read_text(), feature_version=(3, 8))


def test_detect_nvme_through_dm_and_keep_unknown_backing_unknown(tmp_path):
    nvme = tmp_path / "nvme1n1p1"
    nvme.mkdir()
    dm = tmp_path / "dm-2"
    (dm / "slaves").mkdir(parents=True)
    (dm / "slaves/nvme1n1p1").symlink_to(nvme)
    assert probe.block_kind(dm) == "nvme"
    assert probe.block_kind(tmp_path / "zram0") == "zram"
    assert probe.block_kind(tmp_path / "sda") == "disk"
    assert probe.block_kind(tmp_path / "loop0") == "unknown"
    (dm / "slaves/sda").symlink_to(tmp_path / "sda")
    assert probe.block_kind(dm) == "unknown"


def test_swap_inventory_collects_real_zram_overhead_and_four_kib_writeback(tmp_path, monkeypatch):
    swaps = tmp_path / "swaps"
    swaps.write_text(
        "Filename Type Size Used Priority\n/dev/zram0 partition 1000 500 100\n/swap\\040file file 2000 20 -2\n"
    )
    zram = tmp_path / "zram0"
    zram.mkdir()
    (zram / "mm_stat").write_text("512000 128000 150000 0 0 0 0 0")
    (zram / "bd_stat").write_text("3 5 7")
    (zram / "backing_dev").write_text("/dev/nvme0n1p2")
    monkeypatch.setattr(probe, "swap_kind", lambda path: "zram" if "zram" in path else "nvme")
    result = probe.swap_storage(swaps, tmp_path)
    assert result["available"]
    assert result["devices"][1]["path"] == "/swap file"
    assert result["devices"][1]["used"] == 20 * 1024
    assert result["zram"][0]["physical"] == 150000
    assert result["zram"][0]["backing_bytes"] == 3 * 4096
    assert result["zram"][0]["active_swap"]

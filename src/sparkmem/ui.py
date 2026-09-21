"""Textual presentation. Collection and accounting live in separate modules."""

import asyncio
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

from .collector import HostState, is_uma, memory_summary, process_rows, refresh
from .config import SORTS, Settings


def size(value):
    if value is None:
        return "—"
    if value == 0:
        return "0"
    for unit, divisor in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if value >= divisor:
            return f"{value / divisor:.1f}{unit}"
    return str(value)


def clean(value):
    # Treat commands, hostnames and diagnostics as text, never terminal control/markup.
    return "".join(c if c.isprintable() or c == "\n" else " " for c in str(value))


def trend(values):
    return "".join("▁▂▃▄▅▆▇█"[min(7, max(0, int(v * 7 / 100)))] for v in values[-20:])


class Details(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]
    CSS = """
    Details { align: center middle; background: $background 70%; }
    #detail-scroll { width: 90%; height: 85%; border: round $accent; padding: 1 2; }
    #detail-body { height: auto; }
    """

    def __init__(self, content):
        super().__init__()
        self.content = content

    def compose(self):
        with VerticalScroll(id="detail-scroll"):
            yield Static(Text(clean(self.content)), id="detail-body")
        yield Footer()


class SparkMem(App):
    TITLE = "sparkmem"
    SUB_TITLE = "memory across your machines"
    CSS_PATH = "style.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "search", "Search"),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("h", "next_host", "Host"),
        Binding("g", "gpu", "GPU only"),
        Binding("s", "sort", "Sort"),
        Binding("c", "groups", "Services"),
        Binding("space", "pause", "Pause"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
        Binding("1", "host_number(0)", "Host 1", show=False),
        Binding("2", "host_number(1)", "Host 2", show=False),
        Binding("3", "host_number(2)", "Host 3", show=False),
        Binding("4", "host_number(3)", "Host 4", show=False),
        Binding("0", "all_hosts", "All hosts", show=False),
    ]

    def __init__(self, settings: Settings, demo=False):
        super().__init__()
        self.settings = settings
        self.demo = demo
        self.states = {host.name: HostState(host) for host in settings.hosts}
        self.host_filter = ""
        self.gpu_only = False
        self.group_view = False
        self.sort_key = settings.sort
        self.paused = False
        self.query = ""
        self.tasks = {}
        self.row_data = {}
        self.theme = settings.theme

    def compose(self) -> ComposeResult:
        yield Header()
        with Grid(id="hosts"):
            for index, _ in enumerate(self.states):
                yield Static("Connecting…", id=f"host-{index}", classes="host-card")
        yield Static("", id="status")
        yield Input(
            placeholder="Search command, model, service, user, PID, or host…  (Esc clears)",
            id="search",
        )
        yield DataTable(id="processes", cursor_type="row", zebra_stripes=True)
        yield Static(
            "UMA: system RAM is shared. PSS/RSS and GPU allocations may overlap; do not add them.  Units: KiB/MiB/GiB",
            id="legend",
        )
        yield Footer()

    def on_mount(self):
        self.rebuild_columns()
        self.query_one(DataTable).focus()
        if self.demo:
            from .demo import snapshots

            for state, snapshot in zip(
                self.states.values(), snapshots(len(self.states)), strict=True
            ):
                state.accept(snapshot)
        self.paint()
        self.set_interval(1, self.paint_cards)
        self.set_interval(self.settings.interval, self.poll)
        self.poll()
        self.resize_cards(self.size.width)

    def on_resize(self, event):
        if self.is_mounted:
            self.resize_cards(event.size.width)

    def resize_cards(self, width):
        columns = min(len(self.states), 4 if width >= 150 else 2 if width >= 80 else 1)
        grid = self.query_one("#hosts", Grid)
        grid.styles.grid_size_columns = columns
        grid.styles.height = min(16, ((len(self.states) + columns - 1) // columns) * 7)

    def poll(self, force=False):
        if self.demo or (self.paused and not force):
            return
        for name, state in self.states.items():
            if name not in self.tasks or self.tasks[name].done():
                self.tasks[name] = asyncio.create_task(self.update_host(state))

    async def update_host(self, state):
        await refresh(state, self.settings)
        self.paint()

    async def on_unmount(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def paint_cards(self):
        if not self.is_mounted:
            return
        for index, (name, state) in enumerate(self.states.items()):
            widget = self.query_one(f"#host-{index}", Static)
            mode = "UMA" if is_uma(state) else "RAM"
            age = time.monotonic() - state.updated if state.updated else 0
            stale = bool(
                state.snapshot
                and (state.error or age > self.settings.interval * 2 + self.settings.timeout)
            )
            status = (
                "STALE"
                if stale
                else "OFFLINE"
                if state.error
                else "LIVE"
                if state.snapshot
                else "CONNECTING"
            )
            color = "red" if state.error else "yellow" if stale else "cyan"
            text = Text()
            text.append(clean(f"{index + 1} {name}  {mode}  {status}"), style=f"bold {color}")
            if state.snapshot:
                summary = memory_summary(state.snapshot)
                pct = summary["used_percent"]
                bar = int(pct / 100 * 16)
                text.append(
                    f"\n{'━' * bar}{'─' * (16 - bar)} {pct:.0f}%",
                    style="red" if pct > 92 else "green",
                )
                text.append(f"  {size(summary['available'])} available")
                text.append(
                    f"\nUsed {size(summary['used'])}/{size(summary['total'])}  Cache≈{size(summary['cache'])}"
                )
                gpu_text = size(summary["gpu"]) + ("+?" if summary["gpu_partial"] else "")
                text.append(f"\nGPU alloc {gpu_text}  Swap {size(summary['swap'])}")
                psi = state.snapshot.get("pressure", {}).get("some", {}).get("avg10")
                pressure = f"{psi:.1f}%" if psi is not None else "—"
                text.append(
                    f"\nPSI {pressure}  {age:.0f}s ago  {trend(state.history)}", style="dim"
                )
                if state.error:
                    text.append("\n" + clean(state.error.splitlines()[-1]), style="red")
            elif state.error:
                text.append("\n" + clean(state.error), style="red")
            widget.update(text)
            widget.set_class(name == self.host_filter, "selected")

    def rebuild_columns(self):
        table = self.query_one(DataTable)
        table.clear(columns=True)
        if self.group_view:
            columns = [
                ("host", "Host"),
                ("current", "Charged"),
                ("anon", "Anon"),
                ("file", "File cache"),
                ("swap", "Swap"),
                ("limit", "Limit"),
                ("path", "Service / container cgroup"),
            ]
        else:
            titles = {
                "host": "Host",
                "pid": "PID",
                "user": "User",
                "label": "Workload",
                "pss": "PSS",
                "rss": "RSS",
                "gpu": "GPU alloc",
                "swap": "Swap",
                "cpu": "CPU %",
                "command": "Command / model",
                "cgroup": "Service / container cgroup",
            }
            columns = [(key, titles[key]) for key in self.settings.columns]
        widths = {
            "host": 12,
            "pid": 8,
            "user": 10,
            "label": 24,
            "command": 90,
            "cgroup": 70,
            "path": 100,
            "pss": 8,
            "rss": 8,
            "gpu": 10,
            "swap": 8,
            "cpu": 7,
            "current": 9,
            "anon": 8,
            "file": 10,
            "limit": 8,
        }
        for key, title in columns:
            table.add_column(title, key=key, width=widths[key])

    def paint(self):
        if not self.is_mounted:
            return
        self.paint_cards()
        table = self.query_one(DataTable)
        old_index = table.cursor_row
        old_key = (
            table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            if table.row_count
            else None
        )
        scroll_x, scroll_y = table.scroll_x, table.scroll_y
        table.clear()
        self.row_data = {}
        if self.group_view:
            rows = []
            for name, state in self.states.items():
                if not state.snapshot or (self.host_filter and name != self.host_filter):
                    continue
                for group in state.snapshot["cgroups"]:
                    if self.query.casefold() in (name + " " + group["path"]).casefold():
                        rows.append(dict(group, host=name, key=name + ":" + group["path"]))
            rows.sort(key=lambda row: row["current"], reverse=True)
            for row in rows:
                self.row_data[row["key"]] = row
                values = [
                    row["host"],
                    *[size(row.get(key)) for key in ("current", "anon", "file", "swap", "limit")],
                    row["path"],
                ]
                table.add_row(*[Text(clean(v)) for v in values], key=row["key"])
        else:
            rows = process_rows(
                self.states,
                self.settings,
                self.query,
                self.host_filter,
                self.gpu_only,
                self.sort_key,
            )
            for row in rows:
                self.row_data[row["key"]] = row
                values = []
                for key in self.settings.columns:
                    if key in ("pss", "rss", "swap", "gpu"):
                        value = size(row.get("gpu_bytes" if key == "gpu" else key))
                    elif key == "cpu":
                        value = (
                            f"{row['cpu_percent']:.1f}"
                            if row.get("cpu_percent") is not None
                            else "—"
                        )
                    elif key == "command":
                        value = row.get("command") or row.get("exe") or row.get("name", "?")
                    else:
                        value = str(row.get(key, ""))
                    values.append(
                        Text(
                            clean(value).replace("\n", " "),
                            style="cyan" if key == "gpu" and row.get("gpu_devices") else "",
                        )
                    )
                table.add_row(*values, key=row["key"])
        if table.row_count:
            keys = list(self.row_data)
            index = (
                keys.index(old_key) if old_key in self.row_data else min(old_index, len(keys) - 1)
            )
            table.move_cursor(row=index, animate=False, scroll=False)
            table.scroll_to(x=scroll_x, y=scroll_y, animate=False, force=True)
        live = sum(bool(state.snapshot and not state.error) for state in self.states.values())
        mode = (
            "SERVICES · charged memory (parents include children)"
            if self.group_view
            else f"PROCESSES · sort: {self.sort_key}"
        )
        status = f"{'DEMO · ' if self.demo else ''}{'PAUSED · ' if self.paused else ''}{live}/{len(self.states)} connected  |  {self.host_filter or 'all hosts'}  |  {mode}  |  {len(rows)} rows"
        if self.gpu_only and not self.group_view:
            status += "  |  GPU only"
        self.query_one("#status", Static).update(Text(clean(status)))

    @on(Input.Changed, "#search")
    def search_changed(self, event):
        self.query = event.value
        self.paint()

    @on(Input.Submitted, "#search")
    def search_submitted(self):
        self.query_one(DataTable).focus()

    @on(DataTable.HeaderSelected)
    def header_selected(self, event):
        key = event.column_key.value
        if not self.group_view and key in SORTS:
            self.sort_key = key
            self.paint()

    @on(DataTable.RowSelected)
    def selected(self, event):
        row = self.row_data.get(event.row_key.value)
        if not row:
            return
        state = self.states[row["host"]]
        snapshot = state.snapshot
        lines = [f"{row['host']}  ·  {'UMA' if is_uma(state) else 'RAM'}  ·  Esc closes", ""]
        if self.group_view:
            lines += [
                row["path"],
                "",
                f"Charged: {size(row['current'])}   Anon: {size(row['anon'])}   File: {size(row['file'])}",
                f"Swap: {size(row['swap'])}   Limit: {size(row['limit'])}",
                "",
                "memory.current includes descendants and charged page cache.",
                "Parent and child rows overlap. Do not sum them.",
            ]
        else:
            lines += [
                f"{row['label']} · PID {row['pid']} · user {row['user']} · parent {row.get('ppid', '—')}",
                f"PSS {size(row.get('pss'))}   RSS {size(row.get('rss'))}   USS {size(row.get('uss'))}",
                f"GPU allocation {size(row.get('gpu_bytes'))}   Swap {size(row.get('swap'))}",
                f"RSS anon {size(row.get('rss_anon'))} / file {size(row.get('rss_file'))} / shared {size(row.get('rss_shmem'))}",
                f"PSS coverage: {row.get('pss_status', 'unknown')}",
                f"GPU context: {row.get('gpu_type') or '—'}  {', '.join(row.get('gpu_devices', []))}",
                "",
                "COMMAND",
                row.get("command") or "Unavailable",
                "",
                "EXECUTABLE",
                row.get("exe") or "Unavailable",
                "",
                "WORKING DIRECTORY",
                row.get("cwd") or "Unavailable",
                "",
                "CGROUP / SERVICE / CONTAINER",
                row.get("cgroup") or "Unavailable",
                "",
                "PSS apportions shared CPU mappings; USS counts private resident pages.",
                "GPU allocations are driver accounting, not extra RAM to add to PSS/RSS.",
                "Exact CPU/GPU overlap and reclaimable GPU memory are not reported.",
            ]
        if snapshot:
            summary = memory_summary(snapshot)
            lines += [
                "",
                "HOST MEMORY",
                f"Available {size(summary['available'])} / {size(summary['total'])}",
                f"Free {size(snapshot['memory'].get('MemFree'))} · Shared/tmpfs {size(snapshot['memory'].get('Shmem'))}",
                f"Slab {size(snapshot['memory'].get('Slab'))} · Unreclaimable slab {size(snapshot['memory'].get('SUnreclaim'))}",
                f"Zswap pool {size(snapshot['memory'].get('Zswap'))} · Stored in zswap {size(snapshot['memory'].get('Zswapped'))}",
                f"Probe took {snapshot.get('duration', 0):.2f}s · {snapshot.get('skipped_processes', 0)} inaccessible/exited processes",
            ]
            for gpu in snapshot.get("gpus", []):
                lines += [
                    f"{gpu['name']} · utilization {gpu.get('util')}% · {gpu.get('temperature')}°C",
                    f"Dedicated GPU memory: {size(gpu.get('memory_used'))} / {size(gpu.get('memory_total'))}",
                ]
            lines.extend(snapshot.get("notes", []))
        if state.error:
            lines += ["", "LAST SAMPLE IS STALE", state.error]
        self.push_screen(Details("\n".join(lines)))

    def action_search(self):
        self.query_one(Input).focus()

    def action_clear_filter(self):
        self.query_one(Input).value = ""
        self.host_filter = ""
        self.gpu_only = False
        self.query_one(DataTable).focus()
        self.paint()

    def action_next_host(self):
        names = ["", *self.states]
        self.host_filter = names[(names.index(self.host_filter) + 1) % len(names)]
        self.paint()

    def action_host_number(self, index):
        if index < len(self.states):
            self.host_filter = list(self.states)[index]
            self.paint()

    def action_all_hosts(self):
        self.host_filter = ""
        self.paint()

    def action_gpu(self):
        self.gpu_only = not self.gpu_only
        self.paint()

    def action_sort(self):
        self.sort_key = SORTS[(SORTS.index(self.sort_key) + 1) % len(SORTS)]
        self.paint()

    def action_groups(self):
        self.group_view = not self.group_view
        self.rebuild_columns()
        self.paint()

    def action_pause(self):
        self.paused = not self.paused
        self.paint()

    def action_refresh(self):
        self.poll(force=True)

    def action_help(self):
        self.push_screen(
            Details(
                "sparkmem · keys and memory accounting\n\n"
                "/ Search full command lines, model names, paths, users, PIDs\n"
                "Enter Inspect selected process/service; Esc Close or clear filters\n"
                "h Cycle host; 1–4 Select host; 0 All hosts\n"
                "g GPU processes; s Cycle sort; click memory header to sort\n"
                "c Toggle services/containers; Space Pause polling; r Refresh\n"
                "Tab Switch focus; arrows/PageUp/PageDown Navigate; q Quit\n\n"
                "SYSTEM MEMORY\n"
                "Used = MemTotal − MemAvailable. Available estimates allocation headroom\n"
                "without swapping. UMA uses this one physical RAM pool. Cache is an\n"
                "estimate (Cached + Buffers + SReclaimable − Shmem), not guaranteed free.\n"
                "PSI is the % of the last 10 seconds some tasks stalled on memory.\n"
                "The history shows used percentage over the last 60 samples.\n\n"
                "PROCESS MEMORY\n"
                "PSS apportions shared resident mappings. RSS includes shared mappings.\n"
                "GPU alloc is NVIDIA's per-process allocation (compute AND graphics).\n"
                "Do not add GPU + PSS/RSS: overlap is unknown on UMA. These counters\n"
                "cannot produce an exact per-process physical total or CPU-only split.\n"
                "Impact sort uses max(PSS, GPU), falling back to RSS when PSS is absent;\n"
                "this is a ranking heuristic, not a memory total.\n"
                "— means unavailable/not sampled, never zero. PSS samples the largest\n"
                "RSS processes plus every GPU process; pss_limit=0 samples all.\n"
                "CPU % uses interval deltas, one core=100%, unavailable on first sample.\n\n"
                "SERVICES / CONTAINERS\n"
                "cgroup v2 memory.current counts charged memory including descendants\n"
                "and file cache. Parent and child rows overlap. No Docker socket needed.\n"
                "Container IDs appear in cgroup paths; config regex labels can name them.\n\n"
                "CUSTOMIZE\n"
                "~/.config/sparkmem/config.toml controls hosts, Python paths, refresh,\n"
                "columns, labels, sort, theme, and UMA overrides. Restart after editing.\n"
                "Run sparkmem --print-config for an example."
            )
        )

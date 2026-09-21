import pytest
from textual.widgets import DataTable, Input, Static

from sparkmem.config import Settings
from sparkmem.ui import Details, SparkMem, clean, size


async def test_keyboard_search_host_gpu_sort_details_and_services():
    app = SparkMem(Settings(), demo=True)
    async with app.run_test(size=(160, 48)) as pilot:
        table = app.query_one(DataTable)
        assert table.row_count == 16
        assert [column.key.value for column in table.ordered_columns][:6] == [
            "ram",
            "nvme",
            "gpu",
            "swap",
            "pss",
            "rss",
        ]
        await pilot.press("3")
        assert table.row_count == 4
        await pilot.press("g")
        assert table.row_count == 3
        await pilot.press("s")
        assert app.sort_key == "gpu"
        await pilot.press("/")
        app.query_one(Input).value = "Model-70B"
        await pilot.press("enter")
        assert table.row_count == 1
        await pilot.press("enter")
        assert isinstance(app.screen, Details)
        assert "example/Model-70B" in app.screen.content
        assert "ESTIMATED PLACEMENT" in app.screen.content
        assert "not a measurement" in app.screen.content
        await pilot.pause(1.1)  # A host timer tick must work while the modal is open.
        await pilot.press("escape", "escape")
        assert table.row_count == 16
        await pilot.press("c")
        assert app.group_view and table.row_count == 4
        await pilot.press("enter")
        assert "Parent and child rows overlap" in app.screen.content
        await pilot.press("escape", "space")
        assert app.paused
        await pilot.press("question_mark")
        assert isinstance(app.screen, Details)


@pytest.mark.parametrize("dimensions", [(80, 24), (120, 40), (180, 48)])
async def test_responsive_layout_and_stale_host(dimensions):
    app = SparkMem(Settings(), demo=True)
    async with app.run_test(size=dimensions) as pilot:
        app.states["spark-2"].error = "network unavailable"
        app.paint()
        await pilot.pause()
        assert "STALE" in str(app.query_one("#host-1", Static).render())
        assert app.query_one(DataTable).size.height >= 3
        if dimensions == (80, 24):
            assert app.query_one(DataTable).size.height >= 6
            assert "gpu" in app.visible_columns
            assert "command" in app.visible_columns
            assert "label" not in app.visible_columns
            assert app.visible_columns[:6] == ["ram", "nvme", "gpu", "swap", "pss", "rss"]


def test_display_unknown_and_untrusted_text():
    assert size(None) == "—"
    assert size(1024**3) == "1.0G"
    assert "\x1b" not in clean("\x1b[31mhello")

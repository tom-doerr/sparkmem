import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from sparkmem import collector
from sparkmem.collector import HostState, refresh, sample
from sparkmem.config import Host, Settings
from sparkmem.demo import snapshots


class FakeProcess:
    pid = 123456789
    returncode = 0

    def __init__(self, data=None, delay=0):
        self.data = data or snapshots(1)[0]
        self.delay = delay
        self.wait = AsyncMock()

    async def communicate(self, source):
        assert b"def collect(" in source
        await asyncio.sleep(self.delay)
        return json.dumps(self.data).encode(), b""


async def test_ssh_command_quotes_remote_python(monkeypatch):
    spawn = AsyncMock(return_value=FakeProcess())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    host = Host("remote", python="/opt/python path/python3", transport="ssh")
    assert (await sample(host, Settings()))["schema"] == 1
    args = spawn.call_args.args
    assert args[0] == "ssh"
    assert args[-1].startswith("'/opt/python path/python3' -")
    assert "BatchMode=yes" in args
    assert "--" in args


async def test_timeout_kills_probe_and_retains_last_sample(monkeypatch):
    process = FakeProcess(delay=10)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    killed = []
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: killed.append(pid))
    state = HostState(Host("localhost"))
    previous = snapshots(1)[0]
    state.accept(previous)
    await refresh(state, Settings(timeout=0.01))
    assert "Timed out" in state.error
    assert state.snapshot is previous
    assert killed == [process.pid]
    process.wait.assert_awaited_once()


async def test_failure_does_not_block_success(monkeypatch):
    async def fake(host, settings):
        if host.name == "bad":
            raise RuntimeError("offline")
        return snapshots(1)[0]

    monkeypatch.setattr(collector, "sample", fake)
    good, bad = HostState(Host("good")), HostState(Host("bad"))
    await asyncio.gather(refresh(good, Settings()), refresh(bad, Settings()))
    assert good.snapshot and not good.error
    assert bad.error == "offline" and bad.snapshot is None


async def test_invalid_json_becomes_useful_error(monkeypatch):
    process = FakeProcess()
    process.communicate = AsyncMock(return_value=(b"shell startup banner\n{}", b""))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(RuntimeError, match="shell startup"):
        await sample(Host("localhost"), Settings())

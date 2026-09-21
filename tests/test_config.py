from importlib.resources import files

import pytest

from sparkmem.config import Host, Settings, load


def test_example_loads(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(files("sparkmem").joinpath("example.toml").read_text())
    settings = load(config)
    assert len(settings.hosts) == 4
    assert not settings.hosts[-1].gpu
    assert settings.labels[0]["name"] == "vLLM inference"
    assert Host("localhost").local
    assert not Host("localhost", transport="ssh").local


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval": 0},
        {"interval": float("nan")},
        {"timeout": float("inf")},
        {"pss_limit": -1},
        {"pss_limit": 1.5},
        {"columns": ["fake"]},
        {"columns": ["rss", "rss"]},
        {"hosts": []},
        {"hosts": [Host("a"), Host("a")]},
        {"hosts": [Host("-oProxyCommand=bad")]},
        {"hosts": [Host("bad host")]},
        {"hosts": [Host("a", gpu="false")]},
        {"sort": "unknown"},
    ],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs).validate()


def test_unknown_config_is_rejected(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[refresh]\nintervall=5\n")
    with pytest.raises(ValueError, match="Unknown setting"):
        load(path)

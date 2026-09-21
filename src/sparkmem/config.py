"""Small, explicit TOML configuration; no remote installation or daemon."""

import math
import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

COLUMNS = (
    "ram",
    "nvme",
    "gpu",
    "swap",
    "pss",
    "rss",
    "host",
    "pid",
    "user",
    "label",
    "cpu",
    "command",
    "cgroup",
)
SORTS = ("impact", "gpu", "ram", "nvme", "pss", "rss", "swap", "cpu")
DEFAULT_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sparkmem/config.toml"
)


@dataclass
class Host:
    name: str
    target: str = ""
    transport: str = "auto"
    python: str = "python3"
    uma: bool | None = None
    gpu: bool = True

    @property
    def local(self):
        if self.transport != "auto":
            return self.transport == "local"
        return (self.target or self.name) in {
            "localhost",
            "127.0.0.1",
            "::1",
            socket.gethostname(),
            socket.getfqdn(),
        }


@dataclass
class Settings:
    hosts: list[Host] = field(
        default_factory=lambda: (
            [Host(f"spark-{i}", uma=True) for i in range(1, 4)] + [Host("nas", gpu=False)]
        )
    )
    interval: float = 5.0
    timeout: float = 20.0
    pss_limit: int = 80
    sort: str = "impact"
    columns: list[str] = field(default_factory=lambda: list(COLUMNS[:-1]))
    labels: list[dict] = field(default_factory=list)
    theme: str = "textual-dark"

    def validate(self):
        for key in ("interval", "timeout"):
            value = getattr(self, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 1
            ):
                raise ValueError(f"{key} must be a finite number >= 1")
        if type(self.pss_limit) is not int or self.pss_limit < 0:
            raise ValueError("pss_limit must be an integer >= 0 (0 samples all processes)")
        if self.sort not in SORTS:
            raise ValueError(f"sort must be one of {SORTS}")
        if (
            not self.columns
            or len(set(self.columns)) != len(self.columns)
            or set(self.columns) - set(COLUMNS)
        ):
            raise ValueError(f"columns must be unique names from {COLUMNS}")
        if not self.hosts or len({h.name for h in self.hosts}) != len(self.hosts):
            raise ValueError("hosts must be nonempty with unique names")
        for host in self.hosts:
            if not isinstance(host.name, str) or not host.name.strip():
                raise ValueError("Each host requires a nonempty name")
            target = host.target or host.name
            if (
                not isinstance(target, str)
                or target.startswith("-")
                or any(c.isspace() for c in target)
            ):
                raise ValueError(
                    "SSH targets must be aliases or user@host, without options or spaces"
                )
            if host.transport not in ("auto", "ssh", "local"):
                raise ValueError("transport must be auto, ssh, or local")
            if not isinstance(host.python, str) or not host.python or "\0" in host.python:
                raise ValueError("python must be an executable name or absolute path")
            if type(host.gpu) is not bool or (host.uma is not None and type(host.uma) is not bool):
                raise ValueError("gpu and uma must be booleans")
        for label in self.labels:
            if set(label) != {"pattern", "name"} or not all(
                isinstance(v, str) for v in label.values()
            ):
                raise ValueError("labels require string pattern and name")
            try:
                re.compile(label["pattern"])
            except re.error as error:
                raise ValueError(f"Invalid label regex: {error}") from error
        return self


def load(path: Path | None = None):
    path = path or DEFAULT_PATH
    if not path.exists():
        return Settings().validate()
    raw = tomllib.loads(path.read_text())
    unknown = set(raw) - {"hosts", "refresh", "view", "labels"}
    if unknown:
        raise ValueError(f"Unknown config sections: {', '.join(sorted(unknown))}")
    settings = Settings()
    if "hosts" in raw:
        settings.hosts = [Host(**host) for host in raw["hosts"]]
    for section, allowed in (
        ("refresh", {"interval", "timeout", "pss_limit"}),
        ("view", {"sort", "columns", "theme"}),
    ):
        for key, value in raw.get(section, {}).items():
            if key not in allowed:
                raise ValueError(f"Unknown setting: {section}.{key}")
            setattr(settings, key, value)
    settings.labels = raw.get("labels", [])
    return settings.validate()

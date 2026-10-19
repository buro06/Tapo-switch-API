"""Persistent JSON configuration (credentials, server settings, device identifiers)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(os.environ.get("TAPO_SWITCH_CONFIG", "config.json"))

DEFAULTS: dict[str, Any] = {
    "credentials": {"username": None, "password": None},
    "server": {"host": "0.0.0.0", "port": 8080, "autostart": True, "token": None},
    "discovery": {"target": "255.255.255.255", "timeout": 5},
    "devices": {},
}


def _merge(defaults: dict, loaded: dict) -> dict:
    out = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "devices":
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> Config:
        cfg = cls(path)
        if cfg.path.exists():
            cfg.data = _merge(DEFAULTS, json.loads(cfg.path.read_text()))
        return cfg

    def save(self) -> None:
        """Write atomically and keep the file private (it holds the account password)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    @property
    def credentials(self) -> dict[str, str | None]:
        return self.data["credentials"]

    @property
    def server(self) -> dict[str, Any]:
        return self.data["server"]

    @property
    def discovery(self) -> dict[str, Any]:
        return self.data["discovery"]

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        return self.data["devices"]

    def masked(self) -> dict[str, Any]:
        data = copy.deepcopy(self.data)
        if data["credentials"].get("password"):
            data["credentials"]["password"] = "********"
        if data["server"].get("token"):
            data["server"]["token"] = data["server"]["token"][:4] + "…"
        for entry in data["devices"].values():
            entry.pop("connection", None)
        return data

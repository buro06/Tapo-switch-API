import pytest

from tapo_switch_api.config import Config
from tapo_switch_api.devices import DeviceUnavailable, UnknownDevice
from tapo_switch_api.server import create_app


class FakeManager:
    def __init__(self, config: Config):
        self.config = config
        self.states = {"desk": "off", "broken": "off"}
        self.calls: list[tuple[str, str]] = []

    def _check(self, identifier: str) -> str:
        key = identifier.lower()
        if key not in self.states:
            raise UnknownDevice(f"unknown device {identifier!r}")
        if key == "broken":
            raise DeviceUnavailable("broken: timed out")
        return key

    async def set_state(self, identifier, state):
        key = self._check(identifier)
        self.calls.append((key, state))
        if state == "toggle":
            state = "off" if self.states[key] == "on" else "on"
        self.states[key] = state
        return {"id": key, "name": key.title(), "state": state, "host": "10.0.0.2", "model": "P110"}

    async def status(self, identifier):
        key = self._check(identifier)
        return {"id": key, "name": key.title(), "state": self.states[key], "host": "10.0.0.2", "model": "P110"}

    async def status_all(self):
        return [{"id": "desk", "name": "Desk", "state": self.states["desk"]}]


@pytest.fixture
def config(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.devices["desk"] = {"device_id": "abc", "child_id": None, "host": "10.0.0.2", "model": "P110", "tapo_name": "Desk"}
    return cfg


@pytest.fixture
def manager(config):
    return FakeManager(config)


@pytest.fixture
async def client(aiohttp_client, manager, config):
    return await aiohttp_client(create_app(manager, lambda: config.server.get("token")))


async def test_switch_on_and_off(client, manager):
    resp = await client.get("/switch", params={"id": "desk", "state": "on"})
    assert resp.status == 200
    assert (await resp.json())["state"] == "on"
    resp = await client.get("/switch", params={"id": "Desk", "state": "OFF"})
    assert (await resp.json())["state"] == "off"
    assert manager.calls == [("desk", "on"), ("desk", "off")]


async def test_toggle(client):
    resp = await client.get("/switch?id=desk&state=toggle")
    assert (await resp.json())["state"] == "on"


@pytest.mark.parametrize("query", ["", "id=desk", "id=desk&state=maybe", "state=on"])
async def test_bad_params(client, query):
    resp = await client.get(f"/switch?{query}")
    assert resp.status == 400
    assert (await resp.json())["ok"] is False


async def test_unknown_device(client):
    resp = await client.get("/switch?id=nope&state=on")
    assert resp.status == 404


async def test_device_unavailable(client):
    resp = await client.get("/switch?id=broken&state=on")
    assert resp.status == 502


async def test_status(client):
    resp = await client.get("/status?id=desk")
    assert (await resp.json())["state"] == "off"
    resp = await client.get("/status")
    assert (await resp.json())["devices"][0]["id"] == "desk"


async def test_devices(client):
    resp = await client.get("/devices")
    assert (await resp.json())["devices"]["desk"]["name"] == "Desk"


async def test_token_enforced_when_set(client, config, manager):
    config.server["token"] = "s3cret"
    assert (await client.get("/switch?id=desk&state=on")).status == 401
    assert (await client.get("/switch?id=desk&state=on&token=wrong")).status == 401
    assert (await client.get("/switch?id=desk&state=on&token=s3cret")).status == 200
    resp = await client.get("/switch?id=desk&state=off", headers={"Authorization": "Bearer s3cret"})
    assert resp.status == 200
    resp = await client.get("/switch?id=desk&state=on", headers={"X-API-Key": "s3cret"})
    assert resp.status == 200
    assert (await client.get("/health")).status == 200
    assert len(manager.calls) == 3

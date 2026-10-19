import pytest
from kasa import DeviceConfig, KasaException

from tapo_switch_api import devices as devices_mod
from tapo_switch_api.config import Config
from tapo_switch_api.devices import DeviceManager, DeviceUnavailable, UnknownDevice


class FakeDeviceConfig:
    def __init__(self, host):
        self.host = host

    def to_dict_control_credentials(self, **kwargs):
        return DeviceConfig(host=self.host).to_dict()


class FakeDevice:
    def __init__(self, host, alias, device_id, children=(), parent=None):
        self.host, self.alias, self.device_id = host, alias, device_id
        self.model = "P300" if children else "P110"
        self.is_on = False
        self.children = list(children)
        self.config = FakeDeviceConfig(host)
        self.fail_next = 0
        self.disconnected = False
        self.parent = parent

    async def update(self):
        if self.fail_next:
            self.fail_next -= 1
            raise KasaException("timeout")

    async def turn_on(self):
        self.is_on = True

    async def turn_off(self):
        self.is_on = False

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def network(monkeypatch):
    plug = FakeDevice("10.0.0.2", "Desk Lamp", "plug1")
    strip = FakeDevice(
        "10.0.0.3",
        "Strip",
        "strip1",
        children=[FakeDevice("10.0.0.3", "Fan", "strip1-c0"), FakeDevice("10.0.0.3", "Heater", "strip1-c1")],
    )
    devs = {"10.0.0.2": plug, "10.0.0.3": strip}

    async def discover(**kwargs):
        return dict(devs)

    async def discover_single(host, **kwargs):
        return devs.get(host)

    async def connect(*, config):
        dev = devs.get(config.host)
        if dev is None:
            raise KasaException("unreachable")
        await dev.update()
        return dev

    monkeypatch.setattr(devices_mod.Discover, "discover", discover)
    monkeypatch.setattr(devices_mod.Discover, "discover_single", discover_single)
    monkeypatch.setattr(devices_mod.Device, "connect", connect)
    return devs


@pytest.fixture
def manager(tmp_path):
    return DeviceManager(Config(tmp_path / "config.json"))


async def test_discover_flattens_children(manager, network):
    found = await manager.discover()
    assert [d.name for d in found] == ["Desk Lamp", "Fan", "Heater"]
    assert found[1].child_id == "strip1-c0" and found[1].parent_name == "Strip"


async def test_assign_and_switch(manager, network):
    await manager.discover()
    key, dev = await manager.assign("Desk", "1")
    assert key == "desk" and dev.name == "Desk Lamp"
    assert manager.config.path.exists()

    result = await manager.set_state("desk", "on")
    assert result["state"] == "on" and network["10.0.0.2"].is_on
    result = await manager.set_state("desk", "toggle")
    assert result["state"] == "off"


async def test_assign_by_name_and_child_control(manager, network):
    await manager.discover()
    await manager.assign("heater", "heater")
    await manager.set_state("heater", "on")
    strip = network["10.0.0.3"]
    assert strip.children[1].is_on and not strip.children[0].is_on


async def test_assign_ip_of_strip_is_ambiguous(manager, network):
    await manager.discover()
    with pytest.raises(ValueError, match="multiple outlets"):
        await manager.assign("x", "10.0.0.3")


async def test_resolve_falls_back_to_tapo_name(manager, network):
    await manager.discover()
    result = await manager.set_state("desk lamp", "on")
    assert result["state"] == "on"
    with pytest.raises(UnknownDevice):
        await manager.set_state("garage", "on")


async def test_bad_identifier(manager, network):
    await manager.discover()
    with pytest.raises(ValueError):
        await manager.assign("bad name!", "1")


async def test_retries_once_then_reports_unavailable(manager, network):
    await manager.discover()
    await manager.assign("desk", "1")
    plug = network["10.0.0.2"]

    plug.fail_next = 1  # transient: succeeds on retry via reconnect
    assert (await manager.set_state("desk", "on"))["state"] == "on"

    plug.fail_next = 10
    with pytest.raises(DeviceUnavailable):
        await manager.set_state("desk", "off")


async def test_unassign(manager, network):
    await manager.discover()
    await manager.assign("desk", "1")
    manager.unassign("DESK")
    assert "desk" not in manager.config.devices
    with pytest.raises(UnknownDevice):
        manager.unassign("desk")


async def test_reconnects_after_restart_and_follows_ip_change(tmp_path, network):
    first = DeviceManager(Config(tmp_path / "config.json"))
    await first.discover()
    await first.assign("desk", "1")

    # Fresh process: connects straight from the cached connection params.
    second = DeviceManager(Config.load(tmp_path / "config.json"))
    assert (await second.set_state("desk", "on"))["host"] == "10.0.0.2"

    # Plug gets a new DHCP lease; a third process must re-find it by device_id.
    plug = network.pop("10.0.0.2")
    plug.host = plug.config.host = "10.0.0.9"
    network["10.0.0.9"] = plug
    third = DeviceManager(Config.load(tmp_path / "config.json"))
    assert (await third.set_state("desk", "off"))["host"] == "10.0.0.9"
    assert Config.load(tmp_path / "config.json").devices["desk"]["host"] == "10.0.0.9"

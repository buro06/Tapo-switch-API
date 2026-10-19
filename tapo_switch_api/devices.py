"""Local discovery and control of Tapo/Kasa devices via python-kasa.

Everything here talks to devices directly on the LAN. The TP-Link account
credentials are only used to authenticate the local (KLAP/AES) session that
Tapo devices require; nothing is sent to TP-Link's cloud.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from kasa import (
    AuthenticationError,
    Credentials,
    Device,
    DeviceConfig,
    Discover,
    KasaException,
)

from .config import Config

log = logging.getLogger(__name__)

State = Literal["on", "off", "toggle"]
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# Errors that mean "couldn't talk to the device" rather than a programming bug.
DEVICE_ERRORS = (KasaException, OSError, asyncio.TimeoutError)


class UnknownDevice(Exception):
    """No configured identifier or discovered device matches the request."""


class DeviceUnavailable(Exception):
    """The device is known but could not be reached or controlled."""


@dataclass
class DiscoveredDevice:
    """One switchable thing found on the network (a plug, or one outlet of a strip)."""

    host: str
    name: str
    model: str
    device_id: str
    child_id: str | None
    is_on: bool
    parent_name: str | None = None
    connection: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_entry(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "child_id": self.child_id,
            "host": self.host,
            "model": self.model,
            "tapo_name": self.name,
            "connection": self.connection,
        }


def normalize_identifier(identifier: str) -> str:
    return identifier.strip().lower()


def _connection_dict(dev: Device) -> dict[str, Any]:
    """Serialisable connection params (protocol/encryption) without credentials."""
    return dev.config.to_dict_control_credentials(credentials_hash="", exclude_credentials=True)


def _looks_like_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return "." in value and " " not in value


class DeviceManager:
    def __init__(self, config: Config):
        self.config = config
        self.last_discovery: list[DiscoveredDevice] = []
        self.discovery_errors: list[tuple[str, str]] = []
        self._devices: dict[str, Device] = {}  # parent device_id -> connected Device
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ helpers

    @property
    def credentials(self) -> Credentials | None:
        creds = self.config.credentials
        if creds.get("username") and creds.get("password"):
            return Credentials(creds["username"], creds["password"])
        return None

    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    async def _drop(self, device_id: str) -> None:
        dev = self._devices.pop(device_id, None)
        if dev is not None:
            try:
                await dev.disconnect()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass

    @staticmethod
    def _target(dev: Device, entry: dict[str, Any]) -> Device:
        child_id = entry.get("child_id")
        if not child_id:
            return dev
        for child in dev.children:
            if child.device_id == child_id:
                return child
        raise DeviceUnavailable(f"outlet {child_id} not found on {dev.alias}")

    @staticmethod
    def _result(key: str, dev: Device, target: Device) -> dict[str, Any]:
        return {
            "id": key,
            "name": target.alias,
            "state": "on" if target.is_on else "off",
            "host": dev.host,
            "model": dev.model,
        }

    # ---------------------------------------------------------------- discovery

    def _flatten(self, dev: Device) -> list[DiscoveredDevice]:
        conn = _connection_dict(dev)
        if dev.children:
            return [
                DiscoveredDevice(
                    host=dev.host,
                    name=child.alias or f"{dev.alias} #{i}",
                    model=dev.model,
                    device_id=dev.device_id,
                    child_id=child.device_id,
                    is_on=child.is_on,
                    parent_name=dev.alias,
                    connection=conn,
                )
                for i, child in enumerate(dev.children, 1)
            ]
        return [
            DiscoveredDevice(
                host=dev.host,
                name=dev.alias or dev.host,
                model=dev.model,
                device_id=dev.device_id,
                child_id=None,
                is_on=dev.is_on,
                connection=conn,
            )
        ]

    async def _adopt(self, dev: Device) -> Device:
        """Keep a freshly connected device unless we already hold a connection to it."""
        existing = self._devices.get(dev.device_id)
        if existing is not None and existing is not dev:
            await dev.disconnect()
            return existing
        self._devices[dev.device_id] = dev
        return dev

    async def discover(self, timeout: int | None = None) -> list[DiscoveredDevice]:
        timeout = timeout or self.config.discovery["timeout"]
        found = await Discover.discover(
            target=self.config.discovery["target"],
            discovery_timeout=timeout,
            credentials=self.credentials,
        )
        results: list[DiscoveredDevice] = []
        errors: list[tuple[str, str]] = []

        async def probe(host: str, dev: Device) -> None:
            try:
                await dev.update()
            except AuthenticationError:
                errors.append((host, "authentication failed - check `login` credentials"))
                await dev.disconnect()
                return
            except DEVICE_ERRORS as e:
                errors.append((host, str(e) or type(e).__name__))
                await dev.disconnect()
                return
            dev = await self._adopt(dev)
            results.extend(self._flatten(dev))

        await asyncio.gather(*(probe(h, d) for h, d in found.items()))
        results.sort(key=lambda d: (d.parent_name or d.name).lower() + d.name.lower())
        self.last_discovery = results
        self.discovery_errors = errors
        self._refresh_saved_hosts(results)
        return results

    def _refresh_saved_hosts(self, found: list[DiscoveredDevice]) -> None:
        """If a configured device moved to a new IP (DHCP), remember the new one."""
        by_id = {d.device_id: d for d in found}
        changed = False
        for entry in self.config.devices.values():
            d = by_id.get(entry["device_id"])
            if d and (entry.get("host") != d.host or entry.get("connection") != d.connection):
                entry["host"], entry["connection"] = d.host, d.connection
                changed = True
        if changed:
            self.config.save()

    async def _probe_host(self, host: str) -> list[DiscoveredDevice]:
        dev = await Discover.discover_single(host, credentials=self.credentials)
        if dev is None:
            raise DeviceUnavailable(f"no device answered at {host}")
        await dev.update()
        dev = await self._adopt(dev)
        return self._flatten(dev)

    # -------------------------------------------------------------- identifiers

    async def assign(self, identifier: str, target: str) -> tuple[str, DiscoveredDevice]:
        key = normalize_identifier(identifier)
        if not IDENTIFIER_RE.match(key):
            raise ValueError(
                "identifier must be 1-64 chars of a-z, 0-9, '-', '_' or '.', "
                "starting with a letter or digit"
            )
        match = self._match_discovered(target)
        if match is None and _looks_like_host(target):
            candidates = await self._probe_host(target)
            if len(candidates) > 1:
                names = ", ".join(c.name for c in candidates)
                raise ValueError(f"{target} has multiple outlets ({names}); use its number or name")
            match = candidates[0]
        if match is None:
            raise UnknownDevice(f"no discovered device matches {target!r} - run `discover` first")
        self.config.devices[key] = match.to_entry()
        self.config.save()
        return key, match

    def _match_discovered(self, target: str) -> DiscoveredDevice | None:
        target = target.strip()
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(self.last_discovery):
                return self.last_discovery[idx - 1]
            raise UnknownDevice(f"no device #{idx} in the last discovery")
        lowered = target.lower()
        for d in self.last_discovery:
            if d.name.lower() == lowered:
                return d
        hosts = [d for d in self.last_discovery if d.host == target]
        if len(hosts) > 1:
            names = ", ".join(d.name for d in hosts)
            raise ValueError(f"{target} has multiple outlets ({names}); use its number or name")
        return hosts[0] if hosts else None

    def unassign(self, identifier: str) -> None:
        key = normalize_identifier(identifier)
        if key not in self.config.devices:
            raise UnknownDevice(f"no identifier {key!r}")
        del self.config.devices[key]
        self.config.save()

    def resolve(self, identifier: str) -> tuple[str, dict[str, Any]]:
        """Configured identifier first, then the device's own Tapo app name."""
        key = normalize_identifier(identifier)
        if key in self.config.devices:
            return key, self.config.devices[key]
        for k, entry in self.config.devices.items():
            if (entry.get("tapo_name") or "").lower() == key:
                return k, entry
        for d in self.last_discovery:
            if d.name.lower() == key:
                return key, d.to_entry()
        raise UnknownDevice(f"unknown device {identifier!r}")

    # --------------------------------------------------------------- connection

    async def _connect(self, entry: dict[str, Any]) -> Device:
        """Connect to a configured device, re-finding it if its IP has changed."""
        device_id = entry["device_id"]
        creds = self.credentials

        # 1. Cached connection parameters: one round trip, no broadcast needed.
        cfg: DeviceConfig | None = None
        if entry.get("connection") and entry.get("host"):
            try:
                cfg = DeviceConfig.from_dict({**entry["connection"], "host": entry["host"]})
                cfg.credentials = creds
            except Exception as e:  # noqa: BLE001 - stale/incompatible cache; fall back to discovery
                log.debug("ignoring cached connection for %s: %s", entry["host"], e)
                cfg = None
        if cfg is not None:
            try:
                dev = await Device.connect(config=cfg)
                if dev.device_id == device_id:
                    return self._remember(entry, dev)
                await dev.disconnect()
            except AuthenticationError:
                raise
            except DEVICE_ERRORS as e:
                log.debug("direct connect to %s failed: %s", entry["host"], e)

        # 2. Unicast discovery at the last known address.
        if entry.get("host"):
            try:
                dev = await Discover.discover_single(entry["host"], credentials=creds)
                if dev is not None:
                    await dev.update()
                    if dev.device_id == device_id:
                        return self._remember(entry, dev)
                    await dev.disconnect()
            except AuthenticationError:
                raise
            except DEVICE_ERRORS as e:
                log.debug("discover_single %s failed: %s", entry["host"], e)

        # 3. Broadcast discovery to find it at a new address.
        log.info("searching the network for %s", entry.get("tapo_name") or device_id)
        await self.discover()
        dev = self._devices.get(device_id)
        if dev is None:
            raise DeviceUnavailable(f"{entry.get('tapo_name') or device_id} not found on the network")
        return dev

    def _remember(self, entry: dict[str, Any], dev: Device) -> Device:
        conn = _connection_dict(dev)
        if entry.get("host") != dev.host or entry.get("connection") != conn:
            entry["host"], entry["connection"] = dev.host, conn
            if entry in self.config.devices.values():
                self.config.save()
        return dev

    async def _get_device(self, entry: dict[str, Any]) -> Device:
        dev = self._devices.get(entry["device_id"])
        if dev is None:
            dev = await self._adopt(await self._connect(entry))
        return dev

    async def _with_device(self, identifier: str, action) -> dict[str, Any]:
        key, entry = self.resolve(identifier)
        device_id = entry["device_id"]
        async with self._lock(device_id):
            for attempt in (1, 2):
                try:
                    dev = await self._get_device(entry)
                    await action(dev, self._target(dev, entry))
                    return self._result(key, dev, self._target(dev, entry))
                except AuthenticationError as e:
                    await self._drop(device_id)
                    raise DeviceUnavailable(
                        f"authentication failed for {key} - check `login` credentials"
                    ) from e
                except DEVICE_ERRORS as e:
                    await self._drop(device_id)
                    if attempt == 2:
                        raise DeviceUnavailable(f"{key}: {e or type(e).__name__}") from e
                    log.debug("retrying %s after error: %s", key, e)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ control

    async def set_state(self, identifier: str, state: State) -> dict[str, Any]:
        if state not in ("on", "off", "toggle"):
            raise ValueError("state must be 'on', 'off' or 'toggle'")

        async def action(dev: Device, target: Device) -> None:
            if state == "toggle":
                await dev.update()  # cached state may be stale; children update in place
                want_on = not target.is_on
            else:
                want_on = state == "on"
            await (target.turn_on() if want_on else target.turn_off())
            await dev.update()

        return await self._with_device(identifier, action)

    async def status(self, identifier: str) -> dict[str, Any]:
        async def action(dev: Device, target: Device) -> None:
            await dev.update()

        return await self._with_device(identifier, action)

    async def status_all(self) -> list[dict[str, Any]]:
        async def one(key: str) -> dict[str, Any]:
            try:
                return await self.status(key)
            except (DeviceUnavailable, UnknownDevice) as e:
                entry = self.config.devices.get(key, {})
                return {"id": key, "name": entry.get("tapo_name"), "state": "unavailable", "error": str(e)}

        return list(await asyncio.gather(*(one(k) for k in self.config.devices)))

    async def close(self) -> None:
        for device_id in list(self._devices):
            await self._drop(device_id)

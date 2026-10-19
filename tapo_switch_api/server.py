"""HTTP API: GET /switch?id=<identifier>&state=on|off|toggle."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Any

from aiohttp import web

from .devices import DeviceManager, DeviceUnavailable, UnknownDevice

log = logging.getLogger(__name__)

MANAGER_KEY = web.AppKey("manager", DeviceManager)
TOKEN_KEY = web.AppKey("token", Callable[[], str | None])
PUBLIC_PATHS = {"/health"}


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _supplied_token(request: web.Request) -> str | None:
    if token := request.query.get("token"):
        return token
    if token := request.headers.get("X-API-Key"):
        return token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
    expected = request.app[TOKEN_KEY]()
    if expected and request.path not in PUBLIC_PATHS:
        supplied = _supplied_token(request) or ""
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            log.warning("%s %s -> 401 bad or missing token", request.remote, request.path)
            return _error(401, "missing or invalid token")
    return await handler(request)


async def handle_switch(request: web.Request) -> web.Response:
    identifier = request.query.get("id", "").strip()
    state = request.query.get("state", "").strip().lower()
    if not identifier:
        return _error(400, "missing 'id' parameter")
    if state not in ("on", "off", "toggle"):
        return _error(400, "'state' must be 'on', 'off' or 'toggle'")
    try:
        result = await request.app[MANAGER_KEY].set_state(identifier, state)
    except UnknownDevice as e:
        log.warning("%s switch %s=%s -> 404 %s", request.remote, identifier, state, e)
        return _error(404, str(e))
    except DeviceUnavailable as e:
        log.error("%s switch %s=%s -> 502 %s", request.remote, identifier, state, e)
        return _error(502, str(e))
    log.info("%s switch %s=%s -> %s is %s", request.remote, identifier, state, result["name"], result["state"])
    return web.json_response({"ok": True, **result})


async def handle_status(request: web.Request) -> web.Response:
    manager = request.app[MANAGER_KEY]
    identifier = request.query.get("id", "").strip()
    if not identifier:
        return web.json_response({"ok": True, "devices": await manager.status_all()})
    try:
        result = await manager.status(identifier)
    except UnknownDevice as e:
        return _error(404, str(e))
    except DeviceUnavailable as e:
        return _error(502, str(e))
    return web.json_response({"ok": True, **result})


async def handle_devices(request: web.Request) -> web.Response:
    devices: dict[str, Any] = {
        key: {"name": entry.get("tapo_name"), "model": entry.get("model"), "host": entry.get("host")}
        for key, entry in request.app[MANAGER_KEY].config.devices.items()
    }
    return web.json_response({"ok": True, "devices": devices})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def create_app(manager: DeviceManager, token: Callable[[], str | None]) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app[MANAGER_KEY] = manager
    app[TOKEN_KEY] = token
    app.router.add_get("/switch", handle_switch)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/devices", handle_devices)
    app.router.add_get("/health", handle_health)
    return app


class HttpServer:
    """Start/stop wrapper so the CLI can control the server at runtime."""

    def __init__(self, manager: DeviceManager):
        self.manager = manager
        self._runner: web.AppRunner | None = None
        self.host: str | None = None
        self.port: int | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self, host: str, port: int) -> None:
        if self._runner is not None:
            raise RuntimeError(f"already running on {self.host}:{self.port}")
        config = self.manager.config
        app = create_app(self.manager, lambda: config.server.get("token"))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, host, port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self.host, self.port = runner, host, port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = self.host = self.port = None

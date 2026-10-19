"""Interactive REPL that runs alongside the HTTP server in one asyncio loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import shlex
import socket
import sys
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.patch_stdout import patch_stdout

from . import __version__
from .config import DEFAULT_PATH, Config
from .devices import DeviceManager, DeviceUnavailable, DiscoveredDevice, UnknownDevice
from .server import HttpServer

log = logging.getLogger("tapo_switch_api")

HELP = """\
Commands:
  login                          set TP-Link account email/password (used for local auth only)
  discover [seconds]             scan the LAN and list devices with numbers
  alias <identifier> <target>    name a device; target = number from discover, IP, or Tapo name
  unalias <identifier>           remove an identifier
  list                           configured identifiers and their live state
  on|off|toggle <identifier>     switch a device from here
  status [identifier]            show state (all configured devices if omitted)
  serve start [port]|stop|status control the HTTP server
  token generate|show|clear      manage the optional API token
  config                         show the config (secrets masked)
  help                           this text
  quit / exit / Ctrl-D           stop the server and exit"""

COMMANDS = [
    "login", "discover", "alias", "unalias", "list", "on", "off", "toggle",
    "status", "serve", "token", "config", "help", "quit", "exit",
]
SUBCOMMANDS = {"serve": ["start", "stop", "status"], "token": ["generate", "show", "clear"]}
TAKES_IDENTIFIER = {"on", "off", "toggle", "status", "unalias"}


def lan_ip() -> str:
    """Best guess at this machine's LAN address (no packets are actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def table(headers: list[str], rows: Iterable[Iterable[object]]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in rows)])


class CliCompleter(Completer):
    def __init__(self, config: Config):
        self.config = config

    def get_completions(self, document, complete_event):
        words = document.text_before_cursor.split()
        if document.text_before_cursor.endswith(" "):
            words.append("")
        if len(words) <= 1:
            options: Iterable[str] = COMMANDS
        elif len(words) == 2 and words[0] in SUBCOMMANDS:
            options = SUBCOMMANDS[words[0]]
        elif len(words) == 2 and words[0] in TAKES_IDENTIFIER:
            options = self.config.devices.keys()
        else:
            return
        prefix = words[-1] if words else ""
        for opt in options:
            if opt.startswith(prefix):
                yield Completion(opt, start_position=-len(prefix))


class App:
    def __init__(self, config: Config):
        self.config = config
        self.manager = DeviceManager(config)
        self.server = HttpServer(self.manager)
        self.session: PromptSession = PromptSession(completer=CliCompleter(config))
        self.handlers: dict[str, Callable[[list[str]], Awaitable[None]]] = {
            "help": self.cmd_help,
            "login": self.cmd_login,
            "discover": self.cmd_discover,
            "alias": self.cmd_alias,
            "unalias": self.cmd_unalias,
            "list": self.cmd_list,
            "on": self.cmd_switch,
            "off": self.cmd_switch,
            "toggle": self.cmd_switch,
            "status": self.cmd_status,
            "serve": self.cmd_serve,
            "token": self.cmd_token,
            "config": self.cmd_config,
        }
        self._command = ""

    # -------------------------------------------------------------------- loop

    async def run(self, start_server: bool) -> None:
        print(f"tapo-switch-api {__version__} — config: {self.config.path.resolve()}")
        if not self.manager.credentials:
            print("No TP-Link credentials configured yet (needed for Tapo devices).")
            try:
                await self.cmd_login([])
            except (EOFError, KeyboardInterrupt):
                print("\nSkipped — run 'login' later.")
        if start_server:
            await self.dispatch("serve start")
        discovery = asyncio.create_task(self._startup_discovery())
        print("Type 'help' for commands.")
        try:
            while True:
                try:
                    line = await self.session.prompt_async("tapo> ")
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
                if not await self.dispatch(line):
                    break
        finally:
            discovery.cancel()
            await self.server.stop()
            await self.manager.close()
            print("Bye.")

    async def dispatch(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"error: {e}")
            return True
        if not parts:
            return True
        command, args = parts[0].lower(), parts[1:]
        if command in ("quit", "exit"):
            return False
        handler = self.handlers.get(command)
        if handler is None:
            print(f"unknown command {command!r} — type 'help'")
            return True
        self._command = command
        try:
            await handler(args)
        except (UnknownDevice, DeviceUnavailable, ValueError, RuntimeError, OSError) as e:
            print(f"error: {e}")
        except KeyboardInterrupt:
            print("cancelled")
        return True

    async def _startup_discovery(self) -> None:
        try:
            found = await self.manager.discover()
        except Exception as e:  # noqa: BLE001 - background task; just report
            log.warning("startup discovery failed: %s", e)
            return
        print(f"Found {len(found)} device(s) on the network. Run 'discover' to list them.")
        self._print_discovery_errors()

    # ---------------------------------------------------------------- commands

    async def cmd_help(self, args: list[str]) -> None:
        print(HELP)

    async def cmd_login(self, args: list[str]) -> None:
        creds = self.config.credentials
        # Separate session: PromptSession remembers per-call options like is_password.
        session: PromptSession = PromptSession()
        username = await session.prompt_async("TP-Link account email: ", default=creds.get("username") or "")
        password = await session.prompt_async("Password: ", is_password=True)
        if not username.strip() or not password:
            print("Not saved (email and password are both required).")
            return
        creds["username"], creds["password"] = username.strip(), password
        self.config.save()
        await self.manager.close()  # reconnect with the new credentials
        print(f"Saved to {self.config.path} (file mode 600).")

    async def cmd_discover(self, args: list[str]) -> None:
        timeout = int(args[0]) if args else None
        print("Discovering…")
        found = await self.manager.discover(timeout)
        self._print_discovery(found)
        self._print_discovery_errors()

    def _print_discovery(self, found: list[DiscoveredDevice]) -> None:
        if not found:
            print("No devices found. Are you on the same network/subnet as the devices?")
            return
        assigned = {(e["device_id"], e.get("child_id")): k for k, e in self.config.devices.items()}
        rows = [
            (
                i,
                d.name,
                d.parent_name or "",
                d.model,
                d.host,
                "on" if d.is_on else "off",
                assigned.get((d.device_id, d.child_id), ""),
            )
            for i, d in enumerate(found, 1)
        ]
        print(table(["#", "Tapo name", "Parent", "Model", "IP", "State", "Identifier"], rows))

    def _print_discovery_errors(self) -> None:
        for host, err in self.manager.discovery_errors:
            print(f"  ! {host}: {err}")

    async def cmd_alias(self, args: list[str]) -> None:
        if len(args) < 2:
            print("usage: alias <identifier> <number|ip|tapo name>")
            return
        key, dev = await self.manager.assign(args[0], " ".join(args[1:]))
        print(f"'{key}' → {dev.name} ({dev.model} @ {dev.host})")
        if self.server.running:
            print(f"  {self._url('switch', id=key, state='on')}")

    async def cmd_unalias(self, args: list[str]) -> None:
        if len(args) != 1:
            print("usage: unalias <identifier>")
            return
        self.manager.unassign(args[0])
        print(f"Removed '{args[0].lower()}'.")

    async def cmd_list(self, args: list[str]) -> None:
        if not self.config.devices:
            print("No identifiers yet. Run 'discover' then 'alias <identifier> <number>'.")
            return
        results = {r["id"]: r for r in await self.manager.status_all()}
        rows = [
            (key, e.get("tapo_name"), e.get("model"), e.get("host"), results.get(key, {}).get("state", "?"))
            for key, e in sorted(self.config.devices.items())
        ]
        print(table(["Identifier", "Tapo name", "Model", "IP", "State"], rows))

    async def cmd_switch(self, args: list[str]) -> None:
        if not args:
            print(f"usage: {self._command} <identifier>")
            return
        result = await self.manager.set_state(" ".join(args), self._command)
        print(f"{result['id']} ({result['name']}) is now {result['state']}")

    async def cmd_status(self, args: list[str]) -> None:
        if not args:
            await self.cmd_list([])
            return
        result = await self.manager.status(" ".join(args))
        print(f"{result['id']} ({result['name']}, {result['model']} @ {result['host']}) is {result['state']}")

    async def cmd_serve(self, args: list[str]) -> None:
        sub = args[0].lower() if args else "status"
        if sub == "start":
            host = self.config.server["host"]
            port = int(args[1]) if len(args) > 1 else int(self.config.server["port"])
            await self.server.start(host, port)
            if port != self.config.server["port"]:
                self.config.server["port"] = port
                self.config.save()
            print(f"HTTP server listening on {host}:{port}")
            self._print_examples()
        elif sub == "stop":
            if not self.server.running:
                print("Server is not running.")
                return
            await self.server.stop()
            print("HTTP server stopped.")
        elif sub == "status":
            if self.server.running:
                print(f"Running on {self.server.host}:{self.server.port}")
                self._print_examples()
            else:
                print("Server is not running. Use 'serve start [port]'.")
        else:
            print("usage: serve start [port] | stop | status")

    def _url(self, path: str, **params: str) -> str:
        if token := self.config.server.get("token"):
            params["token"] = token
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"http://{lan_ip()}:{self.server.port}/{path}" + (f"?{query}" if query else "")

    def _print_examples(self) -> None:
        example = next(iter(self.config.devices), "<identifier>")
        print(f"  {self._url('switch', id=example, state='on')}")
        print(f"  {self._url('switch', id=example, state='off')}")
        print(f"  {self._url('status')}")
        if not self.config.server.get("token"):
            print("  (no API token set — anyone on your network can call this; see 'token generate')")

    async def cmd_token(self, args: list[str]) -> None:
        sub = args[0].lower() if args else "show"
        server = self.config.server
        if sub == "generate":
            server["token"] = secrets.token_urlsafe(24)
            self.config.save()
            print(f"New token: {server['token']}")
            print("Send it as ?token=…, 'Authorization: Bearer …' or 'X-API-Key: …'.")
        elif sub == "clear":
            server["token"] = None
            self.config.save()
            print("Token cleared — the API no longer requires authentication.")
        elif sub == "show":
            print(server.get("token") or "No token set.")
        else:
            print("usage: token generate | show | clear")

    async def cmd_config(self, args: list[str]) -> None:
        print(json.dumps(self.config.masked(), indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tapo_switch_api",
        description="Switch Tapo plugs/switches locally via HTTP GET requests.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_PATH, help="config file (default: %(default)s)")
    parser.add_argument("--port", type=int, help="override the HTTP port for this run")
    parser.add_argument("--no-server", action="store_true", help="don't start the HTTP server on launch")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    if args.port:
        config.server["port"] = args.port

    async def run() -> None:
        # Configure logging inside patch_stdout so log lines print above the prompt.
        with patch_stdout(raw=True):
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
            root = logging.getLogger()
            root.addHandler(handler)
            root.setLevel(logging.DEBUG if args.verbose else logging.WARNING)
            log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
            # python-kasa logs every unreachable host during discovery; we report failures ourselves.
            logging.getLogger("kasa").setLevel(logging.DEBUG if args.verbose else logging.CRITICAL)
            app = App(config)
            await app.run(start_server=config.server["autostart"] and not args.no_server)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

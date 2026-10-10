"""fleet-deck: the one command that brings the Fleet Deck back.

`fleet-deck` makes sure herdr's server runs, makes sure one deck controller
(fabric-deck) runs detached, then attaches this terminal to herdr. Run twice,
it starts nothing twice: the server answers its socket, and the controller
holds a record lock (fabric_deck.hold_lock) that a second one cannot take.

The controller is started in a session of its own with no terminal: its
stdin is /dev/null and its output goes to a log file, so closing a terminal
or a herdr tab never reaches it, and it is stopped by `fleet-deck stop`
(SIGTERM), never by a key. It is double-forked, so it is no child of the
herdr client this command becomes and is reaped by init.

`fleet-deck stop` stops the controller only. herdr's server keeps every
agent's pane alive; stopping it (`herdr server stop`) ends every pane's
process, so this command never does.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import time
from typing import Callable

from fabric_deck import (
    BOARD_DIR,
    herdr_socket_path,
    lock_holder,
    lock_path,
    server_instance,
    state_dir,
)

DECK = os.path.join(BOARD_DIR, "fabric-deck")

# How long a just-started server or controller has to show itself before
# the command says it did not come up.
SERVER_READY_S = 15
CONTROLLER_READY_S = 10
# How long a stopped controller has to exit: the deck itself gives its state
# stream child 5 s (fabric_deck.STOP_CHILD_GRACE_S) before killing it.
CONTROLLER_STOP_S = 10
# The log is kept to one previous generation, rotated when a controller starts.
LOG_ROTATE_BYTES = 1 << 20


def log_path() -> str:
    return os.path.join(state_dir(), "deck.log")


def in_herdr_pane() -> bool:
    """Whether this process runs inside a herdr pane. herdr marks a pane's
    environment with HERDR_ENV=1 and a popup's with HERDR_POPUP=1 as well;
    a popup is the operator's own configured command and may drive the deck."""
    return os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_POPUP") != "1"


# The prefix herdr's tests check against every harness's keys
# (src/client/shell/tests/harness_keys.rs); 0x1e on a legacy host is ctrl+^.
CHECKED_PREFIXES = frozenset({"ctrl+6", "ctrl+^"})


def herdr_config_path() -> str:
    """herdr's config file, as herdr resolves it (src/config/io.rs config_path)."""
    if os.environ.get("HERDR_CONFIG_PATH"):
        return os.environ["HERDR_CONFIG_PATH"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "config.toml")


def configured_prefix(path: str) -> list[str] | None:
    """keys.prefix as the config sets it, or None when it leaves herdr's
    default (ctrl+6) or cannot be read."""
    import tomllib

    try:
        with open(path, "rb") as handle:
            prefix = tomllib.load(handle).get("keys", {}).get("prefix")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(prefix, str):
        return [prefix]
    if isinstance(prefix, list) and all(isinstance(p, str) for p in prefix):
        return prefix
    return None


def prefix_line(path: str) -> tuple[str, bool]:
    """What the prefix is, and whether it is one the harness check covers."""
    prefix = configured_prefix(path)
    if prefix is None:
        return "prefix: ctrl+6 (herdr's default)", True
    shown = " / ".join(prefix)
    checked = all(p.strip().lower().replace(" ", "") in CHECKED_PREFIXES for p in prefix)
    return f"prefix: {shown} (keys.prefix in {path})", checked


def default_catalog() -> str | None:
    """The role catalogue the deck seeds workspaces from: the agent-fabric
    checkout's, AGENT_FABRIC_ROOT else ~/projects/agent-fabric, when it exists."""
    root = os.environ.get("AGENT_FABRIC_ROOT") or os.path.expanduser("~/projects/agent-fabric")
    path = os.path.join(root, "identities", "roles", "catalog.json")
    return path if os.path.exists(path) else None


def spawn_detached(argv: list[str], log: str | None) -> None:
    """Run `argv` in a new session, with no terminal, as a grandchild that
    init adopts at once. stdin is /dev/null; stdout and stderr append to
    `log`, or /dev/null without one."""
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600) if log else null
        os.dup2(null, 0)
        os.dup2(out, 1)
        os.dup2(out, 2)
        os.closerange(3, os.sysconf("SC_OPEN_MAX") if hasattr(os, "sysconf") else 1024)
        # The stop and interrupt the deck relies on: never inherit an ignore.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.execvp(argv[0], argv)
    except OSError as error:
        # stderr is the log here (or /dev/null): the caller tails the log.
        os.write(2, f"fleet-deck: cannot run {argv[0]}: {error}\n".encode())
    finally:
        os._exit(127)


def wait_for(probe: Callable[[], object], seconds: float, step: float = 0.2):
    deadline = time.monotonic() + seconds
    while True:
        value = probe()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


def server_up() -> tuple[int, int] | None:
    return server_instance(herdr_socket_path())


def ensure_server(say: Callable[[str], None]) -> bool:
    if server_up():
        return True
    if shutil.which("herdr") is None:
        say("herdr is not on PATH")
        return False
    say("starting herdr's server")
    spawn_detached(["herdr", "server"], None)
    if wait_for(server_up, SERVER_READY_S):
        return True
    # herdr writes its own log; its stderr is /dev/null once detached.
    say(f"herdr's server did not answer within {SERVER_READY_S}s; "
        "`herdr server` in a terminal shows why")
    return False


def rotate_log(path: str) -> None:
    try:
        if os.path.getsize(path) > LOG_ROTATE_BYTES:
            os.replace(path, path + ".1")
    except FileNotFoundError:
        pass


def log_tail(path: str, lines: int = 5) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()[-lines:]
    except FileNotFoundError:
        return []


def deck_argv(args) -> list[str]:
    argv = [DECK]
    catalog = args.catalog or default_catalog()
    if catalog:
        argv += ["--catalog", catalog]
    for login in args.exclude:
        argv += ["--exclude", login]
    if args.cwd:
        argv += ["--cwd", args.cwd]
    if args.no_fleet_tab:
        argv.append("--no-fleet-tab")
    return argv


def deck_options_given(args) -> bool:
    return bool(args.catalog or args.exclude or args.cwd or args.no_fleet_tab)


def ensure_controller(args, say: Callable[[str], None]) -> bool:
    holder = lock_holder(lock_path())
    if holder is not None:
        if deck_options_given(args):
            say(f"the deck controller already runs (pid {holder}); its options are unchanged "
                "(fleet-deck restart <options> applies them)")
        return True
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    log = log_path()
    rotate_log(log)
    say(f"starting the deck controller (log: {log})")
    spawn_detached(deck_argv(args), log)
    if wait_for(lambda: lock_holder(lock_path()), CONTROLLER_READY_S):
        return True
    say(f"the deck controller did not start within {CONTROLLER_READY_S}s; its log ends:")
    for line in log_tail(log):
        say(f"  {line}")
    return False


def stop_controller(say: Callable[[str], None]) -> bool:
    holder = lock_holder(lock_path())
    if holder is None:
        say("the deck controller is not running")
        return True
    if holder <= 0:
        # The kernel reports 0 for a holder in another pid namespace, and
        # kill(0) would signal this command's own process group.
        say("the deck controller runs where this command cannot see its pid; "
            "stop it from the namespace it runs in")
        return False
    try:
        os.kill(holder, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if wait_for(lambda: lock_holder(lock_path()) is None, CONTROLLER_STOP_S):
        say(f"the deck controller (pid {holder}) stopped")
        return True
    say(f"the deck controller (pid {holder}) did not stop within {CONTROLLER_STOP_S}s")
    return False


def status(say: Callable[[str], None]) -> int:
    server = server_up()
    holder = lock_holder(lock_path())
    say(f"herdr server: {'running, pid %d' % server[0] if server else 'not running'}")
    say(f"deck controller: {'running, pid %d' % holder if holder is not None else 'not running'}")
    say(f"log: {log_path()}")
    say(prefix_line(herdr_config_path())[0])
    # LSB status codes: 0 running, 3 not running.
    return 0 if server and holder is not None else 3


def attach() -> int:
    os.execvp("herdr", ["herdr"])
    return 127  # not reached


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="fleet-deck",
        description="Fleet Deck in one command: herdr's server, the deck controller "
        "(detached) and this terminal attached. A second run only attaches.",
        epilog="stop and restart act on the deck controller only; herdr's server keeps "
        "every agent's pane running (herdr server stop would end them).",
    )
    parser.add_argument(
        "verb", nargs="?", default="attach",
        choices=["attach", "start", "stop", "restart", "status"],
        help="attach (default): start what is missing, then attach this terminal; "
        "start: the same without attaching; stop|restart: the controller; "
        "status: what runs (exit 0 when both run, 3 otherwise)",
    )
    parser.add_argument("--catalog", help="the role catalogue (default: the agent-fabric checkout's)")
    parser.add_argument("--exclude", action="append", default=[], help="a login to leave out")
    parser.add_argument("--cwd", help="the deck's working directory for new tabs")
    parser.add_argument("--no-fleet-tab", action="store_true",
                        help="do not open the fleet board as a tab of its own")
    args = parser.parse_args(argv)

    def say(line: str) -> None:
        print(f"fleet-deck: {line}", file=sys.stderr, flush=True)

    if args.verb == "status":
        return status(say)
    if args.verb == "stop":
        return 0 if stop_controller(say) else 1

    if in_herdr_pane():
        # server.socket_access = "outside_panes" refuses a pane's process, and
        # a controller started here would be the pane's descendant.
        say("run this from a terminal outside herdr's panes")
        return 2
    line, checked = prefix_line(herdr_config_path())
    if not checked:
        say(f"{line}: not the prefix checked against Claude Code, Codex and the shell; "
            "remove keys.prefix to use ctrl+6")
    if args.verb == "restart" and not stop_controller(say):
        return 1
    if not ensure_server(say):
        return 1
    if not ensure_controller(args, say):
        return 1
    if args.verb == "attach":
        return attach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

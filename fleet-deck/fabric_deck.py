"""Fleet Deck: the operator's console over the fleet, on herdr.

One herdr tab per agent account, labelled with its login, holding three
panes: the harness (moveto <account> --wait, activated by one Enter), the
account's shell, and its status. The deck restores the tabs after a herdr or
host restart and then follows them, showing each harness pane's state in
herdr's agent panel.

The deck owns no session ids. The control plane owns which session belongs to
which account; herdr keeps only the layout; moveto and fabric-resume start
sessions. The deck observes the panes and the state stream and acts on the
tabs only by starting a fixed moveto in the operator's own bare shell
(agent-fabric docs/fleet-deck/tab-states.md, #115).

The states and decisions are pure (deck_tabs.py); the `Deck` runs them with
herdr, the stream and /proc injected, so its rules are tested without a
server. The adapters at the bottom run the real commands.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterable

from deck_tabs import (
    HARNESS,
    HARNESS_RATIO,
    MODES,
    PANE_ROLES,
    PLAIN,
    RESTORE_WAIT_S,
    RESUME,
    SHELL,
    SSH,
    STATUS,
    WAIT,
    WATCH,
    Before,
    Live,
    Seen,
    Shown,
    State,
    Track,
    at_start,
    before_live,
    classify,
    display,
    is_harness,
    live_from_record,
    lost,
    moveto_in,
    note_live,
    restore,
    settle,
    step,
)

# The workspace an account goes to when the catalogue names no group for it,
# or when its group's workspace no longer exists (the operator removed it).
NEW_WORKSPACE = "New"
# The fleet board's own tab: the views' plugin (herdr-plugin.toml), its
# board entrypoint, under this label. No login is this word: a login with
# a hyphenated suffix or a digit is what the registry places.
FLEET_TAB = "fleet"
FLEET_PLUGIN = "fabric.fleet"
FLEET_ENTRYPOINT = "board"
# The plugin's root, where herdr starts and restores the board's pane: this
# file's directory, as README's `herdr plugin link <checkout>/fleet-deck`.
BOARD_DIR = os.path.dirname(os.path.realpath(__file__))

# A login is typed into the operator's shell as part of a moveto command, so
# only a plain Linux login is ever used; anything else is skipped, said.
SAFE_LOGIN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Account:
    login: str
    role: str


def parse_moveto_list(text: str, exclude: Iterable[str]) -> list[Account]:
    """Accounts from `moveto --list` ("account role clones..." per line)."""
    excluded = set(exclude)
    accounts = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] not in excluded:
            accounts.append(Account(login=fields[0], role=fields[1]))
    return accounts


def seed_workspace(role: str, catalog: dict | None) -> str:
    """The workspace the role catalogue seeds for a role, or NEW_WORKSPACE."""
    if not catalog:
        return NEW_WORKSPACE
    groups = set(catalog.get("groups") or [])
    for entry in catalog.get("roles") or []:
        if entry.get("id") == role and entry.get("group") in groups:
            return entry["group"]
    return NEW_WORKSPACE


# ------------------------------------------------------------------ layout


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    tab_id: str
    label: str | None


@dataclass(frozen=True)
class AccountTab:
    """A herdr tab whose label is an account login, and its panes by role.
    `adopt` is a single unlabelled pane (a milestone-1 tab) to label as the
    harness; `undetermined` is a tab the operator split by hand, whose panes
    the deck cannot tell apart and so never touches."""

    login: str
    tab_id: str
    workspace_id: str
    panes: dict[str, str]
    adopt: str | None = None
    undetermined: bool = False


def read_tab(login: str, tab_id: str, workspace_id: str, panes: list[PaneInfo]) -> AccountTab:
    by_role = {p.label: p.pane_id for p in panes if p.label in PANE_ROLES}
    if HARNESS in by_role:
        return AccountTab(login, tab_id, workspace_id, by_role)
    if len(panes) == 1 and not panes[0].label:
        return AccountTab(login, tab_id, workspace_id, by_role, adopt=panes[0].pane_id)
    return AccountTab(login, tab_id, workspace_id, by_role, undetermined=True)


# ------------------------------------------------------------- the stream


@dataclass(frozen=True)
class Session:
    session: str
    state: str
    since: str


@dataclass(frozen=True)
class StateRecord:
    """One account's row of `fabric-ctl all states --json`."""

    login: str
    state: str
    sessions: tuple[Session, ...]
    ts: str
    # The host part of the stream's address: fabric-ctl all states reports
    # every host, and the same login may exist on more than one.
    host: str = ""
    last_session: str | None = None
    resumable: bool | None = None


def parse_state_line(line: str) -> StateRecord | None:
    """A record, or None when the line is not one. The stream's row for an
    account with no state on the channel has no `ts`: that is no record.
    Rows come from other accounts: a field of the wrong type is read as
    absent, never trusted to have the shape the deck expects."""
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):  # deeply nested input is not a record
        return None
    if not isinstance(row, dict):
        return None
    address, ts = row.get("address"), row.get("ts")
    if not isinstance(address, str) or not isinstance(ts, str) or parse_utc(ts) is None:
        return None
    rows = row.get("sessions")
    sessions = tuple(
        Session(s["session"], text_or(s.get("state"), "unknown"), text_or(s.get("since"), ""))
        for s in (rows if isinstance(rows, list) else [])
        if isinstance(s, dict) and isinstance(s.get("session"), str) and s["session"]
    )
    host, _, login = address.rpartition("/")
    resumable = row.get("resumable")
    return StateRecord(
        login=login,
        host=host,
        state=text_or(row.get("state"), "unknown"),
        sessions=sessions,
        ts=ts,
        last_session=text_or(row.get("last_session"), None),
        resumable=resumable if isinstance(resumable, bool) else None,
    )


def text_or(value, default):
    return value if isinstance(value, str) else default


def parse_utc(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def for_this_host(record: StateRecord | None, host: str | None) -> bool:
    """Whether a stream record describes an account on the deck's own host.
    With the host unknown every record is taken, as before hosts mattered."""
    return record is not None and (host is None or record.host == host)


def live_of(record: StateRecord | None, now_utc: datetime.datetime) -> Live | None:
    # A record whose state is "unknown" (the account cannot read its session
    # state: sessions [] and a fresh ts, agent-fabric #179) says nothing about
    # sessions. Read as live = 0 it would note a fall, and a session that died
    # with herdr would not be resumed; it is no record instead.
    if record is None or record.state == "unknown":
        return None
    posted = parse_utc(record.ts)
    if posted is None:
        return None
    age = (now_utc - posted).total_seconds()
    return live_from_record([(s.session, s.state) for s in record.sessions], age)


def printable(text: str) -> str:
    """Another account's pane text, safe to print on the operator's terminal."""
    return "".join(ch for ch in text if ch == "\t" or (ch.isprintable() and ord(ch) >= 0x20))


# ------------------------------------------------------- the process walk


# Shells a restored pane starts: herdr's default program for a pane.
SHELLS = frozenset({"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh"})


def is_board(argv: list[str]) -> bool:
    """fabric-view's board: the launcher execs a Python whose `-c` code
    imports fabric_view, with the mode as the last argument."""
    return bool(argv) and argv[-1] == FLEET_ENTRYPOINT and any(
        "from fabric_view import main" in arg for arg in argv[1:])


def same_dir(a: str | None, b: str) -> bool:
    return isinstance(a, str) and os.path.realpath(a) == os.path.realpath(b)


def at_bare_shell(info: dict) -> bool:
    """A shell at its prompt: its own foreground group, and a shell's argv.
    A plugin pane's program also leads its own group, so the group alone
    would take a running view for a shell."""
    shell, group = info.get("shell_pid"), info.get("foreground_process_group_id")
    if shell is None or shell != group:
        return False
    fg = info.get("foreground_processes") or []
    return bool(fg) and all(os.path.basename((p.get("argv") or ["?"])[0]).lstrip("-") in SHELLS for p in fg)


def ssh_harness(live: Live | None) -> bool | None:
    """Whether a harness runs in a pane entered over ssh, from the stream.
    sshd starts the account's session, so its harness is no descendant of
    the pane's `ssh` and the /proc walk cannot see it; nothing the deck may
    read links an ssh client to the sshd session it opened. The account's
    live session is taken as this pane's: the deck armed it, and an Enter
    in a second pane is refused by fabric-resume. A session started
    elsewhere therefore reads as running here, never as `running
    elsewhere`. A stale or missing record says nothing (None)."""
    if live is None or not live.fresh:
        return None
    return live.count >= 1


def proc_parents(proc_root: str = "/proc") -> dict[int, int]:
    """pid -> parent pid, from each process's `stat`. Only `stat` is read here:
    another account's processes are walked, never inspected."""
    parents: dict[int, int] = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return parents
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "stat"), encoding="utf-8", errors="replace") as f:
                stat = f.read()
        except OSError:
            continue  # gone since the listing
        # "pid (comm) state ppid ...": comm may hold spaces and parentheses.
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) > 1 and fields[1].isdigit():
            parents[int(name)] = int(fields[1])
    return parents


def proc_argv(pid: int, proc_root: str = "/proc") -> list[str]:
    """A descendant's `cmdline`, the one other file of another account's
    process the deck reads (fabric-coordinator, 01a11ab6-46f3)."""
    try:
        with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def harness_under(root: int, parents: dict[int, int], argv: Callable[[int], list[str]]) -> bool:
    """Whether a harness runs among the descendants of `root` (a pane's
    moveto sudo). sudo runs the account in a pty of its own (use_pty), so
    the harness is never in the pane's foreground and is found here instead
    (measured live, GZCoord seq 22145)."""
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    queue, seen = list(children.get(root, [])), set()
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if is_harness(argv(pid)):
            return True
        queue.extend(children.get(pid, []))
    return False


# ------------------------------------------------------------ the record


def load_befores(path: str) -> dict[str, Before] | None:
    """The deck's record of whether each account's session was running, read
    only at a restore. None when there is none yet (a first run); an entry
    of the wrong shape is read as absent."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return None
    except ValueError:
        return {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return {}
    befores = {}
    for login, entry in accounts.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("running"), bool):
            continue
        fall_at, server = entry.get("fall_at"), entry.get("fall_server")
        pending = (isinstance(fall_at, (int, float)) and isinstance(server, list) and len(server) == 2
                   and all(isinstance(x, int) for x in server))
        befores[login] = Before(entry["running"], fall_at if pending else None,
                                tuple(server) if pending else None)
    return befores


def save_befores(path: str, befores: dict[str, Before]) -> None:
    """Written whole and renamed into place, readable only by the operator: it
    holds a login, whether its session was running, and a pending fall's time
    and herdr server, nothing else."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    data = {"accounts": {
        login: {"running": b.running, "fall_at": b.fall_at,
                "fall_server": list(b.fall_server) if b.fall_server else None}
        for login, b in befores.items()
    }}
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, path)


def state_dir() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "fabric-deck")


def befores_path() -> str:
    return os.path.join(state_dir(), "before.json")


def lock_path() -> str:
    return os.path.join(state_dir(), "deck.lock")


def hold_lock(path: str) -> int | None:
    """The one-deck-per-login lock: a POSIX record lock on `path`, held for
    the life of the process. None when another deck holds it. A lock, not a
    pid file, so a deck that died leaves nothing to clean: the kernel drops
    it with the process. A record lock, not flock, because `lock_holder`
    can ask the kernel who holds it without taking it: a probe that locked,
    even shared, would make a deck starting in that instant refuse to run."""
    import fcntl

    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    # For a person reading the file; lock_holder asks the kernel instead.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


# struct flock on Linux: l_type, l_whence, l_start, l_len, l_pid.
_FLOCK = "hhqqi"


def lock_holder(path: str) -> int | None:
    """The pid of the deck holding the lock, as the kernel reports it
    (F_GETLK), or None when no deck runs. Takes no lock."""
    import fcntl
    import struct

    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        query = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
        kind, _, _, _, pid = struct.unpack(_FLOCK, fcntl.fcntl(fd, fcntl.F_GETLK, query))
    finally:
        # Closing any descriptor of the file drops the record locks this
        # process holds on it, so the deck itself never calls this.
        os.close(fd)
    return None if kind == fcntl.F_UNLCK else pid


def herdr_socket_path() -> str | None:
    """The socket the deck's herdr commands reach, as herdr itself resolves
    it when no --session is passed (src/session.rs configure_from_args and
    active_api_socket_path): HERDR_SOCKET_PATH, else the session
    HERDR_SESSION names, else the default session; each session's socket as
    herdr lists it, never re-derived here."""
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    named = os.environ.get("HERDR_SESSION")
    if named == "default":
        named = None
    try:
        result = subprocess.run(["herdr", "session", "list", "--json"],
                                capture_output=True, text=True, timeout=30)
        sessions = json.loads(result.stdout).get("result", json.loads(result.stdout)).get("sessions", [])
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None
    wanted = (lambda s: s.get("name") == named) if named else (lambda s: s.get("default"))
    return next((s.get("socket_path") for s in sessions if isinstance(s, dict) and wanted(s)), None)


def server_instance(path: str | None, proc_root: str = "/proc") -> tuple[int, int] | None:
    """herdr's server instance: the pid at the socket's other end
    (SO_PEERCRED) and that process's raw start time in clock ticks (field 22
    of its stat), so a server restarted under a reused pid is still another
    instance. None when the socket does not answer or the process cannot be
    read, which the deck treats as a loss."""
    import socket
    import struct

    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(path)
            cred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    pid = struct.unpack("3i", cred)[0]
    ticks = start_ticks(pid, proc_root)
    return (pid, ticks) if ticks is not None else None


def start_ticks(pid: int, proc_root: str = "/proc") -> int | None:
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    # starttime is the 22nd field of stat, the 20th after "pid (comm)".
    return int(fields[19]) if len(fields) > 19 and fields[19].isdigit() else None


# ---------------------------------------------------------------- the deck

AGENT_SOURCE = "fabric"
AGENT_LABEL = "claude"
# herdr's pane metadata token naming the account (src/transition_sound.rs).
ACCOUNT_TOKEN = "account"
# How often the deck looks at every harness pane.
POLL_S = 2
# How long a restore waits for the changes the restart caused to reach the
# stream (one STATE_POLL_MS plus a margin) before it trusts a fresh record.
RESTORE_SETTLE_S = 5
# The account list and the tab map are read again this often.
PANE_MAP_REFRESH_S = 10
# Every pane's report is sent again after this long even when nothing
# changed, so a herdr server restarted with the same pane ids (which
# forgets every report) is put right.
RESEND_AFTER_S = 600


@dataclass
class Deck:
    """Restores the account tabs and follows their harness panes. herdr, the
    account list, the stream's records, /proc, the record of what was shown,
    the clock and the log are injected."""

    herdr: "Herdr"
    accounts: Callable[[], list[Account]]
    # Written by the stream's thread, one whole record per assignment.
    records: dict[str, StateRecord]
    modes: frozenset[str]
    cwd: str
    catalog: dict | None
    parents: Callable[[], dict[int, int]]
    argv: Callable[[int], list[str]]
    load: Callable[[], dict[str, Before] | None]
    save: Callable[[dict[str, Before]], None]
    server: Callable[[], tuple[int, int] | None]
    log: Callable[[str], None]
    utc: Callable[[], datetime.datetime] = field(
        default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    tracks: dict[str, Track] = field(default_factory=dict)
    harness_pane: dict[str, str] = field(default_factory=dict)
    # login -> when its harness pane began waiting for the restore decision
    pending: dict[str, float] = field(default_factory=dict)
    # Accounts whose live = 0 dates from before their restore decision: no fall
    # is noted for them until a session has been seen up again.
    unsettled: set[str] = field(default_factory=set)
    # Accounts whose restore failed part-way, restored again at the next map read.
    retry: set[str] = field(default_factory=set)
    # (login, pane, mode) -> since when a shell or status pane waits to be bare
    waiting: dict[tuple[str, str, str], float] = field(default_factory=dict)
    # login -> (what the panel shows, when it was sent)
    sent: dict[str, tuple[Shown, float]] = field(default_factory=dict)
    befores: dict[str, Before] = field(default_factory=dict)
    loaded: bool = False
    instance: tuple[int, int] | None = None
    placed: set[str] = field(default_factory=set)
    unarmed: set[str] = field(default_factory=set)
    said: set[str] = field(default_factory=set)
    mapped_at: float | None = None
    lost: bool = False
    fleet_tab: bool = True
    # The fleet tab whose pane had not started its program when last looked
    # at: looked at again at a map read while it is still there as `fleet`.
    fleet_unsettled: str | None = None

    # ------------------------------------------------------------ restore

    def restore(self, now: float, only: set[str] | None = None) -> None:
        """Every placed account gets its tab and panes. A harness pane with
        moveto running is classified and never re-armed; one at the
        operator's bare shell, or just created, waits for the restore
        decision."""
        try:
            self._restore(now, only)
        except Exception as error:  # herdr failing while it comes back must not end the deck
            self._say("restore-failed", f"cannot restore yet: {printable(str(error))}; trying again")
            self.lost = True
            return
        if not self.lost:
            # Said once per loss: a later loss and restore are said again.
            self.said -= {"restoring", "restore-failed"}

    def _restore(self, now: float, only: set[str] | None = None) -> None:
        if not self.loaded:
            self._load()
        accounts = self._accounts()
        tabs = self._tabs({a.login for a in accounts}) if accounts is not None else None
        if tabs is None:
            self.lost = True
            return
        self.placed = {a.login for a in accounts}
        workspace_labels = {w.get("label") for w in self.herdr.workspaces()}
        first_setup = not tabs
        spare: list[str] = []
        try:
            for account in accounts:
                if only is not None and account.login not in only:
                    continue
                try:
                    self._restore_account(account, tabs.get(account.login), workspace_labels,
                                          first_setup, spare, now)
                    self.retry.discard(account.login)
                except Exception as error:  # one account's failure must not stop the others
                    self.log(f"{account.login}: cannot restore its tab yet: {printable(str(error))}")
                    # Nothing of a half-done restore is followed: the account
                    # is restored whole at the retry, its decision taken then.
                    self._forget(account.login)
                    self.retry.add(account.login)
        finally:
            for tab_id in spare:
                try:
                    self.herdr.call("tab", "close", tab_id)
                except Exception as error:
                    self.log(f"cannot close the spare tab {tab_id}: {printable(str(error))}")
        if only is None and self.fleet_tab:
            self._restore_fleet_tab()
        self.mapped_at = now

    def _restore_fleet_tab(self, recheck: str | None = None) -> None:
        """The fleet board as a tab of its own, labelled `fleet`, in the first
        workspace, made only when no tab has that label: like an account's
        pane, one a person closes stays closed until the next restore. The
        board is opened unfocused, so it reads once and waits for a look.
        `recheck` is the tab left unsettled at the last look: at a map read
        only that tab is looked at again, and only while it is still there
        as `fleet`. Every herdr call is in here, under the handler, so a
        herdr failure is said and never ends the deck."""
        try:
            workspaces = self.herdr.workspaces()
            listings = [(w, self.herdr.call("tab", "list", "--workspace", w["workspace_id"])) for w in workspaces]
            if recheck is not None and not any(
                    tab.get("tab_id") == recheck and tab.get("label") == FLEET_TAB
                    for _, listing in listings for tab in listing.get("tabs", [])):
                # The unsettled tab was closed or renamed by a person since:
                # theirs until the next restore, and nothing is opened.
                self.fleet_unsettled = None
                return
            for workspace, listing in listings:
                for tab in listing.get("tabs", []):
                    if tab.get("label") != FLEET_TAB:
                        continue
                    if recheck is not None and tab.get("tab_id") != recheck:
                        continue
                    held = self._fleet_tab_holds(tab["tab_id"])
                    self.fleet_unsettled = tab["tab_id"] if held == "unsettled" else None
                    if held == "remnant":
                        # herdr restores a tab by its label but not a plugin
                        # pane's program: the board's tab comes back as the
                        # operator's bare shell. That tab is the deck's own.
                        self.herdr.call("tab", "close", tab["tab_id"])
                        self.log(f"the {FLEET_TAB} tab held only a shell after herdr came back; reopening the board")
                        break
                    if held == "other":
                        self._say("fleet-tab-other", f"the {FLEET_TAB} tab runs something else; left alone")
                    # unsettled: looked at again at the next map read.
                    return
                else:
                    continue
                break
            if not workspaces:
                return
            made = self.herdr.call("plugin", "pane", "open", "--plugin", FLEET_PLUGIN,
                                   "--entrypoint", FLEET_ENTRYPOINT,
                                   "--workspace", workspaces[0]["workspace_id"], "--no-focus")
            self.herdr.call("tab", "rename", made["plugin_pane"]["pane"]["tab_id"], FLEET_TAB)
            self.log(f"opened the {FLEET_TAB} tab")
        except Exception as error:  # the board is a view: no account's tab waits on it
            self._say("fleet-tab", f"no {FLEET_TAB} tab: {printable(str(error))} "
                      f"(is {FLEET_PLUGIN} linked? herdr plugin link <checkout>/fleet-deck)")

    def _restore_account(self, account, tab, workspace_labels, first_setup, spare, now) -> None:
        login = account.login
        if not SAFE_LOGIN.match(login):
            self._say(login, f"{login!r}: not a plain login; its tab is left alone")
            return
        if tab is None:
            seeded = seed_workspace(account.role, self.catalog)
            label = seeded if first_setup or seeded in workspace_labels else NEW_WORKSPACE
            workspace = ensure_workspace(self.herdr, label, self.cwd, spare)
            made = self.herdr.call("tab", "create", "--workspace", workspace, "--cwd", self.cwd,
                                   "--label", login, "--no-focus")
            tab = AccountTab(login, made["tab"]["tab_id"], workspace, {},
                             adopt=made["root_pane"]["pane_id"])
        if tab.undetermined:
            self._say(login, f"{login}: its tab was split by hand and has no harness pane; left alone")
            return
        panes = dict(tab.panes)
        if tab.adopt is not None:
            self.herdr.call("pane", "rename", tab.adopt, HARNESS)
            panes[HARNESS] = tab.adopt
        harness = panes[HARNESS]
        if SHELL not in panes:
            panes[SHELL] = self._split(harness, "right", HARNESS_RATIO, SHELL)
        if STATUS not in panes:
            panes[STATUS] = self._split(panes[SHELL], "down", 0.5, STATUS)
        # Everything the restore owes this account is recorded before the
        # first observation, so a herdr call failing below leaves it waiting
        # for its decision, never followed half-restored. The harness waits
        # for the decision at its prompt, the shell and status panes to be
        # started at theirs; decide_pending drops whichever turns out to run
        # this account's moveto already.
        self.harness_pane[login] = harness
        self.pending[login] = now
        self.unsettled.add(login)
        self.tracks[login] = Track(State.IDLE)
        for role, mode in ((SHELL, PLAIN), (STATUS, WATCH)):
            self.waiting[(login, panes[role], mode)] = now
        seen = self.observe(login, harness)
        if seen.moveto is True:
            # The account's moveto holds the pane: classified, never re-armed.
            del self.pending[login]
            self.unsettled.discard(login)
            self.tracks[login] = classify(seen, self.live(login)) or Track(State.IDLE)

    def _fleet_tab_holds(self, tab_id: str) -> str:
        """`board` when the board's view runs in the tab; `remnant` when its
        one pane is a bare shell in the board's own directory (where herdr
        restores the pane, the plugin's root); `unsettled` while a pane's
        program has not started; else `other`: a person's, never closed."""
        panes = [p for p in self.herdr.call("pane", "list").get("panes", []) if p.get("tab_id") == tab_id]
        infos = [self.herdr.process_info(p["pane_id"]) for p in panes]
        if any(is_board(p.get("argv") or []) for info in infos for p in info.get("foreground_processes", [])):
            return "board"
        if any(info.get("shell_pid") is None or not info.get("foreground_processes") for info in infos):
            return "unsettled"
        if len(infos) == 1 and at_bare_shell(infos[0]) and same_dir(panes[0].get("cwd"), BOARD_DIR):
            return "remnant"
        return "other"

    def _split(self, pane: str, direction: str, ratio: float, label: str) -> str:
        made = self.herdr.call("pane", "split", pane, "--direction", direction,
                               "--ratio", str(ratio), "--cwd", self.cwd, "--no-focus")
        new = made["pane"]["pane_id"]
        self.herdr.call("pane", "rename", new, label)
        return new

    def decide_pending(self, now: float) -> None:
        for key, since in list(self.waiting.items()):
            login, pane, mode = key
            try:
                seen = self.observe(login, pane)
                if seen.moveto is False:
                    del self.waiting[key]
                    self._arm(login, pane, mode)
                elif seen.moveto is True:
                    del self.waiting[key]  # a person started it meanwhile
                elif now - since >= RESTORE_WAIT_S:
                    self._say(f"{pane}:busy", f"{login}: pane {pane} is still busy; "
                              "its moveto is started when it reaches its prompt")
            except Exception as error:  # kept waiting; the map is read again first
                self.log(f"{login}: {printable(str(error))}")
                self.mapped_at = None
                return
        for login, since in list(self.pending.items()):
            waited = now - since
            live = self.live(login)
            fresh = live is not None and live.fresh
            if waited < RESTORE_SETTLE_S or (not fresh and waited < RESTORE_WAIT_S):
                continue
            pane = self.harness_pane.get(login)
            try:
                seen = self.observe(login, pane) if pane is not None else Seen(present=False)
                if seen.moveto is True:
                    # A person started this account's moveto meanwhile: followed, not armed.
                    del self.pending[login]
                    self.tracks[login] = classify(seen, live) or Track(State.IDLE)
                    continue
                if seen.moveto is not False:
                    if waited >= RESTORE_WAIT_S:
                        self._say(f"{pane}:busy", f"{login}: its harness pane is still busy; "
                                  "the restore decision is taken when it reaches its prompt")
                    continue
                track, arm = restore(before_live(self.befores.get(login), live), live, now)
                self._arm(login, pane, arm.mode)
            except Exception as error:  # kept pending; the map is read again first
                self.log(f"{login}: {printable(str(error))}")
                self.mapped_at = None
                return
            del self.pending[login]
            if login not in self.unarmed:
                self.tracks[login] = track

    # ------------------------------------------------------------- follow

    def follow(self, now: float) -> None:
        """One look at every followed harness pane."""
        server = self.server()
        if server is None:
            self._lose("herdr's server does not answer; waiting for it")
            return
        if self.instance is not None and server != self.instance:
            # Another server answers: the panes the deck knew are gone with the
            # old one, even though no request failed.
            self._lose("herdr's server was restarted")
        self.instance = server
        # While herdr's server is lost, only the map is read, until it answers.
        if self.lost or self.mapped_at is None or now - self.mapped_at >= PANE_MAP_REFRESH_S:
            if not self._refresh(now):
                return
            if self.retry:
                self.restore(now, only=set(self.retry))
        self.decide_pending(now)
        self._note_befores(server)
        parents = self.parents()
        for login, pane in list(self.harness_pane.items()):
            if login in self.pending:
                continue
            try:
                seen = self.observe(login, pane, parents)
                live = self.live(login)
                track, arm = step(self.tracks.get(login, Track(State.IDLE)), seen, live, now)
                self.tracks[login] = track
                if arm is not None:
                    self._arm(login, pane, arm.mode)
                elif track.halted:
                    self._say(f"{login}:halted", f"{login}: moveto ended at once twice in a row and is "
                              "not started again; start it in the pane, or restart the deck, to try again. "
                              "The pane's last lines say why")
                else:
                    # Lifted: a later halt is said again.
                    self.said.discard(f"{login}:halted")
                # An unarmed harness pane holds no agent, and one whose foreground
                # is something other than this account's moveto (an editor, a
                # command the operator ran) is not the deck's to name: herdr's
                # own word for both, never "dormant" with Enter going elsewhere.
                unknown = login in self.unarmed or (seen.present and seen.moveto is None)
                shown = Shown("unknown", "unknown") if unknown else display(self.tracks[login], live)
                self._show(login, pane, shown, now)
            except Exception as error:  # one account's failure must not stop the others
                self.log(f"{login}: {printable(str(error))}")
                self.mapped_at = None

    def _refresh(self, now: float) -> bool:
        """Read the tab map again. A harness pane that is gone (closed, or its
        shell ended) is followed again only after the next restore. A herdr
        server that could not be reached and now answers again was
        restarted: that is a restore."""
        self.mapped_at = now
        accounts = self._accounts()
        if accounts is None:
            # moveto --list failed, not herdr: the map read last stays.
            return not self.lost
        tabs = self._tabs({a.login for a in accounts}, quiet=self.lost)
        if tabs is None:
            self._lose("herdr's server does not answer; waiting for it")
            return False
        if self.lost:
            self.lost = False
            self._say("restoring", "herdr's server answers: restoring")
            for kept in (self.tracks, self.harness_pane, self.pending, self.waiting, self.sent):
                kept.clear()
            self.restore(now)
            return not self.lost
        newly = {a.login for a in accounts} - self.placed
        self.placed = {a.login for a in accounts}
        self.retry &= self.placed
        # An account placed while the deck runs gets its tab now; one whose
        # harness a person closed stays closed until the next restore.
        self.retry |= newly
        listed = {pane for tab in tabs.values() for pane in tab.panes.values()}
        for key in [k for k in self.waiting if k[1] not in listed]:
            del self.waiting[key]  # closed by a person: re-created only at a restore
        for login in list(self.harness_pane):
            tab = tabs.get(login)
            if tab is None or tab.panes.get(HARNESS) != self.harness_pane[login]:
                self._forget(login)
        if self.fleet_tab and self.fleet_unsettled is not None:
            self._restore_fleet_tab(recheck=self.fleet_unsettled)
        return True

    def _forget(self, login: str) -> None:
        """The account's panes are followed no more, until a restore. A pane
        the deck reported into gives back the agent row, name and labels the
        deck set, so a pane that is still there (relabelled, or its account
        gone from moveto --list) does not keep them. Whether its session was
        running (`befores`, `unsettled`) is kept."""
        pane = self.harness_pane.get(login)
        if pane is not None and login in self.sent:
            for command in release_commands(pane):
                try:
                    self.herdr.call(*command)
                except Exception:  # a pane that is gone has nothing to give back
                    break
        self.harness_pane.pop(login, None)
        self.pending.pop(login, None)
        self.tracks.pop(login, None)
        self.sent.pop(login, None)
        for key in [k for k in self.waiting if k[0] == login]:
            del self.waiting[key]

    # ---------------------------------------------------------- observing

    def observe(self, login: str, pane: str, parents: dict[int, int] | None = None) -> Seen:
        info = self.herdr.process_info(pane)
        shell, group = info.get("shell_pid"), info.get("foreground_process_group_id")
        if shell is None or group is None:
            return Seen(present=True)
        if shell == group:
            return Seen(present=True, moveto=False)
        foreground = [
            (p["pid"], p.get("argv") or []) for p in info.get("foreground_processes", [])
            if isinstance(p.get("pid"), int)
        ]
        found = moveto_in(foreground, login)
        if found is None:
            # Something else holds the operator's shell in this pane: not this
            # account's moveto, and not the deck's to judge.
            return Seen(present=True)
        if found.via == SSH:
            return Seen(present=True, moveto=True, harness=ssh_harness(self.live(login)), mode=found.mode)
        tree = parents if parents is not None else self.parents()
        return Seen(present=True, moveto=True, harness=harness_under(found.pid, tree, self.argv),
                    mode=found.mode)

    def live(self, login: str) -> Live | None:
        return live_of(self.records.get(login), self.utc())

    # ------------------------------------------------------------- acting

    def _arm(self, login: str, pane: str, mode: str) -> None:
        """Start `moveto <login> <mode>` in a pane at the operator's bare
        shell. A mode moveto does not have yet is not armed, said once: the
        deck builds against stand-ins until agent-fabric's activation PR is on
        main, and arms no pane with what is not there."""
        if mode in MODES and mode not in self.modes:
            self._say(f"{login}:{mode}", f"{login}: moveto has no {mode} yet; that pane is left as it is")
            if pane == self.harness_pane.get(login):
                self.unarmed.add(login)
                self.tracks[login] = Track(State.IDLE)
            return
        if pane == self.harness_pane.get(login):
            self.unarmed.discard(login)
        command = " ".join(filter(None, ("moveto", login, mode)))
        self.herdr.call("pane", "run", pane, command)
        self.log(f"{login}: started `{command}` in {pane}")

    def _bare(self, pane: str) -> bool:
        info = self.herdr.process_info(pane)
        shell = info.get("shell_pid")
        return shell is not None and shell == info.get("foreground_process_group_id")

    def _show(self, login: str, pane: str, shown: Shown, now: float) -> None:
        last = self.sent.get(login)
        if last is None or last[0] != shown or now - last[1] >= RESEND_AFTER_S:
            for command in (account_command(pane, login), report_command(pane, shown),
                            *label_commands(pane, shown)):
                self.herdr.call(*command)
            self.sent[login] = (shown, now)
            if last is None or last[0] != shown:
                self.log(status_line(login, shown.label or shown.status))

    # ------------------------------------------------------------- before

    def _load(self) -> None:
        """The record left by the last run, a pending fall in it settled or
        dropped by whether this herdr server is the one that answered before
        the fall."""
        stored = self.load()
        server = self.server()
        self.befores = {login: at_start(b, server) for login, b in (stored or {}).items()}
        self.loaded = True
        if stored is not None and stored != self.befores:
            self._save(self.befores)

    def _note_befores(self, server: tuple[int, int]) -> None:
        """Each account's live count goes into its record, and a pending fall
        settles once this same server answers SETTLE_S after it. A fall is not
        noted for an account whose restore is still undecided."""
        at = self.utc().timestamp()
        befores = dict(self.befores)
        for login in self.placed:
            live = self.live(login)
            current = befores.get(login, Before())
            if live is not None:
                if live.count >= 1:
                    self.unsettled.discard(login)
                # Undecided (pending, or awaiting a retry of its restore), or
                # decided on a live = 0 from before: no fall is noted yet.
                settling = (login not in self.pending and login not in self.retry
                            and login not in self.unsettled)
                current = note_live(current, live.count, at, server, settling)
            befores[login] = settle(current, server, at)
        # An account no longer placed is dropped from the record here.
        befores = {k: v for k, v in befores.items() if k in self.placed}
        if befores != self.befores:
            self.befores = befores
            self._save(befores)

    def _save(self, befores: dict[str, Before]) -> None:
        try:
            self.save(befores)
        except OSError as error:  # kept in memory; written again at the next change
            self.log(f"cannot write the record of running sessions: {printable(str(error))}")

    def _lose(self, line: str) -> None:
        """herdr-lost: pending falls are discarded, so a session that died with
        herdr is still recorded as running at the restore that ends the loss."""
        if not self.lost:
            self.log(line)
        self.lost = True
        self.instance = None
        befores = {login: lost(b) for login, b in self.befores.items()}
        if befores != self.befores:
            self.befores = befores
            self._save(befores)

    # ------------------------------------------------------------- reading

    def _accounts(self) -> list[Account] | None:
        try:
            return self.accounts()
        except Exception as error:
            self.log(f"cannot read the accounts (moveto --list): {printable(str(error))}")
            return None

    def _tabs(self, logins: set[str], quiet: bool = False) -> dict[str, AccountTab] | None:
        try:
            return self.herdr.account_tabs(logins)
        except Exception as error:
            if not quiet:
                self.log(f"cannot read herdr's tabs: {printable(str(error))}")
            return None

    def _say(self, key: str, line: str) -> None:
        if key not in self.said:
            self.said.add(key)
            self.log(line)


def report_command(pane: str, shown: Shown) -> tuple[str, ...]:
    """Without --seq: herdr refuses a sequence number not above the last one
    from this source, and a restarted deck would start again from zero. The
    pane id comes first: release-agent's parser needs it there, and every
    command the deck sends has the one shape."""
    return ("pane", "report-agent", pane, "--source", AGENT_SOURCE, "--agent", AGENT_LABEL,
            "--state", shown.status)


def account_command(pane: str, login: str) -> tuple[str, ...]:
    """The account the pane runs as, which herdr's per-account transition
    sounds (`[ui.sound.accounts."<login>"]`) are chosen by. Sent before the
    state, so the first transition herdr sees already has its account."""
    return ("pane", "report-metadata", pane, "--source", AGENT_SOURCE, "--agent", AGENT_LABEL,
            "--token", f"{ACCOUNT_TOKEN}={login}")


def release_commands(pane: str) -> list[tuple[str, ...]]:
    """What the deck gives back of a pane it stops following."""
    base = ("pane", "report-metadata", pane, "--source", AGENT_SOURCE, "--agent", AGENT_LABEL)
    return [
        ("pane", "release-agent", pane, "--source", AGENT_SOURCE, "--agent", AGENT_LABEL),
        base + ("--clear-state-labels",),
        base + ("--clear-display-agent",),
        base + ("--clear-token", ACCOUNT_TOKEN),
    ]


def label_commands(pane: str, shown: Shown) -> list[tuple[str, ...]]:
    """The word a person reads in the agent row.

    herdr's default agent row shows the agent's name, not its state text,
    so the deck's word goes into the displayed name ("dormant" instead of
    "claude"), and back to herdr's own name while a harness runs. It also
    goes in as the state's label, for a layout that shows `state_text`; the
    labels from an earlier state are cleared first, so a status that comes
    back later (blocked, once shown as failed) reads as herdr's word again.
    herdr refuses a clear and a set in one call, so they are two."""
    base = ("pane", "report-metadata", pane, "--source", AGENT_SOURCE, "--agent", AGENT_LABEL)
    clear = base + ("--clear-state-labels",)
    word = shown.label
    if not word:
        return [clear, base + ("--clear-display-agent",)]
    command = base + ("--display-agent", word)
    if word != shown.status:
        # herdr shows an idle the person has not looked at yet as done: the
        # deck's word holds for both (measured live on the fork).
        for status in (shown.status, "done") if shown.status == "idle" else (shown.status,):
            command += ("--state-label", f"{status}={word}")
    return [clear, command]


def status_line(login: str, status: str, detail: str = "") -> str:
    suffix = f"  {detail}" if detail else ""
    return f"{login:<28} {status}{suffix}"


# ---------------------------------------------------------------- adapters


@dataclass
class Herdr:
    """herdr's CLI, as the deck's login sees it."""

    binary: str = field(default_factory=lambda: shutil.which("herdr") or "herdr")

    def text(self, *args: str) -> str:
        result = subprocess.run(
            [self.binary, *args], capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:300]
            raise DeckError(f"herdr {' '.join(args)}: exit {result.returncode}: {detail}")
        return result.stdout

    def call(self, *args: str) -> dict:
        out = self.text(*args)
        return json.loads(out).get("result", {}) if out.strip() else {}

    def workspaces(self) -> list[dict]:
        return self.call("workspace", "list").get("workspaces", [])

    def account_tabs(self, logins: set[str]) -> dict[str, AccountTab]:
        """The first tab carrying each login wins; a duplicate is the
        operator's to sort out, not the deck's to close."""
        by_tab: dict[str, list[PaneInfo]] = {}
        for pane in self.call("pane", "list").get("panes", []):
            by_tab.setdefault(pane["tab_id"], []).append(
                PaneInfo(pane["pane_id"], pane["tab_id"], pane.get("label"))
            )
        tabs: dict[str, AccountTab] = {}
        for workspace in self.workspaces():
            listing = self.call("tab", "list", "--workspace", workspace["workspace_id"])
            for tab in listing.get("tabs", []):
                label = tab.get("label")
                if label in logins and label not in tabs and tab["tab_id"] in by_tab:
                    tabs[label] = read_tab(label, tab["tab_id"], workspace["workspace_id"],
                                           by_tab[tab["tab_id"]])
        return tabs

    def process_info(self, pane_id: str) -> dict:
        return self.call("pane", "process-info", "--pane", pane_id).get("process_info", {})


class DeckError(RuntimeError):
    pass


def ensure_workspace(herdr: Herdr, label: str, cwd: str, spare_tabs: list[str]) -> str:
    """The workspace with this label, created if missing. A new workspace
    starts with one numbered tab; it is noted in `spare_tabs` to be closed
    once the account tabs exist, never one the operator had."""
    for workspace in herdr.workspaces():
        if workspace.get("label") == label:
            return workspace["workspace_id"]
    made = herdr.call("workspace", "create", "--label", label, "--cwd", cwd, "--no-focus")
    if made.get("tab", {}).get("tab_id"):
        spare_tabs.append(made["tab"]["tab_id"])
    return made["workspace"]["workspace_id"]


def moveto_list() -> str:
    return subprocess.run(
        ["moveto", "--list"], capture_output=True, text=True, timeout=60, check=True
    ).stdout


def moveto_modes() -> frozenset[str]:
    """The modes this host's moveto has. agent-fabric's activation PR adds
    --wait and --watch, and fabric-resume's second-session refusal with
    them; --resume is armed only once --wait is there, because that refusal
    is what makes arming it safe (tab-states.md, fabric side)."""
    try:
        result = subprocess.run(["moveto", "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    return modes_in(result.stdout + result.stderr)


def modes_in(help_text: str) -> frozenset[str]:
    """The modes a `moveto --help` lists, each as a whole option token, so
    neither "--waitfor" nor "--wait-x" reads as --wait; --resume only with
    --wait."""
    modes = {mode for mode in (WAIT, WATCH, RESUME)
             if re.search(rf"(?<![\w-]){re.escape(mode)}(?![\w-])", help_text)}
    if WAIT not in modes:
        modes.discard(RESUME)
    return frozenset(modes)


def local_host() -> str | None:
    """This host's name as the fleet addresses it (the host part of
    fabric-whoami's address), or None when it cannot be told."""
    try:
        result = subprocess.run(
            ["fabric-whoami", "--json"], capture_output=True, text=True, timeout=30
        )
        address = json.loads(result.stdout).get("address", "")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None
    host, _, _ = address.rpartition("/")
    return host or None


def states_snapshot(log: Callable[[str], None]) -> list[str]:
    """One read of the state stream: a line per placed account."""
    try:
        result = subprocess.run(
            ["fabric-ctl", "all", "states", "--json"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        reason = str(error)
    else:
        # Exit 1 only means some account has no record; the relay's own
        # failures (unreachable, refused) exit higher with nothing on stdout.
        if result.returncode in (0, 1) and result.stdout.strip():
            return result.stdout.splitlines()
        reason = (result.stderr.strip() or f"exit {result.returncode}, no records")[:300]
    log(f"no state snapshot before restoring ({reason}); the stream is waited for")
    return []


def load_catalog(path: str | None) -> dict | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ------------------------------------------------------------- the stream

# The stream child is restarted after it exits, waiting longer each time it
# dies young, and from the start again once it has run a while.
WATCH_BACKOFF_S = (1, 2, 5, 10, 30, 60)
WATCH_HEALTHY_S = 60


def next_backoff(failures: int, lived_s: float) -> tuple[int, int]:
    """After a stream ended having run `lived_s`: the new count of quick
    failures and the seconds to wait before starting it again."""
    failures = 1 if lived_s >= WATCH_HEALTHY_S else failures + 1
    return failures, WATCH_BACKOFF_S[min(failures - 1, len(WATCH_BACKOFF_S) - 1)]


STOP_CHILD_GRACE_S = 5


def stop_child(child: subprocess.Popen) -> None:
    """Terminate the child and reap it, killing it if it outlives the grace.
    A ctrl-c or kill while this runs is held, not acted on, so the child is
    reaped first; then it raises KeyboardInterrupt. Ignoring it instead would
    drop a stop that arrives while the deck is on its way out."""
    import signal

    caught: list[int] = []
    held = {
        sig: signal.signal(sig, lambda signum, _frame: caught.append(signum))
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=STOP_CHILD_GRACE_S)
            except subprocess.TimeoutExpired:
                child.kill()
        child.wait()
    finally:
        for sig, handler in held.items():
            signal.signal(sig, handler)
    if caught:
        raise KeyboardInterrupt


@dataclass
class Stream:
    """`fabric-ctl all states --follow --json` on a reader thread, started
    again after the back-off whenever it ends. Lines go to `on_line`; the
    main thread stops it with `close`, which reaps the child (signals can
    only be held from the main thread)."""

    on_line: Callable[[str], None]
    log: Callable[[str], None]
    command: tuple[str, ...] = ("fabric-ctl", "all", "states", "--follow", "--json")
    child: subprocess.Popen | None = None
    closing: bool = False

    def run(self) -> None:
        import time

        failures = 0
        while not self.closing:
            started = time.monotonic()
            try:
                self.child = subprocess.Popen(self.command, stdout=subprocess.PIPE, text=True)
            except OSError as error:
                self.log(f"cannot start the state stream: {error}")
                self.child = None
            if self.closing and self.child is not None:
                # close() ran while this child was starting and could not see it.
                self.child.terminate()
                self.child.wait()
                return
            if self.child is not None and self.child.stdout is not None:
                for raw in self.child.stdout:
                    try:
                        self.on_line(raw)
                    except Exception as error:  # a bad line must not end the stream
                        self.log(f"a state record was skipped: {printable(str(error))}")
                self.child.wait()
            if self.closing:
                return
            failures, wait = next_backoff(failures, time.monotonic() - started)
            code = self.child.returncode if self.child is not None else "none"
            self.log(f"the state stream ended (exit {code}); restarting in {wait}s")
            time.sleep(wait)

    def close(self) -> None:
        self.closing = True
        if self.child is not None:
            stop_child(self.child)


# --------------------------------------------------------------------- run


def run(deck: Deck, stream: Stream, woken: "threading.Event") -> int:
    """Restore, then follow: every POLL_S, and at once when the stream
    delivers a record, so a harness and its session are seen together."""
    import signal
    import threading
    import time

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    # Stopped by `kill` as by ctrl-c (a backgrounded process ignores SIGINT).
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=stream.run, name="fabric-deck-states", daemon=True).start()
    try:
        try:
            deck.restore(time.monotonic())
            while True:
                deck.follow(time.monotonic())
                woken.wait(POLL_S)
                woken.clear()
        finally:
            stream.close()
    except KeyboardInterrupt:  # also a stop held while the stream child was reaped
        return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass
    import sys
    import threading

    parser = argparse.ArgumentParser(
        prog="fabric-deck",
        description="Fleet Deck: every agent account's herdr tab, restored and followed.",
    )
    parser.add_argument("--catalog", help="the role catalogue (identities/roles/catalog.json)")
    parser.add_argument("--exclude", action="append", default=[], help="a login to leave out")
    parser.add_argument("--cwd", default=os.path.expanduser("~/projects"))
    parser.add_argument("--no-fleet-tab", action="store_true",
                        help="do not open the fleet board as a tab of its own")
    args = parser.parse_args(argv)

    stamped = not sys.stderr.isatty()

    def log(line: str) -> None:
        # Detached (fleet-deck start), stderr is the log file: stamp each line.
        stamp = f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} " if stamped else ""
        print(f"{stamp}fabric-deck: {line}", file=sys.stderr, flush=True)

    lock = hold_lock(lock_path())
    if lock is None:
        holder = lock_holder(lock_path())
        # The holder may have exited between the two calls: say what is known.
        who = f" (pid {holder})" if holder else ""
        log(f"another deck held the lock on this login{who}; this one stops")
        return 1

    host = local_host()
    if host is None:
        log("cannot tell this host's fleet name (fabric-whoami); "
            "state records of the same login on other hosts may be mixed in")
    records: dict[str, StateRecord] = {}
    woken = threading.Event()

    def take(raw: str) -> None:
        record = parse_state_line(raw)
        if for_this_host(record, host):
            records[record.login] = record
            woken.set()

    lines = states_snapshot(log)
    hosts = {r.host for r in map(parse_state_line, lines) if r is not None}
    if host is not None and hosts and host not in hosts:
        # fabric-whoami's short hostname and the stream's placement name
        # disagree: every account would read stale without saying why.
        log(f"no state record is for this host ({host}); the stream names {', '.join(sorted(hosts))}")
    for line in lines:
        take(line)

    exclude = {getpass.getuser(), *args.exclude}
    path = befores_path()
    socket_path = herdr_socket_path()
    modes = moveto_modes()
    missing = sorted({WAIT, WATCH, RESUME} - modes)
    if missing:
        log(f"moveto has no {', '.join(missing)} yet: no pane is armed with it")
    deck = Deck(
        herdr=Herdr(),
        accounts=lambda: parse_moveto_list(moveto_list(), exclude=exclude),
        records=records,
        modes=modes,
        cwd=args.cwd,
        catalog=load_catalog(args.catalog),
        parents=proc_parents,
        argv=proc_argv,
        load=lambda: load_befores(path),
        save=lambda befores: save_befores(path, befores),
        server=lambda: server_instance(socket_path),
        log=log,
        fleet_tab=not args.no_fleet_tab,
    )
    return run(deck, Stream(on_line=take, log=log), woken)


if __name__ == "__main__":
    raise SystemExit(main())

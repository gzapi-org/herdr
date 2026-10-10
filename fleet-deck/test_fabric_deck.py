import datetime
import json
import os
import stat
import tempfile
import unittest

from deck_tabs import RESTORE_WAIT_S, RESUME, SAME_SESSION_S, SETTLE_S, WAIT, WATCH, Before, Shown
from fabric_deck import (
    live_of,
    BOARD_DIR,
    PANE_MAP_REFRESH_S,
    NEW_WORKSPACE,
    RESEND_AFTER_S,
    RESTORE_SETTLE_S,
    Account,
    Deck,
    Herdr,
    Session,
    StateRecord,
    Stream,
    for_this_host,
    harness_under,
    herdr_socket_path,
    account_command,
    label_commands,
    load_befores,
    modes_in,
    next_backoff,
    parse_moveto_list,
    parse_state_line,
    proc_argv,
    proc_parents,
    release_commands,
    report_command,
    save_befores,
    server_instance,
    start_ticks,
    seed_workspace,
    stop_child,
)

CATALOG = {
    "groups": ["Coordinator", "Rust Developer", "Experts"],
    "roles": [
        {"id": "fabric-coordinator", "group": "Coordinator"},
        {"id": "rust-ui-dev", "group": "Rust Developer"},
        {"id": "language-culture", "group": "Experts"},
        {"id": "orphan-role", "group": "A group the catalogue does not list"},
    ],
}

NOW = datetime.datetime(2026, 10, 8, 9, 0, 0, tzinfo=datetime.timezone.utc)
SERVER, OTHER = (4242, 777), (4343, 999)
ENTER = "/usr/local/share/moveto/enter"


class ParseMovetoList(unittest.TestCase):
    def test_reads_account_and_role_and_skips_the_excluded_login(self):
        text = (
            "user               fabric-coordinator  agent-fabric\n"
            "rust-ui-dev-01     rust-ui-dev         herdr interweave\n"
            "\n"
            "lonely\n"
        )
        self.assertEqual(
            parse_moveto_list(text, exclude={"user"}),
            [Account("rust-ui-dev-01", "rust-ui-dev")],
        )


class Seed(unittest.TestCase):
    def test_a_role_goes_to_its_catalogue_group(self):
        self.assertEqual(seed_workspace("rust-ui-dev", CATALOG), "Rust Developer")

    def test_unlisted_role_unlisted_group_or_no_catalogue_go_to_new(self):
        self.assertEqual(seed_workspace("nobody", CATALOG), NEW_WORKSPACE)
        self.assertEqual(seed_workspace("orphan-role", CATALOG), NEW_WORKSPACE)
        self.assertEqual(seed_workspace("rust-ui-dev", None), NEW_WORKSPACE)


class Records(unittest.TestCase):
    def test_parses_a_stream_line_of_today(self):
        line = (
            '{"address":"develop-qzapp/ui","ts":"2026-10-08T05:00:31Z","role":"rust-ui-dev",'
            '"sessions":[{"session":"s1","state":"idle","since":"2026-10-08T05:00:30Z"}],'
            '"state":"idle","since":"2026-10-08T05:00:30Z"}'
        )
        parsed = parse_state_line(line)
        self.assertEqual(
            (parsed.login, parsed.host, [x.session for x in parsed.sessions]),
            ("ui", "develop-qzapp", ["s1"]),
        )
        self.assertIsNone(parse_state_line("not json"))

    def test_a_field_of_the_wrong_type_is_read_as_absent(self):
        ts = '"ts":"2026-10-08T05:00:00Z"'
        for line in ('[1]', '"x"', '{"address":5,%s}' % ts, '{"address":"h/ui","ts":5}'):
            self.assertIsNone(parse_state_line(line), line)
        odd = parse_state_line(
            '{"address":"h/ui",%s,"state":["working"],"last_session":7,"resumable":"yes",'
            '"sessions":[{"session":3},{"session":"s1","state":{},"since":9},"s2"]}' % ts
        )
        self.assertEqual((odd.state, odd.last_session, odd.resumable), ("unknown", None, None))
        self.assertEqual(odd.sessions, (Session("s1", "unknown", ""),))
        self.assertEqual(parse_state_line('{"address":"h/ui",%s,"sessions":{}}' % ts).sessions, ())

    def test_the_streams_no_record_row_is_no_record(self):
        # ctl.mjs stateRow for an account with nothing on the channel: no ts.
        row = ('{"address":"develop-qzapp/ui","role":"rust-ui-dev","state":"unknown",'
               '"sessions":[],"why":"no state record on the channel"}')
        self.assertIsNone(parse_state_line(row))
        self.assertIsNone(parse_state_line('{"address":"develop-qzapp/ui","ts":"yesterday"}'))
        self.assertIsNone(parse_state_line('{"address":"develop-qzapp/ui","ts":"2026-10-08T05:00:00"}'))

    def test_the_stream_filter_keeps_only_this_hosts_records(self):
        here = StateRecord("ui", "idle", (), "2026-10-08T05:00:00Z", host="host-a")
        there = StateRecord("ui", "idle", (), "2026-10-08T05:00:00Z", host="host-b")
        self.assertTrue(for_this_host(here, "host-a"))
        self.assertFalse(for_this_host(there, "host-a"))
        self.assertTrue(for_this_host(there, None), "an unknown host takes every record")
        self.assertFalse(for_this_host(None, "host-a"))


def record(login, *states, age_s=10):
    ts = (NOW - datetime.timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sessions = tuple(Session(f"s{i}", s, ts) for i, s in enumerate(states))
    return StateRecord(login, states[0] if states else "none", sessions, ts, host="h")


BARE = {"shell_pid": 10, "foreground_process_group_id": 10,
        "foreground_processes": [{"pid": 10, "argv": ["bash"]}]}
# The fleet board, as fabric-view's launcher leaves it in its pane.
BOARD = {"shell_pid": 30, "foreground_process_group_id": 30, "foreground_processes": [{"pid": 30, "argv": [
    "/usr/local/bin/fabric-python", "-B", "-c",
    "import sys; sys.path.insert(0, sys.argv[1]); from fabric_view import main; raise SystemExit(main(sys.argv[2:]))",
    "/home/user/projects/herdr/fleet-deck", "board"]}]}
# A shell still running the operator's rc file: neither bare nor moveto.
BUSY = {"shell_pid": 10, "foreground_process_group_id": 11,
        "foreground_processes": [{"pid": 11, "argv": ["direnv", "hook"]}]}


def in_moveto(login, mode="", pid=20):
    argv = ["sudo", "-n", "-u", login, "-H", ENTER, f"/home/{login}/projects", login]
    return {"shell_pid": 10, "foreground_process_group_id": pid,
            "foreground_processes": [{"pid": pid, "argv": argv + ([mode] if mode else [])}]}


def in_ssh(login, word, pid=20):
    """moveto --via ssh: the operator's ssh into the account's forced command."""
    argv = ["ssh", "-t", "-i", "/home/user/.ssh/fabric_deck", f"{login}@127.0.0.1", "--", word]
    return {"shell_pid": 10, "foreground_process_group_id": pid,
            "foreground_processes": [{"pid": pid, "argv": argv}]}


class FakeHerdr(Herdr):
    """herdr's server as the deck's calls see it: workspaces, tabs, labelled
    panes and each pane's process info, every call recorded. The adapter's
    own account_tabs and process_info run on top of it."""

    def __init__(self):
        super().__init__(binary="herdr")
        self.workspace_list = [{"workspace_id": "w1", "label": "New"}]
        self.tabs = {"w1": []}
        self.panes = {}
        self.proc = {}
        self.calls = []
        self.down = False
        self.n = 0
        self.new_pane = BARE
        self.workspace_lists_ok = None  # after this many, `workspace list` fails
        self.fail_once = set()  # (subcommand, pane) pairs whose next call fails
        self.plugin_linked = True

    def _id(self, kind):
        self.n += 1
        return f"{kind}{self.n}"

    def add_tab(self, label, panes, workspace="w1"):
        tab = self._id("t")
        self.tabs[workspace].append({"tab_id": tab, "label": label})
        for pane, pane_label, info in panes:
            self.panes[pane] = {"pane_id": pane, "tab_id": tab, "label": pane_label}
            self.proc[pane] = info
        return tab

    def runs(self, pane=None):
        return [c[3] for c in self.calls if c[:2] == ("pane", "run") and (pane is None or c[2] == pane)]

    def reports(self, pane):
        return [c for c in self.calls if c[1] in ("report-agent", "report-metadata") and c[2] == pane]

    def call(self, *args):
        if self.down:
            raise ConnectionError("herdr's socket refuses")
        self.calls.append(args)
        head = args[:2]
        target = args[3] if head == ("pane", "process-info") else (args[2] if len(args) > 2 else None)
        if (head[1], target) in self.fail_once:
            self.fail_once.discard((head[1], target))
            raise RuntimeError(f"herdr {' '.join(args)}: exit 1: server_unavailable")
        if head == ("workspace", "list"):
            if self.workspace_lists_ok is not None:
                if self.workspace_lists_ok == 0:
                    raise RuntimeError("herdr workspace list: exit 1: not ready")
                self.workspace_lists_ok -= 1
            return {"workspaces": list(self.workspace_list)}
        if head == ("workspace", "create"):
            w = self._id("w")
            self.workspace_list.append({"workspace_id": w, "label": args[3]})
            self.tabs[w] = []
            return {"workspace": {"workspace_id": w}, "tab": {"tab_id": self.add_tab(None, [], w)}}
        if head == ("tab", "list"):
            return {"tabs": list(self.tabs[args[3]])}
        if head == ("tab", "create"):
            pane = self._id("p")
            tab = self.add_tab(args[args.index("--label") + 1], [(pane, None, self.new_pane)], args[3])
            return {"tab": {"tab_id": tab}, "root_pane": {"pane_id": pane}}
        if head == ("tab", "close"):
            for tabs in self.tabs.values():
                tabs[:] = [t for t in tabs if t["tab_id"] != args[2]]
            return {}
        if head == ("pane", "list"):
            return {"panes": list(self.panes.values())}
        if head == ("pane", "split"):
            pane = self._id("p")
            self.panes[pane] = {"pane_id": pane, "tab_id": self.panes[args[2]]["tab_id"], "label": None}
            self.proc[pane] = self.new_pane
            return {"pane": {"pane_id": pane}}
        if head == ("pane", "rename"):
            self.panes[args[2]]["label"] = args[3]
            return {}
        if head == ("pane", "process-info"):
            return {"process_info": self.proc[args[3]]}
        if head[0] == "pane" and head[1] in ("run", "report-agent", "report-metadata", "release-agent"):
            return {}
        if args[:3] == ("plugin", "pane", "open"):
            if not self.plugin_linked:
                raise RuntimeError("herdr plugin pane open: exit 1: plugin_not_found")
            pane = self._id("p")
            tab = self.add_tab(None, [(pane, None, BOARD)], args[args.index("--workspace") + 1])
            return {"plugin_pane": {"plugin_id": args[4], "entrypoint": args[6],
                                    "pane": {"pane_id": pane, "tab_id": tab}}}
        if head == ("tab", "rename"):
            for tabs in self.tabs.values():
                for tab in tabs:
                    if tab["tab_id"] == args[2]:
                        tab["label"] = args[3]
            return {}
        raise AssertionError(f"unexpected herdr call {args}")


ALL_MODES = frozenset({WAIT, WATCH, RESUME})


class Harness:
    """A deck over a FakeHerdr, with the stream's records, a /proc tree and
    the record of what was shown as plain values a test sets."""

    def __init__(self, herdr=None, logins=("ui",), modes=ALL_MODES, befores=None, fleet_tab=False):
        self.herdr = herdr or FakeHerdr()
        self.records = {}
        self.tree, self.argvs = {}, {}
        self.store = None if befores is None else dict(befores)
        self.logs = []
        self.logins = list(logins)
        self.instance = SERVER
        self.wall = NOW

        def save(befores):
            self.store = dict(befores)

        self.deck = Deck(
            herdr=self.herdr,
            accounts=lambda: [Account(login, "rust-ui-dev") for login in self.logins],
            records=self.records,
            modes=modes,
            cwd="/c",
            catalog=None,
            parents=lambda: dict(self.tree),
            argv=lambda pid: self.argvs.get(pid, []),
            load=lambda: None if self.store is None else dict(self.store),
            save=save,
            server=lambda: None if self.herdr.down else self.instance,
            log=self.logs.append,
            utc=lambda: self.wall,
            # The account tabs' tests count herdr calls: the fleet tab is
            # tested on its own (FleetTab).
            fleet_tab=fleet_tab,
        )

    def at(self, deck_s):
        """The deck's clock and the wall clock, moved together."""
        self.wall = NOW + datetime.timedelta(seconds=deck_s)
        return deck_s

    def harness_runs(self, pane, login="ui", mode=WAIT):
        """A person pressed Enter: moveto holds the pane, a harness under it."""
        self.herdr.proc[pane] = in_moveto(login, mode)
        self.tree.update({21: 20, 22: 21})
        self.argvs[22] = ["claude", "--model", "x"]


def harness_of(h, login="ui"):
    return h.deck.harness_pane[login]


class FleetTab(unittest.TestCase):
    def tabs(self, h):
        return [(w, t["label"]) for w, ts in h.herdr.tabs.items() for t in ts]

    def test_a_restore_opens_the_board_as_a_tab_labelled_fleet_once(self):
        h = Harness(fleet_tab=True)
        h.deck.restore(0)
        opened = [c for c in h.herdr.calls if c[:3] == ("plugin", "pane", "open")]
        self.assertEqual(len(opened), 1)
        self.assertIn("--no-focus", opened[0])
        self.assertEqual(opened[0][opened[0].index("--entrypoint") + 1], "board")
        self.assertIn(("w1", "fleet"), self.tabs(h))
        h.deck.restore(10)
        self.assertEqual(len([c for c in h.herdr.calls if c[:3] == ("plugin", "pane", "open")]), 1)

    def test_a_fleet_tab_running_the_board_anywhere_is_kept_and_none_is_added(self):
        h = Harness(fleet_tab=True)
        h.herdr.workspace_list.append({"workspace_id": "w9", "label": "Ops"})
        h.herdr.tabs["w9"] = []
        h.herdr.add_tab("fleet", [("p90", None, BOARD)], "w9")
        h.deck.restore(0)
        self.assertFalse([c for c in h.herdr.calls if c[:3] == ("plugin", "pane", "open")])

    def test_a_fleet_tab_herdr_brought_back_as_a_bare_shell_gets_the_board_again(self):
        # Measured on a real herdr: a session restore keeps the tab's label
        # and gives its pane a shell, not the plugin's program.
        h = Harness(fleet_tab=True)
        old = h.herdr.add_tab("fleet", [("p90", None, BARE)])
        h.herdr.panes["p90"]["cwd"] = BOARD_DIR
        h.deck.restore(0)
        self.assertIn(("tab", "close", old), h.herdr.calls)
        self.assertEqual(len([c for c in h.herdr.calls if c[:3] == ("plugin", "pane", "open")]), 1)
        self.assertEqual([t for _, t in self.tabs(h)].count("fleet"), 1)

    def test_a_persons_shell_tab_named_fleet_elsewhere_is_never_closed(self):
        h = Harness(fleet_tab=True)
        tab = h.herdr.add_tab("fleet", [("p90", None, BARE)])
        h.herdr.panes["p90"]["cwd"] = "/home/user/projects"
        h.deck.restore(0)
        self.assertNotIn(("tab", "close", tab), h.herdr.calls)
        self.assertFalse([c for c in h.herdr.calls if c[0] == "plugin"])

    def test_a_fleet_tab_whose_shell_has_not_started_is_looked_at_again_at_the_next_map_read(self):
        h = Harness(fleet_tab=True)
        tab = h.herdr.add_tab("fleet", [("p90", None, {"shell_pid": None, "foreground_processes": []})])
        h.herdr.panes["p90"]["cwd"] = BOARD_DIR
        h.deck.restore(0)
        self.assertNotIn(("tab", "close", tab), h.herdr.calls)
        self.assertFalse([l for l in h.logs if "something else" in l])
        h.herdr.proc["p90"] = BARE
        h.deck.follow(PANE_MAP_REFRESH_S + 1)
        self.assertIn(("tab", "close", tab), h.herdr.calls)
        self.assertEqual(len([c for c in h.herdr.calls if c[:3] == ("plugin", "pane", "open")]), 1)

    def test_an_unsettled_fleet_tab_a_person_closes_is_not_reopened_before_the_next_restore(self):
        h = Harness(fleet_tab=True)
        tab = h.herdr.add_tab("fleet", [("p90", None, {"shell_pid": None, "foreground_processes": []})])
        h.herdr.panes["p90"]["cwd"] = BOARD_DIR
        h.deck.restore(0)
        h.herdr.call("tab", "close", tab)
        h.deck.follow(PANE_MAP_REFRESH_S + 1)
        h.deck.follow(2 * PANE_MAP_REFRESH_S + 2)
        self.assertFalse([c for c in h.herdr.calls if c[:3] == ("plugin", "pane", "open")])

    def test_herdr_failing_while_an_unsettled_fleet_tab_is_looked_at_again_leaves_the_deck_running(self):
        h = Harness(fleet_tab=True)
        h.herdr.add_tab("fleet", [("p90", None, {"shell_pid": None, "foreground_processes": []})])
        h.herdr.panes["p90"]["cwd"] = BOARD_DIR
        h.deck.restore(0)
        tabs = h.herdr.account_tabs

        def then_down(logins):
            got = tabs(logins)
            h.herdr.down = True        # the server goes between the map read and the look
            return got
        h.herdr.account_tabs = then_down
        h.deck.follow(PANE_MAP_REFRESH_S + 1)   # must not raise
        self.assertIsNotNone(h.deck.fleet_unsettled)   # looked at again once herdr answers
        h.herdr.down = False
        h.herdr.account_tabs = tabs
        h.herdr.proc["p90"] = BARE
        h.deck.follow(2 * PANE_MAP_REFRESH_S + 2)
        self.assertEqual(len([c for c in h.herdr.calls if c[:3] == ("plugin", "pane", "open")]), 1)

    def test_a_fleet_tab_running_something_else_is_left_alone_and_said_once(self):
        h = Harness(fleet_tab=True)
        tab = h.herdr.add_tab("fleet", [("p90", None, in_moveto("ui"))])
        h.deck.restore(0)
        h.deck.restore(10)
        self.assertNotIn(("tab", "close", tab), h.herdr.calls)
        self.assertFalse([c for c in h.herdr.calls if c[0] == "plugin"])
        self.assertEqual(sum("runs something else" in l for l in h.logs), 1)

    def test_an_account_only_restore_leaves_the_fleet_tab_alone(self):
        h = Harness(fleet_tab=True)
        h.deck.restore(0, only={"ui"})
        self.assertFalse([c for c in h.herdr.calls if c[0] == "plugin"])

    def test_a_plugin_not_linked_is_said_once_and_the_accounts_are_restored(self):
        h = Harness(fleet_tab=True)
        h.herdr.plugin_linked = False
        h.deck.restore(0)
        h.deck.restore(10)
        said = [l for l in h.logs if "no fleet tab" in l]
        self.assertEqual(len(said), 1)
        self.assertIn("herdr plugin link", said[0])
        self.assertIn(("w1", "ui"), self.tabs(h))
        self.assertNotIn(("w1", "fleet"), self.tabs(h))


class Restore(unittest.TestCase):
    def test_a_new_account_gets_a_three_pane_tab_its_harness_waiting_for_the_decision(self):
        h = Harness()
        h.deck.restore(0)
        labels = sorted(p["label"] for p in h.herdr.panes.values())
        self.assertEqual(labels, ["harness", "shell", "status"])
        harness = harness_of(h)
        self.assertEqual(h.herdr.panes[harness]["label"], "harness")
        split = next(c for c in h.herdr.calls if c[:2] == ("pane", "split"))
        self.assertEqual((split[2], split[4], split[6]), (harness, "right", "0.65"))
        h.deck.follow(1)
        self.assertEqual(sorted(h.herdr.runs()), ["moveto ui", "moveto ui --watch"])
        self.assertEqual(h.herdr.runs(harness), [], "the harness waits for the stream")

    def test_panes_still_busy_when_created_wait_for_their_prompt_and_the_decision(self):
        h = Harness(befores={"ui": Before(running=True)})
        h.herdr.new_pane = BUSY
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        h.deck.follow(h.at(RESTORE_SETTLE_S))
        self.assertEqual(h.herdr.runs(), [], "nothing is typed into a busy pane")
        self.assertEqual(h.deck.pending.keys(), {"ui"})
        for pane in h.herdr.panes:
            h.herdr.proc[pane] = BARE
        h.deck.follow(h.at(RESTORE_SETTLE_S + 2))
        self.assertEqual(sorted(h.herdr.runs()), ["moveto ui", "moveto ui --resume", "moveto ui --watch"])
        h.deck.follow(h.at(60))
        self.assertEqual(h.store, {"ui": Before(running=True)}, "the fall from before the decision is never noted")

    def test_a_harness_busy_past_the_wait_still_gets_the_restore_decision(self):
        h = Harness(befores={"ui": Before(running=True)})
        h.herdr.new_pane = BUSY
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        for at in range(1, RESTORE_WAIT_S + 10, 2):
            h.deck.follow(h.at(at))
        self.assertEqual(h.herdr.runs(), [])
        self.assertEqual(sum("still busy" in line for line in h.logs), 3, "said once per pane")
        for pane in h.herdr.panes:
            h.herdr.proc[pane] = BARE
        h.deck.follow(h.at(RESTORE_WAIT_S + 12))
        self.assertIn("moveto ui --resume", h.herdr.runs(harness_of(h)))

    def test_shell_and_status_panes_already_running_moveto_are_not_waited_for(self):
        herdr = FakeHerdr()
        herdr.add_tab("ui", [("p0", "harness", in_moveto("ui", WAIT)), ("p1", "shell", in_moveto("ui")),
                             ("p2", "status", in_moveto("ui", WATCH))])
        h = Harness(herdr)
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        h.deck.follow(1)
        self.assertEqual(h.deck.waiting, {}, "dropped at the first look")
        for at in range(1, RESTORE_WAIT_S + 10, 2):
            h.deck.follow(at)
        self.assertFalse(any("busy" in line for line in h.logs))

    def test_a_restore_that_keeps_failing_is_said_once(self):
        h = Harness()
        h.deck.restore(0)
        h.herdr.down = True
        h.deck.follow(10)
        h.herdr.down = False
        # The map reads and the restore's own tab read pass; its listing fails.
        h.herdr.workspace_lists_ok = 2
        for at in range(20, 40, 2):
            h.deck.follow(at)
            h.herdr.workspace_lists_ok = 2
        self.assertEqual(sum(line.startswith("cannot restore yet") for line in h.logs), 1)
        self.assertEqual(h.logs.count("herdr's server answers: restoring"), 1)
        h.herdr.workspace_lists_ok = None
        h.deck.follow(50)
        self.assertFalse(h.deck.lost)
        h.instance = OTHER  # a later restart is said again
        h.deck.follow(52)
        self.assertEqual(h.logs.count("herdr's server answers: restoring"), 2)

    def test_a_failed_look_at_a_pane_during_restore_still_leaves_the_decision_to_come(self):
        for failing in ("p1", "p3", "p4"):  # the harness, the shell, the status pane
            h = Harness(befores={"ui": Before(running=True)})
            h.records["ui"] = record("ui")
            h.herdr.fail_once = {("process-info", failing)}
            h.deck.restore(0)
            for at in range(1, PANE_MAP_REFRESH_S + RESTORE_SETTLE_S + 4, 2):
                h.deck.follow(h.at(at))
            self.assertFalse(h.herdr.fail_once, f"{failing}'s look failed once")
            harness = harness_of(h)
            self.assertEqual(h.herdr.runs(harness)[:1], ["moveto ui --resume"], f"{failing}: the decision")
            others = sorted(r for c, r in ((c, c[3]) for c in h.herdr.calls if c[:2] == ("pane", "run"))
                            if c[2] != harness)
            self.assertEqual(others, ["moveto ui", "moveto ui --watch"], failing)

    def test_an_account_whose_restore_failed_part_way_is_restored_again(self):
        h = Harness(befores={"ui": Before(running=True)})
        h.records["ui"] = record("ui")
        h.herdr.fail_once = {("split", "p1")}
        h.deck.restore(0)
        self.assertEqual(h.deck.retry, {"ui"})
        self.assertEqual((h.deck.harness_pane, h.deck.pending, h.deck.waiting), ({}, {}, {}),
                         "nothing of the half-done restore is followed")
        h.deck.follow(h.at(RESTORE_SETTLE_S))
        self.assertEqual(h.herdr.runs(), [], "no decision before the retry")
        h.deck.follow(h.at(PANE_MAP_REFRESH_S))
        h.deck.follow(h.at(PANE_MAP_REFRESH_S + RESTORE_SETTLE_S))
        h.deck.follow(h.at(PANE_MAP_REFRESH_S + RESTORE_SETTLE_S + 2))
        self.assertEqual(h.deck.retry, set())
        self.assertEqual(sorted(p["label"] for p in h.herdr.panes.values()), ["harness", "shell", "status"])
        self.assertIn("moveto ui --resume", h.herdr.runs(harness_of(h)))

    def test_an_account_that_leaves_the_list_is_not_retried(self):
        h = Harness()
        h.herdr.fail_once = {("split", "p1")}
        h.deck.restore(0)
        h.logins = []
        h.deck.follow(PANE_MAP_REFRESH_S)
        self.assertEqual(h.deck.retry, set())

    def test_an_account_placed_while_the_deck_runs_gets_its_tab(self):
        h = Harness(logins=("ui",))
        h.records.update(ui=record("ui"), vo=record("vo"))
        h.deck.restore(0)
        h.logins.append("vo")
        h.deck.follow(h.at(PANE_MAP_REFRESH_S))
        h.deck.follow(h.at(PANE_MAP_REFRESH_S + RESTORE_SETTLE_S))
        self.assertIn("vo", h.deck.harness_pane)
        self.assertIn("moveto vo --wait", h.herdr.runs(harness_of(h, "vo")))

    def test_a_harness_closed_by_hand_is_not_recreated_before_a_restore(self):
        h = Harness()
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        del h.herdr.panes[harness_of(h)]
        for at in range(PANE_MAP_REFRESH_S, 4 * PANE_MAP_REFRESH_S, 2):
            h.deck.follow(at)
        self.assertEqual(sum(c[:2] == ("pane", "split") for c in h.herdr.calls), 2, "only the first restore's")
        self.assertNotIn("ui", h.deck.harness_pane)

    def test_a_herdr_failure_while_restoring_leaves_the_deck_waiting_not_dead(self):
        h = Harness()
        h.herdr.workspace_lists_ok = 1  # the tab map reads, the restore's own listing fails
        h.deck.restore(0)
        self.assertTrue(h.deck.lost)
        self.assertTrue(any(line.startswith("cannot restore yet") for line in h.logs))
        h.herdr.workspace_lists_ok = None
        h.deck.follow(1)
        self.assertEqual(h.deck.harness_pane.keys(), {"ui"})

    def test_nothing_running_before_or_now_arms_wait_after_the_settle(self):
        h = Harness()
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        harness = harness_of(h)
        h.deck.follow(RESTORE_SETTLE_S - 1)
        self.assertEqual(h.herdr.runs(harness), [])
        h.deck.follow(RESTORE_SETTLE_S)
        self.assertEqual(h.herdr.runs(harness), ["moveto ui --wait"])

    def test_what_ran_before_and_died_is_resumed_and_what_still_runs_gets_a_plain_shell(self):
        for live, expected in (((), "moveto ui --resume"), (("working",), "moveto ui")):
            h = Harness(befores={"ui": Before(running=True)})
            h.records["ui"] = record("ui", *live)
            h.deck.restore(0)
            h.deck.follow(RESTORE_SETTLE_S)
            self.assertEqual(h.herdr.runs(harness_of(h)), [expected], live)

    def test_with_no_fresh_record_it_waits_then_arms_wait_never_resume(self):
        h = Harness(befores={"ui": Before(running=True)})
        h.records["ui"] = record("ui", age_s=3600)
        h.deck.restore(0)
        harness = harness_of(h)
        h.deck.follow(RESTORE_WAIT_S - 1)
        self.assertEqual(h.herdr.runs(harness), [])
        h.deck.follow(RESTORE_WAIT_S)
        self.assertEqual(h.herdr.runs(harness), ["moveto ui --wait"])

    def test_a_pane_a_person_started_something_in_during_the_wait_is_not_armed(self):
        h = Harness()
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        harness = harness_of(h)
        h.herdr.proc[harness] = in_moveto("ui")
        h.deck.follow(RESTORE_SETTLE_S)
        self.assertEqual(h.herdr.runs(harness), [])

    def test_a_milestone_one_tab_is_adopted_as_the_harness(self):
        herdr = FakeHerdr()
        herdr.add_tab("ui", [("p0", None, BARE)])
        h = Harness(herdr)
        h.deck.restore(0)
        self.assertEqual(herdr.panes["p0"]["label"], "harness")
        self.assertEqual(sorted(p["label"] for p in herdr.panes.values()), ["harness", "shell", "status"])
        self.assertFalse(any(c[:2] == ("tab", "create") for c in herdr.calls))

    def test_a_tab_split_by_hand_is_left_alone_and_said_once(self):
        herdr = FakeHerdr()
        herdr.add_tab("ui", [("p0", None, BARE), ("p1", None, BARE)])
        h = Harness(herdr)
        h.deck.restore(0)
        h.deck.restore(1)
        self.assertEqual(h.herdr.runs(), [])
        self.assertFalse(any(c[1] in ("split", "rename") for c in herdr.calls))
        self.assertEqual(sum("split by hand" in line for line in h.logs), 1)

    def test_a_surviving_harness_is_classified_and_never_rearmed(self):
        herdr = FakeHerdr()
        herdr.add_tab("ui", [("p0", "harness", BARE), ("p1", "shell", in_moveto("ui")),
                             ("p2", "status", in_moveto("ui", WATCH))])
        h = Harness(herdr)
        h.harness_runs("p0")
        h.records["ui"] = record("ui", "working")
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        self.assertEqual(h.herdr.runs(), [], "nothing is typed while moveto runs")
        self.assertEqual(h.herdr.reports("p0")[1][-1], "working", "after the account")

    def test_a_tab_entered_over_ssh_is_classified_like_a_sudo_one_from_the_stream(self):
        herdr = FakeHerdr()
        herdr.add_tab("ui", [("p0", "harness", in_ssh("ui", WAIT)), ("p1", "shell", in_ssh("ui", "shell")),
                             ("p2", "status", in_ssh("ui", WATCH))])
        h = Harness(herdr)
        h.records["ui"] = record("ui", "working")
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        self.assertEqual(h.herdr.runs(), [], "nothing is typed while ssh runs the account")
        self.assertEqual(h.herdr.reports("p0")[1][-1], "working", "after the account")
        h.records["ui"] = record("ui", "none")
        h.deck.follow(RESTORE_SETTLE_S + 30)
        self.assertEqual(h.herdr.runs(), [])
        self.assertIn("idle=shell", h.herdr.reports("p0")[-1])

    def test_an_ssh_pane_with_a_stale_record_is_left_as_it_was(self):
        herdr = FakeHerdr()
        herdr.add_tab("ui", [("p0", "harness", in_ssh("ui", WAIT)), ("p1", "shell", in_ssh("ui", "shell")),
                             ("p2", "status", in_ssh("ui", WATCH))])
        h = Harness(herdr)
        h.records["ui"] = record("ui", "working", age_s=10_000)
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        self.assertEqual(h.herdr.runs(), [])
        self.assertNotIn("working", [r[-1] for r in h.herdr.reports("p0")])

    def test_an_account_with_an_unsafe_login_is_never_typed(self):
        h = Harness(logins=("ui; rm -rf ~",))
        h.deck.restore(0)
        h.deck.follow(RESTORE_WAIT_S)
        self.assertEqual(h.herdr.runs(), [])
        self.assertFalse(any(c[:2] == ("tab", "create") for c in h.herdr.calls))


class Follow(unittest.TestCase):
    def ready(self, befores=None):
        """A restored account whose harness pane runs `moveto ui --wait`."""
        h = Harness(befores=befores)
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        harness = harness_of(h)
        h.herdr.proc[harness] = in_moveto("ui", WAIT)
        h.deck.follow(RESTORE_SETTLE_S + 1)
        return h, harness

    def test_dormant_then_enter_shows_the_session_and_records_running(self):
        h, harness = self.ready()
        self.assertEqual(h.herdr.reports(harness)[-1][-3:], ("idle=dormant", "--state-label", "done=dormant"))
        self.assertIn(("--display-agent", "dormant"), list(zip(h.herdr.reports(harness)[-1], h.herdr.reports(harness)[-1][1:])))
        h.harness_runs(harness)
        h.records["ui"] = record("ui", "working")
        h.deck.follow(20)
        self.assertEqual(h.herdr.reports(harness)[-3][-1], "working")
        self.assertEqual(h.store, {"ui": Before(running=True)})

    def test_a_session_elsewhere_reads_running_elsewhere_and_touches_nothing(self):
        h, harness = self.ready()
        h.records["ui"] = record("ui", "blocked")
        h.deck.follow(20)
        self.assertEqual(h.herdr.reports(harness)[-1][-1], "done=dormant", "not within the same-session window")
        h.deck.follow(20 + SAME_SESSION_S)
        self.assertEqual(h.herdr.reports(harness)[-1][-1], "unknown=running elsewhere")
        self.assertEqual(h.herdr.runs(harness), ["moveto ui --wait"])

    def test_a_wait_that_ends_is_rearmed_wait_and_reads_dormant_not_failed(self):
        h, harness = self.ready()
        h.herdr.proc[harness] = BARE
        h.deck.follow(30)
        self.assertEqual(h.herdr.reports(harness)[-1][-3:], ("idle=dormant", "--state-label", "done=dormant"))
        self.assertEqual(h.herdr.runs(harness), ["moveto ui --wait"] * 2)

    def test_a_resume_that_ends_before_any_harness_is_failed_and_rearmed_wait(self):
        h = Harness(befores={"ui": Before(running=True)})
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        harness = harness_of(h)
        self.assertEqual(h.herdr.runs(harness), ["moveto ui --resume"])
        h.deck.follow(RESTORE_SETTLE_S * 3 + 20)
        self.assertEqual(h.herdr.reports(harness)[-1][-1], "blocked=failed")
        self.assertEqual(h.herdr.runs(harness), ["moveto ui --resume", "moveto ui --wait"])

    def test_a_moveto_that_keeps_ending_at_once_is_not_typed_again_and_reads_failed(self):
        h, harness = self.ready()
        h.herdr.proc[harness] = BARE
        for at in range(10, 60, 2):
            h.deck.follow(at)
        self.assertEqual(h.herdr.runs(harness), ["moveto ui --wait"] * 2)
        self.assertEqual(h.herdr.reports(harness)[-1][-1], "blocked=failed")
        self.assertEqual(sum("ended at once twice" in line for line in h.logs), 1)
        # A person starts it again, and it fails again: said again.
        h.herdr.proc[harness] = in_moveto("ui", WAIT)
        h.deck.follow(70)
        h.herdr.proc[harness] = BARE
        for at in range(72, 120, 2):
            h.deck.follow(at)
        self.assertEqual(sum("ended at once twice" in line for line in h.logs), 2)

    def test_a_moveto_list_failure_is_not_a_loss_of_herdr(self):
        h = Harness(befores={"ui": Before(running=True)})
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        h.herdr.proc[harness_of(h)] = in_moveto("ui", RESUME)  # the --resume is starting
        h.logins = None  # moveto --list fails from here on
        h.deck.accounts = lambda: (_ for _ in ()).throw(OSError("moveto --list: timed out"))
        h.deck.follow(RESTORE_SETTLE_S + PANE_MAP_REFRESH_S)
        self.assertFalse(h.deck.lost)
        self.assertNotIn("herdr's server answers: restoring", h.logs)
        self.assertEqual(h.herdr.reports(harness_of(h))[-1][-1], "working=restoring")

    def test_only_a_change_is_reported_until_the_resend_interval(self):
        h, harness = self.ready()
        sent = len(h.herdr.reports(harness))
        h.deck.follow(20)
        self.assertEqual(len(h.herdr.reports(harness)), sent)
        h.deck.follow(RESTORE_SETTLE_S + 1 + RESEND_AFTER_S)
        self.assertEqual(len(h.herdr.reports(harness)), sent + 4,
                         "the account, the state, the clear and the label")

    def test_a_pane_left_by_its_account_gives_back_what_the_deck_set(self):
        h, harness = self.ready()
        h.logins = []
        h.deck.follow(RESTORE_SETTLE_S + 1 + PANE_MAP_REFRESH_S)
        given = [c for c in h.herdr.calls if harness in c][-4:]
        self.assertEqual([c[1] for c in given],
                         ["release-agent", "report-metadata", "report-metadata", "report-metadata"])
        self.assertEqual([c[-1] for c in given[1:]],
                         ["--clear-state-labels", "--clear-display-agent", "account"])

    def test_a_harness_held_by_another_command_reads_unknown_not_dormant(self):
        h, harness = self.ready()
        h.herdr.proc[harness] = BUSY
        h.deck.follow(20)
        self.assertEqual(h.herdr.reports(harness)[-1][-2:], ("--display-agent", "unknown"))

    def test_a_harness_pane_closed_by_hand_is_followed_no_more(self):
        h, harness = self.ready()
        del h.herdr.panes[harness]
        h.deck.follow(RESTORE_SETTLE_S + 1 + PANE_MAP_REFRESH_S)
        calls = len(h.herdr.calls)
        h.deck.follow(RESTORE_SETTLE_S + 3 + PANE_MAP_REFRESH_S)
        self.assertFalse(any(harness in c for c in h.herdr.calls[calls:]))

    def test_a_herdr_server_that_comes_back_is_a_restore(self):
        h, harness = self.ready()
        h.herdr.down = True
        h.deck.follow(100)
        self.assertTrue(h.deck.lost)
        logged = len(h.logs)
        h.deck.follow(102)
        self.assertEqual(len(h.logs), logged, "lost, the deck only tries the map again, quietly")
        h.herdr.down = False
        h.herdr.proc[harness] = BARE
        h.deck.follow(100 + PANE_MAP_REFRESH_S)
        self.assertIn("herdr's server answers: restoring", h.logs)
        self.assertEqual(h.deck.pending.keys(), {"ui"})

    def test_one_accounts_herdr_failure_does_not_stop_the_others(self):
        h = Harness(logins=("ui", "vo"))
        h.records.update(ui=record("ui"), vo=record("vo"))
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        del h.herdr.proc[harness_of(h, "ui")]
        vo = harness_of(h, "vo")
        h.herdr.proc[vo] = in_moveto("vo", WAIT)
        h.records["vo"] = record("vo", "idle")
        h.deck.follow(RESTORE_SETTLE_S + 1)
        h.deck.follow(RESTORE_SETTLE_S + 1 + SAME_SESSION_S)
        self.assertEqual(h.herdr.reports(vo)[-1][-1], "unknown=running elsewhere")
        self.assertTrue(any(line.startswith("ui:") for line in h.logs))

    def test_the_record_keeps_only_placed_accounts(self):
        h, harness = self.ready(befores={"gone": Before(running=True)})
        h.harness_runs(harness)
        h.records["ui"] = record("ui", "working")
        h.deck.follow(20)
        self.assertEqual(h.store, {"ui": Before(running=True)})

    def test_another_server_answering_is_herdr_lost_and_a_restore(self):
        h, harness = self.ready()
        h.instance = OTHER
        h.herdr.proc[harness] = BARE
        h.deck.follow(20)
        self.assertIn("herdr's server was restarted", h.logs)
        self.assertIn("herdr's server answers: restoring", h.logs)
        self.assertEqual(h.deck.pending.keys(), {"ui"})


class BeforeFollow(unittest.TestCase):
    def running(self):
        h = Harness()
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        h.deck.follow(h.at(RESTORE_SETTLE_S))
        h.records["ui"] = record("ui", "working")
        h.deck.follow(h.at(10))
        self.assertEqual(h.store, {"ui": Before(running=True)})
        return h

    def test_a_session_that_ends_while_herdr_stays_up_settles_as_ended(self):
        h = self.running()
        h.records["ui"] = record("ui")
        h.deck.follow(h.at(20))
        self.assertEqual(h.store["ui"].fall_at, h.wall.timestamp())
        h.deck.follow(h.at(20 + SETTLE_S))
        self.assertEqual(h.store, {"ui": Before(running=False)})

    def test_a_session_that_ends_with_herdr_stays_recorded_as_running(self):
        h = self.running()
        h.records["ui"] = record("ui")
        h.deck.follow(h.at(20))
        h.herdr.down = True
        h.deck.follow(h.at(22))
        h.deck.follow(h.at(40))
        self.assertEqual(h.store, {"ui": Before(running=True)})

    def test_a_fall_left_pending_settles_at_start_only_on_the_same_server(self):
        fall = Before(running=True, fall_at=NOW.timestamp() - 30, fall_server=SERVER)
        for instance, running in ((SERVER, False), (OTHER, True)):
            h = Harness(befores={"ui": fall})
            h.instance = instance
            h.deck.restore(0)
            self.assertEqual(h.deck.befores["ui"].running, running, instance)
            self.assertEqual(h.store["ui"].fall_at, None, "settled or dropped, and written")


class NotYet(unittest.TestCase):
    def test_modes_moveto_lacks_are_never_armed_and_the_pane_reads_unknown(self):
        h = Harness(modes=frozenset())
        h.records["ui"] = record("ui")
        h.deck.restore(0)
        h.deck.follow(RESTORE_SETTLE_S)
        h.deck.follow(RESTORE_SETTLE_S + 20)
        self.assertEqual(h.herdr.runs(), ["moveto ui"], "only the plain shell pane")
        last = h.herdr.reports(harness_of(h))[-1]
        self.assertEqual(last[-2:], ("--display-agent", "unknown"), "the row does not read claude")
        self.assertEqual(sum("no --wait" in line for line in h.logs), 1)


class Panel(unittest.TestCase):
    def test_the_pane_id_comes_first_and_no_seq_is_sent(self):
        report = report_command("p1", Shown("idle", "dormant"))
        self.assertEqual(report[:3], ("pane", "report-agent", "p1"))
        self.assertNotIn("--seq", report)
        clear, label = label_commands("p1", Shown("idle", "dormant"))
        self.assertEqual(clear[-1], "--clear-state-labels")
        self.assertNotIn("--clear-state-labels", label, "herdr refuses a clear and a set in one call")
        self.assertEqual(label[-6:], ("--display-agent", "dormant", "--state-label", "idle=dormant",
                                      "--state-label", "done=dormant"))
        self.assertEqual(label_commands("p1", Shown("blocked", "failed"))[1][-4:],
                         ("--display-agent", "failed", "--state-label", "blocked=failed"))

    def test_the_account_token_is_set_with_the_state_and_given_back_on_release(self):
        self.assertEqual(account_command("p1", "rust-ui-dev-01")[:3], ("pane", "report-metadata", "p1"))
        self.assertEqual(account_command("p1", "rust-ui-dev-01")[-2:], ("--token", "account=rust-ui-dev-01"))
        self.assertIn(("--clear-token", "account"), [c[-2:] for c in release_commands("p1")])

    def test_a_running_harness_reads_as_herdrs_own_agent_name_and_state(self):
        clear, name = label_commands("p1", Shown("working", ""))
        self.assertEqual((clear[-1], name[-1]), ("--clear-state-labels", "--clear-display-agent"))

    def test_moveto_modes_are_whole_option_tokens_and_resume_needs_wait(self):
        self.assertEqual(modes_in("usage: moveto <account> [--wait|--resume|--watch]"),
                         frozenset({WAIT, RESUME, WATCH}))
        self.assertEqual(modes_in("usage: moveto <account> [--resume] [--waitfor N]"), frozenset())
        self.assertEqual(modes_in("--watch-later --wait-x"), frozenset())


class SocketPath(unittest.TestCase):
    def test_the_session_herdr_reaches_is_the_one_probed(self):
        import subprocess
        from unittest import mock

        listing = ('{"sessions": [{"name": "default", "default": true, "socket_path": "/d.sock"},'
                   ' {"name": "work", "default": false, "socket_path": "/w.sock"}]}')
        ran = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout=listing))
        cases = (({"HERDR_SESSION": "work", "HERDR_SOCKET_PATH": "/x.sock"}, "/x.sock"),
                 ({"HERDR_SESSION": "work"}, "/w.sock"), ({"HERDR_SESSION": "default"}, "/d.sock"),
                 ({}, "/d.sock"))
        for env, expected in cases:
            with mock.patch.dict(os.environ, env, clear=False), mock.patch("subprocess.run", ran):
                for name in {"HERDR_SESSION", "HERDR_SOCKET_PATH"} - env.keys():
                    os.environ.pop(name, None)
                self.assertEqual(herdr_socket_path(), expected, env)


class Proc(unittest.TestCase):
    def fake_proc(self, processes):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root)
        for pid, (ppid, comm, argv) in processes.items():
            os.makedirs(os.path.join(root, str(pid)))
            with open(os.path.join(root, str(pid), "stat"), "w") as f:
                f.write(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1\n")
            with open(os.path.join(root, str(pid), "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        os.makedirs(os.path.join(root, "self"))
        return root

    def test_a_harness_is_found_under_the_panes_sudo_through_its_own_pty(self):
        root = self.fake_proc({
            20: (10, "sudo", ["sudo", "-u", "ui"]),
            21: (20, "sudo", ["sudo", "-u", "ui"]),
            22: (21, "bash", ["bash", "-i"]),
            23: (22, "fabric-python", ["/usr/local/bin/fabric-python", "-I", "/a/tools/fabric/launch.py"]),
            30: (1, "claude ) (x", ["claude"]),
        })
        parents = proc_parents(root)
        self.assertEqual(parents[30], 1, "a comm with parentheses is read past")
        argv = lambda pid: proc_argv(pid, root)
        self.assertTrue(harness_under(20, parents, argv))
        self.assertFalse(harness_under(21, {21: 20, 22: 21}, lambda pid: ["bash"]))
        self.assertFalse(harness_under(99, parents, argv), "a pid with no children")

    def test_a_cycle_in_a_racing_tree_does_not_hang(self):
        self.assertFalse(harness_under(1, {2: 3, 3: 2, 4: 1}, lambda pid: []))


class BeforeFile(unittest.TestCase):
    def test_the_record_is_written_owner_only_and_read_back_whole(self):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root)
        path = os.path.join(root, "fabric-deck", "before.json")
        self.assertIsNone(load_befores(path), "no file is a first run")
        befores = {"ui": Before(running=True, fall_at=12.5, fall_server=SERVER), "vo": Before()}
        save_befores(path, befores)
        self.assertEqual(load_befores(path), befores)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_an_entry_of_the_wrong_shape_is_read_as_absent(self):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root)
        path = os.path.join(root, "before.json")
        with open(path, "w") as f:
            f.write('{"accounts": {"ui": {"running": "yes"}, "vo": {"running": true, "fall_at": 3, '
                    '"fall_server": ["x", 1]}, "wo": 5}}')
        self.assertEqual(load_befores(path), {"vo": Before(running=True)})
        with open(path, "w") as f:
            f.write("[")
        self.assertEqual(load_befores(path), {})


class ServerInstance(unittest.TestCase):
    def test_the_peer_pid_and_its_start_time_name_the_instance(self):
        import socket

        root = tempfile.mkdtemp(dir=f"/run/user/{os.getuid()}" if os.path.isdir(f"/run/user/{os.getuid()}") else None)
        self.addCleanup(__import__("shutil").rmtree, root)
        path = os.path.join(root, "s.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        self.assertEqual(server_instance(path), (os.getpid(), start_ticks(os.getpid())))
        self.assertIsNone(server_instance(os.path.join(root, "none.sock")))
        self.assertIsNone(server_instance(None))


class Backoff(unittest.TestCase):
    def test_waits_grow_and_reset_after_a_healthy_stream(self):
        failures, waits = 0, []
        for _ in range(8):
            failures, wait = next_backoff(failures, 0.5)
            waits.append(wait)
        self.assertEqual(waits, [1, 2, 5, 10, 30, 60, 60, 60])
        self.assertEqual(next_backoff(failures, 61), (1, 1))


class StreamLines(unittest.TestCase):
    def test_a_line_that_fails_to_apply_does_not_end_the_stream(self):
        import sys

        seen, logs = [], []
        stream = None

        def on_line(raw):
            seen.append(raw.strip())
            if raw.strip() == "a":
                raise ValueError("bad")
            stream.closing = True

        stream = Stream(on_line=on_line, log=logs.append,
                        command=(sys.executable, "-c", "print('a'); print('b')"))
        stream.run()
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(logs, ["a state record was skipped: bad"])


class StopChild(unittest.TestCase):
    def stubborn_child(self):
        import subprocess
        import sys

        child = subprocess.Popen(
            [sys.executable, "-c",
             "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "print(flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, text=True,
        )
        child.stdout.readline()  # its SIGTERM handler is in place
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    def stop(self, child, grace=0.5, signal_after=None):
        import signal
        import threading
        import fabric_deck

        saved = fabric_deck.STOP_CHILD_GRACE_S
        fabric_deck.STOP_CHILD_GRACE_S = grace
        timer = None
        if signal_after is not None:
            timer = threading.Timer(signal_after, os.kill, (os.getpid(), signal.SIGINT))
            timer.start()
        try:
            stop_child(child)
        finally:
            fabric_deck.STOP_CHILD_GRACE_S = saved
            if timer is not None:
                timer.join()

    def test_a_child_that_ignores_terminate_is_killed_and_reaped(self):
        import signal

        child = self.stubborn_child()
        before = signal.getsignal(signal.SIGINT)
        self.stop(child, grace=0.2)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertIs(signal.getsignal(signal.SIGINT), before, "the handlers are given back")

    def test_a_stop_during_the_grace_is_held_until_the_child_is_reaped(self):
        import signal

        child = self.stubborn_child()
        with self.assertRaises(KeyboardInterrupt):
            self.stop(child, grace=0.5, signal_after=0.1)
        self.assertEqual(child.returncode, -signal.SIGKILL, "reaped before the stop is acted on")


class UnknownStateTest(unittest.TestCase):
    def test_a_record_whose_state_is_unknown_is_no_record(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.isoformat()
        unreadable = parse_state_line(json.dumps({
            "address": "h/a", "ts": ts, "state": "unknown", "sessions": [],
            "why": "the account cannot read its session state"}))
        idle = parse_state_line(json.dumps({"address": "h/a", "ts": ts, "state": "idle", "sessions": []}))
        self.assertIsNone(live_of(unreadable, now), "an unreadable account says nothing")
        self.assertEqual(live_of(idle, now).count, 0, "a readable one with no session says 0")


if __name__ == "__main__":
    unittest.main()

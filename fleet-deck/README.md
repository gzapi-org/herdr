# Fleet Deck

The operator's console over the agent fleet, built on this herdr fork.
Each agent account gets one tab, labelled with its login, holding three
panes:

| pane | runs | for |
|---|---|---|
| **harness** (left, 65 %, focused) | `moveto <account> --wait` | one line, `<account> - Enter to activate`; Enter starts or resumes the account's session |
| **shell** (top right) | `moveto <account>` | a shell as the account |
| **status** (bottom right) | `moveto <account> --watch` | the account's status, read-only |

This directory exists only in gzapi-org's fork; upstream merges never
touch it. The contract is agent-fabric's `docs/fleet-deck/tab-states.md`
(agent-fabric#115). Its state machine is implemented in `deck_tabs.py`,
and the deck around it in `fabric_deck.py`.

## Start and stop

One command brings the whole deck back, from a fresh terminal on the
operator's human login:

```sh
fleet-deck
```

It starts herdr's server if none answers, starts the deck controller if
none runs, and attaches this terminal to herdr. Run it again, or from a
second terminal, and it only attaches: the server answers its socket, and
the controller holds a lock (`$XDG_STATE_HOME/fabric-deck/deck.lock`) that a
second one cannot take. Detach with **ctrl+6 q**; the deck goes on.

```sh
fleet-deck status    # the server, the controller, the log, the prefix; exit 0 when both run, 3 otherwise
fleet-deck stop      # stops the controller (SIGTERM; its state stream goes with it)
fleet-deck restart   # stops the controller and starts a new one
fleet-deck start     # starts what is missing, without attaching
```

- **The controller has no terminal.** It runs in a session of its own, its
  input is `/dev/null`, and it writes to `$XDG_STATE_HOME/fabric-deck/deck.log`
  (one line per change, timestamped). Each time a controller starts, a log
  past 1 MiB is moved to `deck.log.1`; a controller that runs for weeks
  keeps writing to the same file. Closing a terminal or a herdr tab never
  reaches it. Only `fleet-deck stop` or `restart` stops it.
- **`stop` never stops herdr's server.** The server keeps every agent's pane
  running; `herdr server stop` would end them all. After a server restart the
  deck restores the tabs (see Restore).
- **The deck's options** (`--catalog`, `--exclude`, `--cwd`, `--no-fleet-tab`)
  are given to `fleet-deck` and take effect when it starts the controller;
  change them with `fleet-deck restart <options>`. The catalogue defaults to
  the agent-fabric checkout's (`AGENT_FABRIC_ROOT`, else
  `~/projects/agent-fabric`).
- **Outside herdr's panes only.** `server.socket_access = "outside_panes"`
  refuses a pane's processes, so `fleet-deck` refuses to start anything from
  inside a pane (a herdr popup is the operator's own and may).
- **Install** once, on the operator's login:
  `ln -s <this checkout>/fleet-deck/fleet-deck ~/.local/bin/fleet-deck`.

`fabric-deck` is the controller itself; `fleet-deck` starts it. Run in a
terminal by hand, it still refuses to run beside another deck.

## Keys

Every pane runs a harness, so every key combination reaches it as typed:
ctrl+c, ctrl+v, ctrl+z, esc, PageUp and PageDown included, whether or not
something is selected or on the clipboard. herdr keeps only these:

| key | herdr's action |
|---|---|
| **ctrl+6** | the prefix: herdr's commands are ctrl+6 then a key (`ctrl+6 q` detaches; the `prefix+f/a/p` keys below); ctrl+6 twice sends ctrl+6 to the pane. A legacy terminal sends it as ctrl+^ (0x1e), which works too. |
| **ctrl+alt+c** | copies the selection |
| **ctrl+alt+p** | pastes the clipboard's text into the pane |
| **cmd+c / cmd+v** | copy (any system) and paste (macOS), where the terminal forwards cmd |
| **alt+PageUp / alt+PageDown** | scroll herdr's scrollback, when the pane is on its main screen with no mouse reporting (otherwise they reach the pane) |
| the **mouse** | selection, menus and scrolling |

**Copying and pasting.**
- A mouse selection is copied as soon as the button is released, and stays
  highlighted until you click or type. ctrl+alt+c copies it again. The
  right-click menu has Copy and Paste.
- `ctrl+6 [` enters copy mode, to select and copy with the keyboard.
- ctrl+alt+p pastes, and so does the terminal's own paste (ctrl+shift+v in
  ptyxis, which Qubes' clipboard also uses). Either arrives as a paste in the
  pane. ctrl+v goes to the harness, which pastes itself (Claude Code reads an
  image from the clipboard).
- To scroll, use alt+PageUp/PageDown or the mouse wheel.
- `[ui] clipboard_shortcuts = false` gives ctrl+alt+p to the pane as well,
  and ctrl+alt+c unless `copy_on_select = false` (then it is the only key
  that copies a kept selection). A herdr binding configured on either key
  wins over it.

**Why these keys.** Each was checked on 2026-10-10 against what the
harnesses bind, and against what the owner's desktop and terminal keep for
themselves (herdr never receives those).
- **Claude Code:** its default keybindings in every context, chords by their
  first key, and its reserved keys (code.claude.com/docs/en/keybindings).
- **Codex CLI:** its built-in TUI keymap (openai/codex
  `codex-rs/tui/src/keymap.rs` at 806d9732). It binds ctrl+alt+v to paste an
  image, which is why herdr's paste is ctrl+alt+p.
- **A plain shell:** bash's emacs keymap (`bind -p`), with its ESC-ctrl
  bindings counted as ctrl+alt (a legacy terminal sends ctrl+alt+x as ESC
  ctrl+x), and the tty's control characters (`stty -a`).
- **The desktop:**
  - dom0's Xfce: ctrl+alt+l and ctrl+alt+Escape;
  - ptyxis: alt+digits, alt+comma, ctrl(+shift)+PageUp/PageDown and
    ctrl+shift+c/v;
  - the Qubes clipboard: ctrl+shift+c/v.

Upstream's prefix, ctrl+b, is Claude Code's "background the task", and
Codex's and readline's backward-char. Upstream's plain PageUp/PageDown
scrolling, and this fork's earlier ctrl+c/ctrl+v copy and paste (60764f3f),
took keys every harness uses.

`src/client/shell/tests/harness_keys.rs` pins all of it:
- **The lists.** Each harness's keys and the desktop's are written there. The
  prefix, every default direct binding and herdr's own keys are checked
  against them, and so are the `prefix+f/a/p` keys below.
- **Byte for byte.** Through herdr's real input path, the keys a harness uses
  reach the pane exactly as typed, from a legacy terminal and from a kitty
  terminal. ctrl+c still does with a visible selection, and ctrl+v with text
  on the clipboard.
- **herdr's own keys** never reach the pane.
- **ctrl+^.** A binding to it is refused beside the ctrl+6 prefix, since a
  legacy terminal sends both as one byte.

A `keys.prefix` in herdr's config replaces ctrl+6; `fleet-deck` says when it
is one the check does not cover.

## Restore

When the deck starts, and whenever herdr's server comes back after being
lost or replaced, every placed account (`moveto --list`, without the
deck's own login) gets its tab. An account placed while the deck runs
gets its tab within 10 s. One whose restore failed part-way is restored
again whole within 10 s, and is followed in nothing until then.
- **The tab.** A missing tab is created. On a host's first setup the role
  catalogue's group seeds the workspace; afterwards a new account goes to
  its group's workspace if one still exists, else to `New`.
- **A milestone-1 tab** (one unlabelled pane) keeps that pane as the
  harness and gains the shell and status panes.
- **A tab split by hand** with no `harness` pane is left alone, and the
  deck says so once.
- **The shell and status panes** are started once their shell is at its
  prompt, since a pane just created may still be running the operator's
  rc file. One still busy after 120 s is said once, and started whenever
  it reaches its prompt. One already running this account's moveto is
  left as it is.
- **A harness pane running this account's moveto** is classified and
  never touched.
- **Any other harness pane** waits until it is at the operator's prompt,
  and 5 s for the stream. That covers a bare pane, one just created, or
  one still busy (said once after 120 s; it never gets `--wait` in place
  of the decision). Then:
  - if a session runs on the account now, it gets a plain `moveto`
    shell, so an Enter there cannot start a second session;
  - if the account's session was running before the restart and none
    runs now, it gets `--resume`;
  - otherwise, or with no fresh record within 120 s, it gets `--wait`.

## What a person sees

The deck reports each harness pane into herdr's agent panel as the agent
`claude` from the source `fabric`. herdr's default agent row shows the
agent's name, so the deck's word goes in its place, and herdr's own
`claude` comes back while a harness runs.

| state | herdr state (icon, notifications) | the row reads |
|---|---|---|
| a harness runs in the pane | the session's: `working`, `idle` or `blocked` | `claude` |
| waiting for Enter | `idle` | `dormant` |
| the account's shell after a session ended here | `idle` | `shell` |
| a `--resume` the deck started, no harness yet | `working` | `restoring` |
| a `--resume` that produced no harness, or a moveto that ended at once twice | `blocked` | `failed` |
| moveto has no `--wait` yet, so the harness pane is left at the operator's shell | `unknown` | `unknown` |
| something other than this account's moveto holds the harness pane | `unknown` | `unknown` |
| a session runs on the account, not in this pane | `unknown` | `running elsewhere` |
| the account's record is older than two heartbeats, or says the account cannot read its session state (`state: unknown`) | `unknown` | `stale` |

The same words are set as herdr state labels, for a sidebar layout that
shows `state_text`. herdr shows an `idle` the person has not looked at as
`done`, so the word is set for both. The deck's log prints one line per
change.

Each harness pane also carries the metadata token `account=<login>`, so
herdr can tell the accounts apart although every pane reports as
`claude`: `[ui.sound.accounts."<login>"]` in herdr's config gives an
account its own sound per state transition (see the Sound section of the
configuration docs). Sounds follow the herdr state column above, the
deck's own states included: a `--resume` that fails (`working ->
blocked`) sounds as any agent that needs attention does.

## What the deck observes, and never does

- **moveto in the pane** comes from herdr's `pane process-info`. The
  pane's foreground is moveto's `sudo … enter <dir> <title> [mode]`
  hand-off, or the operator's bare shell once moveto ended. A bare shell
  is its own foreground process group (`foreground_process_group_id ==
  shell_pid`).
- **A harness in the pane** is a `claude` process, or agent-fabric's
  `launch.py`, among the descendants of that `sudo`.
  - sudo runs the account in a pty of its own (`use_pty`), so the harness
    is never in the pane's foreground. The deck walks `/proc/<pid>/stat`
    parent links instead, and reads only `stat`, plus `cmdline` of those
    descendants.
  - This was measured live on develop-qzapp (GZCoord seq 22145); `/proc`
    is mounted without `hidepid`.
- **A pane entered over ssh** (`moveto --via ssh`, agent-fabric ADR-048)
  is the account's when its foreground is `ssh` connecting as the account
  with one of the forced command's four words as the whole remote command:
  `--wait`, `--watch`, `--resume`, or `shell` (plain). The argv is read as
  OpenSSH reads it: the user is the first one set, by `-l`, `-o User=` or
  `<login>@`; options after the destination are ssh's too, so a word that
  starts with `-` must follow `--` (`ssh <login>@<host> -- --wait`). It is classified, followed and never typed into, like a
  sudo pane.
  - sshd starts the account's session, so its harness is no descendant of
    the pane's `ssh`, and nothing the deck may read links the two. The
    account's live session, from the stream, is taken as the pane's.
  - So a session started elsewhere shows as running in an ssh pane, never
    as `running elsewhere`; an Enter there is still refused by
    `fabric-resume`. A stale record changes nothing.
- **The account's sessions** come from `fabric-ctl all states --follow
  --json`, this host's records only. The stream is restarted after 1, 2,
  5, 10, 30, then 60 s.
- **herdr's server instance** is the pid at the socket's other end
  (`SO_PEERCRED`) plus that process's start ticks.
  - The socket is the one the deck's herdr commands reach, in herdr's
    own order: `HERDR_SOCKET_PATH`, else the session `HERDR_SESSION`
    names, else the default session, each as `herdr session list --json`
    reports it.
  - A different or unreadable instance is a loss of herdr, and its next
    answer is a restore.
  - A failing `moveto --list` is not a loss: the last tab map stays.
- **What the deck does to panes:** it starts a fixed `moveto <account>
  [--wait|--resume|--watch]` only in the operator's own bare shell. While
  moveto runs, it never types into a pane, sends a key or closes one. A
  pane a person closes stays closed until the next restore.
- **A pane the deck stops following** gives back what the deck set: the
  agent row, the displayed name and the state labels. That covers a pane
  whose account left `moveto --list`, and one whose tab or label changed.
- **What the deck never does:** it never retries a failed `--resume`. One
  Enter in the re-armed `--wait` pane does that.
- **A moveto that keeps failing:** when a moveto the deck started ends
  within 15 s with no harness, twice in a row, the deck stops starting it.
  The row reads `failed`, the deck says so, and the pane's last lines say
  why. A person who starts moveto in that pane is followed again, and so
  is a restore.

Until agent-fabric's activation PR puts `--wait`, `--watch` and
`fabric-resume`'s second-session refusal on main, `moveto --help` does not
list them. The deck then arms no pane with them, says so once, and shows
those harness panes as herdr's `unknown`.

## What the deck keeps

`$XDG_STATE_HOME/fabric-deck/before.json` (mode 0600) holds, per placed
account, whether its session was running. It is read only at a restore,
to decide `--resume`.
- **A rise** is written at once.
- **A fall** is written only once herdr has visibly stayed up. That takes
  an answer from the same server instance as the last answer before the
  fall, at least 5 s after it. A fall seen around a loss of herdr never
  settles, so a session that died with herdr is resumed.
- **A fall still pending when the deck stops** is kept with its server
  instance. At the next start it settles only if that same server is
  still running.
- **An account that leaves `moveto --list`** is dropped at the next write.

## Views

`fabric-view` shows what the fleet does, as a fleet and per agent, in
herdr plugin panes (`herdr-plugin.toml`, plugin `fabric.fleet`). Its data
is agent-fabric's `tools/fabric/fleet.py`, imported from the agent-fabric
checkout and never copied (agent-fabric ADR-046). The checkout is
`AGENT_FABRIC_ROOT`, else the one the installed `fabric-ctl` links into.

```sh
herdr plugin link <this checkout>/fleet-deck   # once, on the operator's login
```

and, in herdr's `config.toml`, the keys:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "fabric.fleet.board"
description = "fleet board"

[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "fabric.fleet.agent"
description = "this agent"

[[keys.command]]
key = "prefix+p"
type = "plugin_action"
command = "fabric.fleet.prs"
description = "pull requests in flight"
```

The deck opens the board as a tab of its own, labelled `fleet`, in the
first workspace, unless one exists anywhere (`--no-fleet-tab` turns this
off). Like an account's pane, a fleet tab a person closes stays closed
until the next restore. herdr's session restore brings a tab back by its
label but not a plugin pane's program: a `fleet` tab whose one pane is a
bare shell in the plugin's own directory (`fleet-deck/`, where herdr
restores it) is the deck's, closed and replaced by the board. A pane whose
program has not started yet is looked at again at the next map read. A
`fleet` tab running anything else, or a shell elsewhere, is a person's,
left alone and said once.
Without the plugin linked, the deck says so once and restores the
account tabs as before.

| view | opens as | shows |
|---|---|---|
| **board** | the `fleet` tab | one row per placed agent: state, current job, closed/open jobs, RSS, CPU, tokens over 7 days, the 5-hour usage window, open PR; each host's memory and memory pressure below |
| **compare** | `c` in the fleet tab | one bar per agent for one metric: RSS, CPU, swap, tokens over 7 days, jobs done, open jobs; largest first, scaled to the largest, labelled "operational, not a score" |
| **plan** | `p` in the fleet tab | each plan the coordinator keeps (agent-fabric ADR-047): its steps on a board by state (to do, queued, doing, delivered, done, and any other state), and a Gantt below |
| **PRs** | a popup (`fabric.fleet.prs`), or `P` in the fleet tab | in-flight pull requests by owner, each with its repository, title, work and fix commits, checks, review, armed, and the gate's verdict |
| **agent** | an overlay over the tab it was opened from, or Enter on a board row | state, role, project, session, what it waits on; resources, with a sparkline of the samples taken while open; open and closed jobs with their times; its PRs; tokens over 7 days; usage windows |

- **Keys.** ↑/↓ (or j/k), PgUp/PgDn, Home/End move. Enter on the board opens
  that agent, and Esc goes back to the board. In the fleet tab, `b`, `c`,
  `p` and `P` switch between board, compare, plan and PRs, and Esc leaves
  PRs for the screen it was opened from. In compare, ←/→ (or h/l, Tab)
  and 1-6 choose the metric; the chosen one is bracketed in the footer.
  `r` refetches the screen's sections once. `q` closes the pane, as does
  Esc on an overlay or the popup.
- **Compare.** A value that was not read has no bar and reads `…` or `?`,
  and is listed after the rest; the footer says why. "Jobs done" counts
  jobs closed as done, never dropped: fleet.py's `done_total`. Without
  it, the list of closed jobs is counted only when it is the whole list.
- **Plan.** A step's state is its job's (ADR-047). Each Gantt row is the
  step's id, owner, title and what it waits for (`⇠s1,s2`). A bar from the
  job's log (first active to last done, dropped or delivered; to now while
  running) is solid. A step with no time recorded is drawn hollow and
  marked `est`: it is placed after what it waits for, or at now, for its
  `est_days`, else one day. The `│` column is now. fabric-plan reads the
  plan files of the login it runs as, so on any login but the
  coordinator's the view says `no plans kept by <login>`.
- **PRs.** fleet.py reads fabric-pr gate in agent-fabric's checkout only,
  and the popup says so. A pushed branch without a PR is counted under its
  owner, not listed. A PR whose owner is no placed account is listed apart.
  A gate or GitHub that did not answer is said beside the owner. A
  fleet.py that does not list PRs of unplaced owners is said so.
- **The agent.** The overlay is that of the tab it was opened from: the
  deck labels an agent's tab with its login, and the view reads the label
  from `HERDR_PLUGIN_CONTEXT_JSON`. On any other tab it says so.
- **On demand.** At open, a view draws fleet.py's cache as it is, saying
  how old it is, and fetches. While the pane has focus (terminal focus
  reporting, DECSET 1004), it fetches each section again once that section's
  time to live has passed: 5 s for proc and states, up to 600 s for tokens
  (fleet.py's cost classes). When focus leaves, the status line says
  `paused` and nothing new is fetched. A fetch already running finishes,
  since fleet.py bounds every source.
- **Focus starts unknown.** herdr reports focus changes only. So a view
  counts as focused from the first focus-in or key, and until then it
  stays at its open-time fetch, saying `press a key to go live`. A view
  opened by its action (the keys above) is opened focused and starts live.
- **What a cell says.** `…` means not read yet. `?` means its section failed,
  and the board's footer says why, once per section. `-` means there is
  none. A value ending in `~` is stale: fleet.py's last good value, kept
  while a fresh read fails inside the section's stale window. The footer
  (board, compare), the owner's line (PRs) or the section (agent view)
  says whose, since when and why. The selected row is marked `>`, and every state is a word, never
  only a colour. `NO_COLOR` turns colour off.
- **Who can read what.** fleet.py's `jobs`, `usage`, `host`, `tokens` and
  `accounts` sections answer only a host operator. The views are meant for
  the operator's login; on any other they show those sections as `?`,
  with fabric-ctl's refusal.
- **What a view never does.** It calls no herdr socket: `outside_panes`
  refuses a pane's process. The overlay is opened by the plugin's action,
  which herdr runs outside the panes. A view sends no signed action and
  reads no other source than fleet.py.
- **What it writes.** Nothing of its own; it runs Python with `-B`.
  - fleet.py, which it calls, keeps its shared cache under
    `$XDG_RUNTIME_DIR/fabric-fleet/` (ADR-046 rule 2).
  - fleet.py's `prs` section runs pr-gate, which fetches origin in the
    agent-fabric checkout (fleet.py's documented side effect): at open,
    on `r`, and every 120 s while focused.
  - Quitting while a fetch is writing can leave one of fleet.py's
    temporary files in that cache directory.

## Tests

`just fleet-deck-test` runs the state machine (`test_deck_tabs`) and the
deck against a fake herdr, a fake `/proc` and a real Unix socket
(`test_fabric_deck`), without a server. It also runs both views' layout on
fixture records (`test_view_render`, `test_report_render`), and the views' input,
navigation, refresh rule and data location (`test_fabric_view`).

The deck was also run against a real, isolated herdr server, with
stand-ins for `moveto` and `fabric-ctl`. The `moveto` stand-in has the
same `sudo … enter` argv shape and a pty of its own for the account's
shell. The run covered:
- restore, with `--resume` and `--wait`;
- Enter;
- a session ending;
- a deck restart that re-armed nothing;
- a herdr server restart that resumed only the session that died with
  it.

The rendered herdr client was inspected on a private X display. The real
`moveto`, `fabric-resume` and the fleet's stream have not been run with
it.

"""Restart Ghostty with every tab restored and the Claude Code sessions resumed.

Why this exists. macOS grants ~/Documents (and Desktop, Downloads) to Ghostty, but it checks each
process that touches those folders. When a program still running in a tab has had its file
deleted (`brew upgrade` replacing claude, node or Ghostty itself), macOS can no longer identify it
("Failed to create Attribution Chain" in tccd's log). It refuses, and from then on it refuses
every process in Ghostty until Ghostty relaunches: reads fail with "Operation not permitted" while
System Settings still shows Ghostty allowed. Restarting while that program is idle prevents the
cut; restarting after the cut ends it.

A restart:
  1. reads every window, tab and split of Ghostty (folder, tty) and the Claude sessions in them
  2. ends those sessions (SIGTERM, like the Close action), then quits Ghostty
  3. relaunches Ghostty fresh and rebuilds the windows, tabs and splits in the same folders.
     A tab that ran Claude types `claude <same flags> --resume <session id>`. The ANTHROPIC_* and
     CLAUDE_CODE_* variables the session started with (an alias such as claude-kimi) are passed
     as tab environment, so they are neither typed on screen nor written to disk.

Other programs running in a tab (a dev server, an editor) are not restarted: the tab comes back
as a shell in the same folder. Splits come back side by side, not in their exact layout.

`serve` restarts by itself only when Claude Code (or Ghostty) was replaced, and only once every
Claude session in Ghostty is idle and the keyboard was left alone for a minute. When another
program was replaced (a reinstall swapping node_modules/.bin/turbo under a running `turbo dev`),
it only warns: restarting that program is enough, and a Ghostty restart would stop dev servers.
"""

from __future__ import annotations

import collections
import ctypes
import errno
import json
import os
import shlex
import signal
import subprocess
import sys
import time

import cockpit as cp

BUNDLE = "com.mitchellh.ghostty"
EXE_SUFFIX = ".app/Contents/MacOS/ghostty"

STATE_NAME = "ghostty-restart.json"  # the last restart: running, when, result
REQUEST_NAME = "ghostty-restart-request.json"  # asks `serve` to restart (from inside Ghostty)
PLAN_NAME = "ghostty-restart-plan.json"  # the layout of the last restart, for --restore

CHECK_EVERY = 5.0  # seconds between scans for deleted programs
SETTLED = 10.0  # a deleted program must be seen this long before an automatic restart
AWAY = 60.0  # automatic restarts wait until keyboard and mouse were untouched this long
COOLDOWN = 600.0  # no automatic restart within this many seconds of the last restart
RUNNING_FOR = 180.0  # a restart still marked running after this long is dead
REQUEST_WAIT = 6.0  # how long a request from inside Ghostty waits for `serve` to take it
# Set on a restart `serve` runs for a request: the PIDs of the session that asked, which may be
# ended while busy (it is busy only running the request).
SPARE_ENV = "COCKPIT_RESTART_SPARE"
AUTO_FOR = {"claude", "Ghostty"}  # replaced programs that restart Ghostty by themselves

ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
ENV_NAMES = ("CLAUDE_CONFIG_DIR",)

# Ghostty is opened with the environment the Dock would give it. `open` hands the caller's
# environment to the app, and this process may carry a Claude session's (see
# cockpit.CLAUDE_INTERNAL_ENV): every tab would then start claude as a child session.
LAUNCH_ENV = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "SSH_AUTH_SOCK", "__CF_USER_TEXT_ENCODING", "LANG", "LC_ALL", "LC_CTYPE")
LAUNCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# Sent to a session the restart stopped mid-turn, so it picks the work up again.
CONTINUE_PROMPT = "Ghostty was restarted while you were working, which stopped your last turn. Continue where you left off."
WORKING = ("busy", "waiting")

# claude flags kept when resuming. Everything else (a prompt, --resume, --continue) is dropped.
SWITCHES = {
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--verbose",
    "--strict-mcp-config",
    "--chrome",
    "--no-chrome",
    "--ide",
}
ONE_VALUE = {"--model", "--permission-mode", "--agent", "--settings", "--fallback-model", "--system-prompt", "--append-system-prompt"}
MANY_VALUES = {"--add-dir", "--mcp-config", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools"}
DROPPED_WITH_VALUE = {"--resume", "-r", "--session-id"}


# ------------------------------------------------------------------- state files


def _path(name):
    return cp.DATA_DIR / name


def load_state():
    try:
        data = json.loads(_path(STATE_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(data):
    cp.write_json_atomic(_path(STATE_NAME), data)


def restart_running(state=None, now=None):
    state = load_state() if state is None else state
    now = time.time() if now is None else now
    return bool(state.get("running") and cp.alive(state.get("pid")) and now - state.get("started", 0) < RUNNING_FOR)


# --------------------------------------------------------------------- processes


def process_table():
    """{pid: (ppid, stat, command path)} for every process."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,comm="], capture_output=True, text=True, timeout=10).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
    return table


def ghostty_pid(table):
    return next((pid for pid, (ppid, _stat, comm) in sorted(table.items()) if ppid == 1 and comm.endswith(EXE_SUFFIX)), None)


def descendants(root, table):
    children = collections.defaultdict(list)
    for pid, (ppid, _stat, _comm) in table.items():
        children[ppid].append(pid)
    out, todo = [], [root]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child)
    return out


def ancestors(pid, table):
    out = []
    while pid in table and pid not in out:
        out.append(pid)
        pid = table[pid][0]
    return out


_libproc = None


def exe_path(pid):
    """The program file a process runs; None when that file is gone, False when the process is gone.

    proc_pidpath follows a renamed file and fails once it is deleted. The path is not checked on
    disk: that would ask macOS for Desktop or Downloads access on behalf of this app.
    """
    global _libproc
    if _libproc is None:
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    buf = ctypes.create_string_buffer(4096)
    if _libproc.proc_pidpath(int(pid), buf, 4096) > 0:
        return buf.value.decode(errors="replace")
    return False if ctypes.get_errno() == errno.ESRCH else None


def stale_processes(table=None):
    """[(pid, name)] for processes in Ghostty, Ghostty included, whose program file was deleted."""
    table = process_table() if table is None else table
    root = ghostty_pid(table)
    if not root:
        return []
    out = []
    for pid in [root, *descendants(root, table)]:
        _ppid, stat, comm = table[pid]
        if stat.startswith("Z"):  # a zombie has no program left, and is harmless
            continue
        if exe_path(pid) is None:
            out.append((pid, "Ghostty" if pid == root else os.path.basename(comm)))
    return out


def summarize(stale):
    counts = collections.Counter(name for _pid, name in stale)
    return [f"{name} ×{n}" if n > 1 else name for name, n in sorted(counts.items())]


def parse_procargs(data):
    """argv and environment out of a KERN_PROCARGS2 buffer: argc, exec path, padding, argv, env."""
    if len(data) < 4:
        return [], {}
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    end = rest.find(b"\0")
    if end < 0:
        return [], {}
    parts = rest[end:].lstrip(b"\0").split(b"\0")
    argv = [p.decode(errors="replace") for p in parts[:argc]]
    env = {}
    for p in parts[argc:]:
        if not p:
            break
        key, sep, value = p.decode(errors="replace").partition("=")
        if sep:
            env[key] = value
    return argv, env


def proc_args(pid):
    """(argv, environment) a process of this user was started with."""
    libc = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
    argmax, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:  # KERN_ARGMAX
        return [], {}
    buf, size = ctypes.create_string_buffer(argmax.value), ctypes.c_size_t(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, int(pid)), 3, buf, ctypes.byref(size), None, 0) != 0:  # KERN_PROCARGS2
        return [], {}
    return parse_procargs(buf.raw[: size.value])


def user_idle_seconds():
    """Seconds since the last key press or mouse move, None when unknown."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if '"HIDIdleTime" =' in line:
            value = line.split("=", 1)[1].strip()
            return int(value) / 1e9 if value.isdigit() else None
    return None


# ------------------------------------------------------------------------ resume


def resume_flags(args):
    """The flags of a claude command line worth keeping when it is resumed."""
    kept, i = [], 0
    while i < len(args):
        arg = args[i]
        name = arg.split("=", 1)[0]
        i += 1
        if arg in SWITCHES:
            kept.append(arg)
        elif name in ONE_VALUE or name in MANY_VALUES:
            kept.append(arg)
            if "=" in arg:
                continue
            if name in ONE_VALUE:
                if i < len(args):
                    kept.append(args[i])
                    i += 1
            else:
                while i < len(args) and not args[i].startswith("-"):
                    kept.append(args[i])
                    i += 1
        elif arg in DROPPED_WITH_VALUE and i < len(args) and not args[i].startswith("-"):
            i += 1
    return kept


def resume_env(env):
    """The settings a session was started with (an alias such as claude-kimi), not Claude Code's own markers."""
    return {
        k: v for k, v in env.items() if (k.startswith(ENV_PREFIXES) or k in ENV_NAMES) and k not in cp.CLAUDE_INTERNAL_ENV and "\n" not in v
    }


def resume_command(sid, args, prompt=None):
    return shlex.join(["claude", *resume_flags(args), "--resume", sid, *([prompt] if prompt else [])])


def resume_target(args):
    """The session id a claude command line resumes, if any."""
    for i, arg in enumerate(args):
        if arg.startswith("--resume="):
            return arg.split("=", 1)[1] or None
        if arg in ("--resume", "-r") and i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return None


def process_cwd(pid):
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=10).stdout
    return next((line[1:] for line in out.splitlines() if line.startswith("n")), "")


def unregistered_sessions(known):
    """Resumed claude processes with no ~/.claude/sessions entry.

    A claude that inherited a child-session marker registers nowhere and saves no transcript: its
    `--resume` id is the latest saved state, so that is what comes back.
    """
    out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=10).stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit() or os.path.basename(parts[1]) != "claude" or int(parts[0]) in known:
            continue
        pid = int(parts[0])
        sid = resume_target(proc_args(pid)[0][1:])
        if sid:
            found.append({"pid": pid, "sessionId": sid, "cwd": process_cwd(pid), "status": "unknown", "name": sid[:8]})
    return found


# ------------------------------------------------------------------------ layout

LAYOUT = """
set sep to character id 9
tell application id "com.mitchellh.ghostty"
  set frontId to ""
  try
    set frontId to id of front window
  end try
  set out to ""
  set wi to 0
  repeat with w in windows
    set wi to wi + 1
    set isFront to ((id of w) is frontId)
    repeat with t in tabs of w
      set ti to index of t
      set sel to selected of t
      repeat with term in terminals of t
        set wd to ""
        try
          set wd to working directory of term
        end try
        if wd is missing value then set wd to ""
        set out to out & wi & sep & isFront & sep & ti & sep & sel & sep & (tty of term) & sep & wd & linefeed
      end repeat
    end repeat
  end repeat
  return out
end tell
"""

WINDOW_IDS = """
tell application id "com.mitchellh.ghostty"
  set out to ""
  repeat with w in windows
    set out to out & (id of w) & linefeed
  end repeat
  return out
end tell
"""

# Closes the windows whose ids are given, one at a time: closing shifts the others' indexes.
CLOSE_WINDOWS = """
on run argv
  tell application id "com.mitchellh.ghostty"
    repeat 50 times
      set found to false
      repeat with w in windows
        if argv contains ((id of w) as text) then
          close window w
          set found to true
          exit repeat
        end if
      end repeat
      if not found then exit repeat
    end repeat
  end tell
  return "ok"
end run
"""

QUIT = 'tell application id "com.mitchellh.ghostty" to quit'


def read_layout():
    """[{window, front, tab, selected, tty, cwd}] in window, tab and split order."""
    rows = []
    for line in cp.osascript(LAYOUT, timeout=20).splitlines():
        parts = line.split("\t")
        if len(parts) != 6 or not parts[0].isdigit() or not parts[2].isdigit():
            continue
        rows.append(
            {
                "window": int(parts[0]),
                "front": parts[1] == "true",
                "tab": int(parts[2]),
                "selected": parts[3] == "true",
                "tty": parts[4],
                "cwd": parts[5],
            }
        )
    return rows


def make_plan(layout, sessions, tty_of, args_of):
    """Windows > tabs > terminals: each terminal with its folder and, when Claude ran there, how to resume it.

    sessions: live Claude sessions; tty_of: {pid: tty}; args_of: pid -> (argv, environment).
    """
    by_tty = {tty_of[s["pid"]]: s for s in sessions if tty_of.get(s["pid"])}
    home = os.path.expanduser("~")
    windows = {}
    for row in layout:
        w = windows.setdefault(row["window"], {"front": row["front"], "selected": None, "tabs": {}})
        if row["selected"]:
            w["selected"] = row["tab"]
        term = {"cwd": row["cwd"] or home, "resume": None}
        s = by_tty.get(row["tty"])
        if s:
            argv, env = args_of(s["pid"])
            status = s.get("status") or "idle"
            term = {
                "cwd": s.get("cwd") or term["cwd"],
                "resume": {
                    "pid": s["pid"],
                    "sid": s["sessionId"],
                    "name": s.get("name") or s["sessionId"][:8],
                    "status": status,
                    "command": resume_command(s["sessionId"], argv[1:], CONTINUE_PROMPT if status in WORKING else None),
                    "env": resume_env(env),
                },
            }
        w["tabs"].setdefault(row["tab"], []).append(term)
    plan = []
    for _, w in sorted(windows.items()):
        order = sorted(w["tabs"])
        selected = order.index(w["selected"]) + 1 if w["selected"] in order else 1
        plan.append({"front": w["front"], "selected": selected, "tabs": [w["tabs"][k] for k in order]})
    return plan


def resumed(plan):
    return [t["resume"] for w in plan for tab in w["tabs"] for t in tab if t["resume"]]


def redacted(plan):
    """The plan without environment values, to keep on disk."""
    out = json.loads(json.dumps(plan))
    for r in resumed(out):
        r["env"] = {k: "" for k in r["env"]}
        r["env_values_dropped"] = True
    return out


def aq(text):
    """An AppleScript string literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def surface(term, with_env=True):
    props = [f"initial working directory:{aq(term['cwd'])}"]
    r = term.get("resume")
    if r:
        props.append(f"initial input:{aq(r['command'])} & linefeed")
        env = {k: v for k, v in r.get("env", {}).items() if with_env and not r.get("env_values_dropped")}
        if env:
            props.append("environment variables:{" + ", ".join(aq(f"{k}={v}") for k, v in sorted(env.items())) + "}")
    return "{" + ", ".join(props) + "}"


def build_script(plan):
    lines = ['tell application id "com.mitchellh.ghostty"']
    front = None
    for wi, w in enumerate(plan, 1):
        if not w["tabs"]:
            continue
        lines.append(f"set w{wi} to new window with configuration {surface(w['tabs'][0][0])}")
        lines.append("delay 0.2")
        for ti, tab in enumerate(w["tabs"], 1):
            if ti == 1:
                lines.append(f"set t to tab 1 of w{wi}")
            else:
                lines.append(f"set t to new tab in w{wi} with configuration {surface(tab[0])}")
                lines.append("delay 0.15")
            lines.append("set term to focused terminal of t")
            for extra in tab[1:]:
                lines.append(f"set term to split term direction right with configuration {surface(extra)}")
                lines.append("delay 0.15")
        lines.append(f"select tab (tab {w['selected']} of w{wi})")
        if w["front"]:
            front = wi
    if front:
        lines.append(f"activate window w{front}")
    lines.append("end tell")
    return "\n".join(lines)


def run_script(script, timeout=120):
    """Runs AppleScript given on stdin, so nothing in it shows in the process list."""
    result = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        if "-1743" in result.stderr:
            raise RuntimeError(cp.NOT_ALLOWED)
        raise RuntimeError(result.stderr.strip()[-300:] or f"osascript exit {result.returncode}")
    return result.stdout.strip()


def window_ids():
    return [line for line in cp.osascript(WINDOW_IDS, timeout=10).splitlines() if line]


def notify(title, text):
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]
    try:
        subprocess.run(["osascript", *script, title, text], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


# ----------------------------------------------------------------------- restart


def wait_gone(pid, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not cp.alive(pid):
            return True
        time.sleep(0.2)
    return not cp.alive(pid)


def end_sessions(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)  # these exact processes only
        except OSError:
            pass
    deadline = time.time() + 10
    while time.time() < deadline and any(cp.alive(p) for p in pids):
        time.sleep(0.25)


def quit_ghostty(pid):
    try:
        cp.osascript(QUIT, timeout=8)
    except Exception as exc:  # noqa: BLE001 - a quit confirmation or no answer: signals follow
        cp.log(f"ghostty quit: {exc}")
    if wait_gone(pid, 8):
        return
    for sig, seconds in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        if wait_gone(pid, seconds):
            return
    raise RuntimeError("Ghostty did not quit")


def launch_fresh():
    """Opens Ghostty without restoring its windows; returns the ids of the windows it opens by itself."""
    subprocess.run(["open", "-F", "-b", BUNDLE], check=True, timeout=30, env=launch_env())
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            ids = window_ids()
        except Exception:  # noqa: BLE001 - still starting
            ids = []
        if ids:
            time.sleep(0.5)
            return window_ids()
        time.sleep(0.5)
    return []  # initial-window = false


def launch_env():
    env = {k: v for k, v in os.environ.items() if k in LAUNCH_ENV}
    env["PATH"] = LAUNCH_PATH
    return env


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def restart(force=False, auto=False, spare=None):
    requested = SPARE_ENV in os.environ
    if spare is None:
        spare = {int(p) for p in os.environ.get(SPARE_ENV, "").split(",") if p.isdigit()}
    if restart_running():
        raise RuntimeError("Ghostty is already restarting")
    table = process_table()
    gpid = ghostty_pid(table)
    if not gpid:
        raise RuntimeError("Ghostty is not running")
    layout = read_layout()
    if not layout:
        raise RuntimeError("Ghostty has no window to restore")
    sessions = cp.live_sessions()
    sessions += unregistered_sessions({s["pid"] for s in sessions})
    plan = make_plan(layout, sessions, cp.ttys_of([s["pid"] for s in sessions]), proc_args)
    claude = resumed(plan)
    busy = [r for r in claude if r["status"] in WORKING and r["pid"] not in spare]
    if busy and not force:
        names = ", ".join(cp.clip(r["name"], 40) for r in busy)
        reason = f"{plural(len(busy), 'Claude session')} still working ({names})"
        if requested:  # run by `serve` for a Ghostty tab: nobody sees the error below
            cp.log(f"ghostty restart canceled: {reason}")
            notify("Ghostty restart canceled", f"{reason}. Wait, or restart anyway with --force.")
        raise RuntimeError(f"{reason}. Wait, or restart anyway with --force.")

    tabs = sum(len(w["tabs"]) for w in plan)
    started = time.time()
    save_state({"running": True, "pid": os.getpid(), "started": started, "auto": auto})
    cp.write_json_atomic(_path(PLAN_NAME), {"at": started, "windows": redacted(plan)})
    cp.log(
        f"ghostty restart ({'auto' if auto else 'asked'}): {plural(tabs, 'tab')}, {plural(len(claude), 'Claude session')}, deleted programs: {summarize(stale_processes(table))}"
    )
    try:
        end_sessions([r["pid"] for r in claude])
        quit_ghostty(gpid)
        opened = launch_fresh()
        run_script(build_script(plan))
        if opened:
            cp.osascript(CLOSE_WINDOWS, *opened, timeout=15)
    except Exception as exc:
        save_state({"running": False, "started": started, "finished": time.time(), "auto": auto, "error": str(exc)})
        cp.log(f"ghostty restart failed: {exc!r}")
        notify("Ghostty restart failed", f"{exc}. Rebuild the tabs with: cockpit.py restart-ghostty --restore")
        raise RuntimeError(
            f"Ghostty restart failed: {exc}. The tabs are saved; rebuild them with `cockpit.py restart-ghostty --restore`."
        ) from None
    message = f"{plural(tabs, 'tab')} restored, {plural(len(claude), 'Claude session')} resumed"
    save_state({"running": False, "started": started, "finished": time.time(), "auto": auto, "result": message})
    cp.log(f"ghostty restarted: {message}")
    notify("Ghostty restarted", message)
    return f"Ghostty restarted: {message}"


def restore():
    """Rebuilds the tabs of the last restart (after a failed one), next to the windows open now."""
    try:
        plan = json.loads(_path(PLAN_NAME).read_text())["windows"]
    except (OSError, ValueError, KeyError):
        raise RuntimeError("No saved tabs to restore") from None
    if not ghostty_pid(process_table()):
        launch_fresh()
    run_script(build_script(plan))
    return f"Rebuilt {plural(sum(len(w['tabs']) for w in plan), 'tab')}"


# ---------------------------------------------- asking from inside Ghostty itself


def request(force=False):
    """From a Ghostty tab, `serve` (outside Ghostty) has to do the restart: this process dies with Ghostty."""
    table = process_table()
    root = ghostty_pid(table)
    mine = set(ancestors(os.getpid(), table))
    inside = set(descendants(root, table)) if root else set()
    busy = [s for s in cp.live_sessions() if s["pid"] in inside and s["pid"] not in mine and s.get("status") not in (None, "idle")]
    if busy and not force:
        names = ", ".join(cp.clip(s.get("name") or s["sessionId"][:8], 40) for s in busy)
        raise RuntimeError(f"{plural(len(busy), 'Claude session')} still working ({names}). Wait, or restart anyway with --force.")
    path = _path(REQUEST_NAME)
    # `serve` checks again when it restarts: a session may start working in between. Only the
    # one this was typed in (busy just running this command) may be ended without --force.
    cp.write_json_atomic(path, {"at": time.time(), "force": force, "spare": sorted(mine)})
    deadline = time.time() + REQUEST_WAIT
    while time.time() < deadline:
        if not path.exists():
            return "Claude Cockpit is restarting Ghostty: the tabs come back in a few seconds."
        time.sleep(0.2)
    try:
        path.unlink()
    except OSError:
        pass
    raise RuntimeError("Claude Cockpit is not running, and a restart has to come from outside Ghostty. Open Claude Cockpit and try again.")


def take_request():
    path = _path(REQUEST_NAME)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        data = {}
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(data, dict) or time.time() - data.get("at", 0) > 30:
        return None
    return data


def inside_ghostty():
    table = process_table()
    root = ghostty_pid(table)
    return bool(root) and root in ancestors(os.getpid(), table)


def command(force=False, auto=False, restore_saved=False):
    """`cockpit.py restart-ghostty`."""
    if inside_ghostty():
        if restore_saved:
            return restore()
        return request(force)
    return restore() if restore_saved else restart(force=force, auto=auto)


def spawn_restart(force=False, auto=False, spare=None):
    """Runs a restart in its own process, so `serve` keeps reporting meanwhile.

    `spare`: set for a request from a Ghostty tab, the PIDs of the session that asked.
    """
    args = [sys.executable, os.path.abspath(cp.__file__), "restart-ghostty"]
    args += ["--force"] if force else []
    args += ["--auto"] if auto else []
    env = dict(os.environ)
    env.pop(SPARE_ENV, None)
    if spare is not None:
        env[SPARE_ENV] = ",".join(str(p) for p in spare)
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)


# --------------------------------------------------------------------- watching


class Watcher:
    """Called by `serve` on every tick: notices deleted programs under Ghostty and starts restarts."""

    def __init__(self, spawn=None, scan=None, idle=None):
        self.spawn = spawn or spawn_restart
        self.scan = scan or stale_processes
        self.idle = idle or user_idle_seconds
        self.checked_at = 0.0
        self.stale = []  # "claude ×2", "turbo"
        self.claude = False  # Claude Code or Ghostty itself among them: restart by itself
        self.stale_since = None
        self.notified = None
        self.spawned_at = 0.0

    def tick(self, rows, now=None):
        """The `ghostty` part of a snapshot: None while there is nothing to say."""
        now = time.time() if now is None else now
        asked = take_request()
        if asked is not None:
            spare = [p for p in asked.get("spare") or [] if isinstance(p, int)]
            self.spawn(force=bool(asked.get("force")), auto=False, spare=spare)
            self.spawned_at = now
        if now - self.checked_at >= CHECK_EVERY:
            self.checked_at = now
            try:
                found = self.scan()
            except Exception as exc:  # noqa: BLE001 - a failed scan finds nothing
                cp.log(f"deleted program scan failed: {exc!r}")
                found = []
            self.stale = summarize(found)
            self.claude = any(name in AUTO_FOR for _pid, name in found)
            if not self.stale:
                self.stale_since = self.notified = None
            elif self.stale_since is None:
                self.stale_since = now
        state = load_state()
        restarting = restart_running(state, now) or now - self.spawned_at < 5
        if not self.stale and not restarting:
            return None
        auto = bool(cp.config().get("auto_restart_ghostty", True)) and self.claude
        waiting = None
        if self.stale and not restarting:
            waiting = self.blocker(rows, now, state) if auto else None
            if auto and waiting is None:
                self.spawn(force=False, auto=True)
                self.spawned_at, restarting = now, True
            elif self.notified != self.stale:
                self.notified = list(self.stale)
                names = ", ".join(self.stale)
                if auto:
                    text = f"{names} was replaced while running. Ghostty restarts by itself, with your Claude sessions resumed, once they are idle and you leave the keyboard for a minute."
                elif self.claude:
                    text = f"{names} was replaced while running. Restart Ghostty from Claude Cockpit before it loses access to Documents."
                else:
                    text = f"{names} was replaced while running in Ghostty. Restart it (or Ghostty, from Claude Cockpit) before Ghostty loses access to Documents."
                notify("Ghostty needs a restart", text)
        return {"stale": self.stale, "auto": auto, "waiting": waiting if auto and not restarting else None, "restarting": restarting}

    def blocker(self, rows, now, state):
        """Why an automatic restart has to wait, or None when it can go now."""
        if now - (self.stale_since or now) < SETTLED:
            return "Checking"
        if now - state.get("started", 0) < COOLDOWN:
            return "Restarted a few minutes ago: restart again by hand if needed"
        working = [
            r for r in rows if r.get("tab") is not None and (r.get("status") != "idle" or r.get("kind") in ("picker", "plan", "permission"))
        ]
        if working:
            return f"Waiting for {plural(len(working), 'session')} to finish"
        idle = self.idle()
        if idle is None or idle < AWAY:
            return "Waiting until you leave the keyboard for a minute"
        return None

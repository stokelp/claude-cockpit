#!/usr/bin/env python3
"""Claude Cockpit: where every open Claude Code session stands.

Commands used by the menu bar app:
  serve                   print a JSON snapshot (one per line) whenever something changes
  focus PID               bring the session's terminal tab to the front
  focused                 print the PID in the terminal tab in front of you, if any
  reply PID               type stdin into an idle session and press Enter
  clear PID               type /clear into an idle session: same tab, fresh conversation
  close PID               end an idle session (SIGTERM to that PID) and close its tab
  check SESSION_ID        tick the follow-up whose text is on stdin (uncheck: untick it)
  hooks-status            print installed, missing or outdated

Setup and debugging:
  hook                    Claude Code hook entry point (Stop, StopFailure, Notification)
  install-hooks           add the hook to Claude Code's user settings (a backup is kept)
  uninstall-hooks         remove it again
  snapshot [--no-judge]   print one snapshot, indented
  judge SESSION_ID        judge one session now and print the verdict
  demo                    print a snapshot made of sample sessions

Every open session lands in one group:
  needs_you   a question picker, a plan or a permission prompt is waiting, or the
              judge found that Claude asked something it cannot go on without
  working     a turn is running
  followups   the judge found the work finished, with concrete items left (a checklist:
              once you tick every item, the session moves to close)
  done        the judge found the work finished, worth a look before closing
  close       the judge found nothing left: closing the session loses nothing
  ready       nothing asked yet (or just cleared): free for the next task

The judge is a short Haiku call through your own `claude` CLI, run once per finished
turn. Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- paths & config

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
DATA_DIR = CLAUDE_DIR / "cockpit"
STATE_DIR = DATA_DIR / "state"
LOG_FILE = DATA_DIR / "cockpit.log"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "judge": True,  # False: no model calls, finished sessions land in "done"
    "judge_model": "haiku",
    "language": "English",  # language of headlines and follow-ups
}

HOOK_EVENTS = ("Stop", "StopFailure", "Notification")
HOOK_PATTERN = re.compile(r"cockpit\.py['\"]?\s+hook\b")

GROUPS = ("needs_you", "working", "followups", "done", "close", "ready")
VERDICTS = ("needs_you", "followups", "done", "close")
SUBAGENT_TOOLS = {"Agent", "Task"}
INTERRUPT_MARKERS = ("[Request interrupted by user", "[Request cancelled")

TAIL_BYTES = 800_000  # transcript tail read for the live state
HEAD_BYTES = 200_000  # transcript head read for the first prompt
JUDGE_HEAD_BYTES, JUDGE_TAIL_BYTES = 200_000, 2_500_000
JUDGE_TIMEOUT = 120
MAX_CHECKED = 30  # ticked follow-ups remembered per session
STALE_LOCK = 180  # a judge still marked running after this many seconds is dead
RETRY_AFTER = 120  # seconds before retrying a failed judge
SETTLE = 2.0  # serve waits this long after the last transcript write before judging
TICK = 1.5
TERMINAL_REFRESH = 4.0
HEARTBEAT = 10.0

EXTRA_PATH = [
    Path("~/.local/bin").expanduser(),
    Path("~/.claude/local").expanduser(),
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
]


def config():
    try:
        user = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        user = {}
    return {**DEFAULT_CONFIG, **(user if isinstance(user, dict) else {})}


# ---------------------------------------------------------------------- helpers


def log(msg):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("")
        with LOG_FILE.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def clip(text, n):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def no_dashes(text):
    return re.sub(r"\s*[—–]\s*", ": ", str(text or "")).strip()


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def utc_epoch(stamp):
    """'2026-10-29T13:51:48.123Z' -> epoch seconds."""
    if not stamp:
        return None
    try:
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def file_size(path):
    try:
        return os.path.getsize(path)
    except (OSError, TypeError):
        return None


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def find_executable(name):
    path = os.pathsep.join([os.environ.get("PATH", ""), *map(str, EXTRA_PATH)])
    return shutil.which(name, path=path)


# ------------------------------------------------------------------- state files


def safe_id(value):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:64]


def state_file(sid):
    return STATE_DIR / f"{safe_id(sid)}.json"


def lock_file(sid):
    return STATE_DIR / f"{safe_id(sid)}.lock"


def load_state(sid):
    try:
        return json.loads(state_file(sid).read_text())
    except (OSError, ValueError):
        return {}


def save_state(sid, state):
    write_json_atomic(state_file(sid), state)


def set_checked(sid, text, done):
    """Tick or untick one follow-up. Ticks are kept by text, so they survive a new verdict."""
    text = text.strip()
    if not safe_id(sid) or not text:
        raise RuntimeError("Need a session id and the follow-up text")
    state = load_state(sid)
    checked = [t for t in state.get("checked") or [] if t != text]
    if done:
        checked.append(text)
    state["checked"] = checked[-MAX_CHECKED:]
    save_state(sid, state)


# ------------------------------------------------------------------- transcripts


def read_tail(path, size):
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as f:
            if total <= size:
                return f.read()
            f.seek(total - size)
            data = f.read()
    except OSError:
        return b""
    return data.split(b"\n", 1)[1] if b"\n" in data else b""


def read_head_and_tail(path, head, tail):
    total = os.path.getsize(path)
    with open(path, "rb") as f:
        if total <= head + tail:
            return f.read().splitlines()
        first = f.read(head).splitlines()[:-1]
        f.seek(total - tail)
        return first + f.read().splitlines()[1:]


def entries(lines):
    """Main-thread user and assistant entries of a transcript."""
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") in ("user", "assistant") and not entry.get("isSidechain"):
            yield entry


def blocks(entry):
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def prompt_text(entry):
    """The text of a prompt the user typed, or None for tool results, reminders and the like."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    parts = blocks(entry)
    if any(b.get("type") == "tool_result" for b in parts):
        return None
    text = "\n".join(b.get("text", "") for b in parts if b.get("type") == "text").strip()
    if not text:
        return None
    command = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if command:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return f"{command.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    if text.lstrip().startswith("<") or any(m in text for m in INTERRUPT_MARKERS):
        return None
    return text


def is_interrupt(entry):
    if entry.get("type") != "user":
        return False
    text = " ".join(b.get("text", "") for b in blocks(entry) if b.get("type") == "text")
    return any(m in text for m in INTERRUPT_MARKERS)


def describe_tool(name, args):
    """A few words on what a tool call does, for the "In progress" line."""
    args = args or {}

    def base(p):
        return os.path.basename(str(p or "")) or "a file"

    if name == "Bash":
        return clip(args.get("description") or args.get("command"), 80)
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"Editing {base(args.get('file_path') or args.get('notebook_path'))}"
    if name == "Read":
        return f"Reading {base(args.get('file_path'))}"
    if name in ("Grep", "Glob"):
        return "Searching the code"
    if name in SUBAGENT_TOOLS:
        return clip(f"Subagent: {args.get('description') or ''}", 80)
    if name == "WebSearch":
        return clip(f"Searching the web: {args.get('query') or ''}", 80)
    if name == "WebFetch":
        return "Reading a web page"
    if name == "AskUserQuestion":
        return "Asking you a question"
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1].replace("claude_ai_", "").replace("_", " ") if len(parts) > 2 else ""
        return clip(f"{server}: {parts[-1].replace('_', ' ')}", 80)
    return name or "Working"


def scan(path):
    """Live facts from the end of a transcript."""
    facts = {
        "turn_key": None,  # uuid of the last assistant message: one verdict per value
        "turn_start": None,  # when the last prompt was sent
        "last_prompt": None,
        "last_ts": None,
        "activity": None,
        "pending": [],  # tool calls without a result since the last prompt
        "interrupted": False,
    }
    pending = {}
    for entry in entries(read_tail(path, TAIL_BYTES).splitlines()):
        ts = utc_epoch(entry.get("timestamp"))
        if ts:
            facts["last_ts"] = ts
        if entry["type"] == "user":
            prompt = prompt_text(entry)
            if prompt is not None:
                pending = {}
                facts.update(turn_start=ts, last_prompt=prompt, interrupted=False, activity=None)
                continue
            if is_interrupt(entry):
                pending = {}
                facts["interrupted"] = True
            for b in blocks(entry):
                if b.get("type") == "tool_result":
                    pending.pop(b.get("tool_use_id"), None)
            continue
        facts["turn_key"] = entry.get("uuid") or facts["turn_key"]
        facts["interrupted"] = False
        for b in blocks(entry):
            kind = b.get("type")
            if kind == "tool_use":
                name = b.get("name") or ""
                pending[b.get("id")] = {"id": b.get("id"), "name": name, "input": b.get("input") or {}, "ts": ts}
                facts["activity"] = describe_tool(name, b.get("input"))
            elif kind == "text" and b.get("text", "").strip():
                facts["activity"] = "Writing"
            elif kind == "thinking":
                facts["activity"] = "Thinking"
    facts["pending"] = list(pending.values())
    return facts


def first_prompt(path):
    """The session's first typed prompt (slash commands aside), from the head of its transcript."""
    try:
        with open(path, "rb") as f:
            lines = f.read(HEAD_BYTES).splitlines()
    except OSError:
        return None
    for entry in entries(lines):
        prompt = prompt_text(entry)
        if prompt and not prompt.startswith("/"):
            text = clip(re.sub(r"\[(Image|Pasted text) #\d+[^\]]*\]", " ", prompt), 80)
            if text:
                return text[0].upper() + text[1:]
    return None


def transcript_path(sid, cwd=""):
    guess = PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", cwd or "") / f"{sid}.jsonl"
    if guess.exists():
        return guess
    return next(PROJECTS_DIR.glob(f"*/{sid}.jsonl"), None)


def subagent_bytes(path, sid):
    """Total size of a session's subagent transcripts: grows as soon as a subagent moves on."""
    folder = Path(path).parent / sid / "subagents"
    return sum(file_size(p) or 0 for p in folder.glob("*.jsonl"))


# ------------------------------------------------------------------------ judge

JUDGE_PROMPT = """You review one of a developer's Claude Code sessions after a turn ended, for a cockpit that lists many sessions side by side. Tell the developer where this session stands. Reply with JSON only:
{"verdict": "...", "headline": "...", "ask": "...", "followups": [{"who": "you", "text": "..."}]}

verdict, exactly one of:
- "needs_you": Claude's last message asks the developer something and cannot go on without the answer: a question, a choice between options, an approval, a value or credential, a test result or feedback. Also when the turn was interrupted or failed mid-task and nothing says what to do next.
- "followups": Claude finished what was asked, but concrete work remains on this topic: steps the developer must take (commit, push, merge, deploy, run a command, test, check something later), next steps Claude said it would do or offered, caveats that need action, tickets still to work on.
- "done": Claude finished and considers the task done. Nothing concrete remains except the developer looking at or trying the result.
- "close": the work is complete and handed over: nothing to review, try, merge, deploy or follow up, so closing the session loses nothing (a question fully answered, a ticket filed, a report delivered, changes merged or pushed and verified). When in doubt between "close" and another verdict, do not pick "close".

headline: one sentence of at most 100 characters saying what this session achieved or where it stands. Be specific: name the feature, ticket, file or metric. Plain words, no filler like "Claude has".
ask: for "needs_you" only, the question or decision waiting on the developer, at most 160 characters. Otherwise "".
followups: for "followups" (optionally "done"), up to 5 concrete items of at most 100 characters each. who is "you" when the developer must act, "claude" when Claude said it would do it or offered to. Empty list for "close". Follow-ups the developer already ticked off are done: leave them out, and if nothing else remains, the verdict is not "followups".

Write the headline, ask and followups in {language}. Never use the characters "—" or "–"."""


def judge_context(path, name, cwd, checked=()):
    prompts, replies, actions = [], [], []
    interrupted = False
    for entry in entries(read_head_and_tail(path, JUDGE_HEAD_BYTES, JUDGE_TAIL_BYTES)):
        if entry["type"] == "user":
            prompt = prompt_text(entry)
            if prompt is not None:
                prompts.append(prompt)
                interrupted = False
            elif is_interrupt(entry):
                interrupted = True
            continue
        for b in blocks(entry):
            if b.get("type") == "text" and b.get("text", "").strip():
                replies.append(b["text"].strip())
                interrupted = False
            elif b.get("type") == "tool_use":
                actions.append(describe_tool(b.get("name") or "", b.get("input")))
    if not prompts and not replies:
        return None
    lines = [
        f"Session name: {name or '(none)'}",
        f"Working directory: {str(cwd).replace(str(Path.home()), '~')}",
        f"Now: {time.strftime('%Y-%m-%d %H:%M')}",
    ]
    if len(prompts) > 6:
        lines.append(f"First request of the session: {clip(prompts[0], 500)}")
    lines.append("Most recent developer messages (oldest first):")
    lines += [f"- {clip(p, 700)}" for p in prompts[-6:]]
    if actions:
        lines.append("Recent actions: " + "; ".join(clip(a, 70) for a in actions[-15:]))
    if replies:
        lines.append("Earlier Claude replies (oldest first):")
        lines += [f"- {clip(r, 700)}" for r in replies[-4:-1]]
        lines.append("Claude's LAST message (judge mainly on this):")
        lines.append(replies[-1][-6000:])
    if interrupted:
        lines.append("The developer interrupted the last turn before it finished.")
    if checked:
        lines.append("Follow-ups the developer already ticked off as done:")
        lines += [f"- {clip(t, 140)}" for t in checked[-10:]]
    return "\n".join(lines)


def ask_model(context, cfg):
    claude = find_executable("claude")
    if not claude:
        raise RuntimeError("claude CLI not found")
    result = subprocess.run(
        [
            claude,
            "-p",
            "--model",
            str(cfg["judge_model"]),
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--settings",
            '{"disableAllHooks": true}',
            "--tools",
            "",
            "--disable-slash-commands",
            "--system-prompt",
            JUDGE_PROMPT.replace("{language}", str(cfg["language"])),
        ],
        input=context,
        capture_output=True,
        text=True,
        timeout=JUDGE_TIMEOUT,
        cwd=DATA_DIR,
        env={**os.environ, "CLAUDE_COCKPIT_CHILD": "1"},
    )
    match = re.search(r"\{.*\}", result.stdout, re.S)
    if result.returncode != 0 or not match:
        raise RuntimeError(f"claude -p exit {result.returncode}: {(result.stdout + result.stderr)[-300:]!r}")
    return json.loads(match.group(0))


def clean_verdict(answer):
    verdict = answer.get("verdict")
    if verdict not in VERDICTS:
        verdict = "done"
    followups = []
    for item in answer.get("followups") or []:
        if isinstance(item, str):
            item = {"who": "you", "text": item}
        if isinstance(item, dict) and str(item.get("text", "")).strip():
            who = "claude" if item.get("who") == "claude" else "you"
            followups.append({"who": who, "text": clip(no_dashes(item["text"]), 140)})
    return {
        "verdict": verdict,
        "headline": clip(no_dashes(answer.get("headline")), 140),
        "ask": clip(no_dashes(answer.get("ask")), 220) if verdict == "needs_you" else "",
        "followups": [] if verdict == "close" else followups[:5],
    }


def take_lock(sid, turn_key):
    """One judge per session at a time: the hook and serve may both try."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = lock_file(sid)
    try:
        if time.time() - lock.stat().st_mtime > STALE_LOCK:
            lock.unlink()
    except OSError:
        pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(turn_key).encode())
    os.close(fd)
    return True


def release_lock(sid):
    try:
        lock_file(sid).unlink()
    except OSError:
        pass


def judge_running(sid):
    try:
        return time.time() - lock_file(sid).stat().st_mtime <= STALE_LOCK
    except OSError:
        return False


def judge_due(sid, turn_key):
    if not turn_key or not config()["judge"]:
        return False
    state = load_state(sid)
    if state.get("turn_key") == turn_key:
        return False
    if state.get("failed_key") == turn_key and time.time() - state.get("failed_at", 0) < RETRY_AFTER:
        return False
    return not judge_running(sid)


def spawn_judge(sid, path, name, cwd, turn_key):
    """Run the judge in a detached process so neither the hook nor serve waits on it."""
    if not take_lock(sid, turn_key):
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as err:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_judge", sid, str(path), name or "", cwd or "", turn_key],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
            cwd=DATA_DIR,
        )


def run_judge(sid, path, name, cwd, turn_key):
    started = time.time()
    try:
        time.sleep(1.0)  # let the final message land in the transcript
        turn_key = scan(path)["turn_key"] or turn_key
        context = judge_context(path, name, cwd, load_state(sid).get("checked") or [])
        if not context:
            return
        verdict = clean_verdict(ask_model(context, config()))
        state = load_state(sid)
        state.update(verdict, turn_key=turn_key, judged_at=time.time())
        state.pop("failed_key", None)
        save_state(sid, state)
        log(f"{sid[:8]} {verdict['verdict']} in {time.time() - started:.1f}s")
    except Exception as exc:  # noqa: BLE001 - a failed judge must never surface in a session
        state = load_state(sid)
        state.update(failed_key=turn_key, failed_at=time.time())
        save_state(sid, state)
        log(f"{sid[:8]} judge failed: {exc!r}")
    finally:
        release_lock(sid)


# ------------------------------------------------------------------------- hook


def session_name(sid):
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if s.get("sessionId") == sid:
            return s.get("name") or ""
    return ""


def hook():
    """Stop / StopFailure / Notification hook. Never blocks: the judge runs detached."""
    if os.environ.get("CLAUDE_COCKPIT_CHILD"):
        return
    if not os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli").startswith("cli"):
        return  # headless `claude -p` runs and SDK sessions
    data = json.load(sys.stdin)
    sid, event, path = data.get("session_id"), data.get("hook_event_name"), data.get("transcript_path")
    if not sid or not path or not os.path.exists(path):
        return
    if event == "Notification":
        kind = data.get("notification_type") or ""
        message = data.get("message") or ""
        if kind == "permission_prompt" or (not kind and re.search(r"permission|approve", message, re.I)):
            pending = scan(path)["pending"]
            tool = pending[-1] if pending else None
            state = load_state(sid)
            state["attention"] = {
                "kind": "permission",
                "tool_use_id": tool["id"] if tool else None,
                "tool": tool["name"] if tool else "",
                "text": describe_tool(tool["name"], tool["input"]) if tool else "",
                "size": file_size(path),
                "subagent_bytes": subagent_bytes(path, sid),
                "at": time.time(),
            }
            save_state(sid, state)
            return
        if kind not in ("idle_prompt", ""):
            if kind != "auth_success":
                state = load_state(sid)
                state["attention"] = {"kind": "notification", "text": clip(message, 160), "size": file_size(path), "at": time.time()}
                save_state(sid, state)
            return
    turn_key = scan(path)["turn_key"]
    if judge_due(sid, turn_key):
        spawn_judge(sid, path, session_name(sid), data.get("cwd") or "", turn_key)


# -------------------------------------------------------------------- terminals


NOT_ALLOWED = "Not allowed to control Ghostty. Turn it on in System Settings > Privacy & Security > Automation."


def osascript(script, *args, timeout=10):
    result = subprocess.run(["osascript", "-e", script, *map(str, args)], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        if "-1743" in result.stderr:  # errAEEventNotPermitted: the Automation permission is off
            raise RuntimeError(NOT_ALLOWED)
        raise RuntimeError(result.stderr.strip()[-300:] or f"osascript exit {result.returncode}")
    return result.stdout.strip()


class Ghostty:
    """Ghostty through its AppleScript dictionary (Ghostty 1.3 and later)."""

    BUNDLE = "com.mitchellh.ghostty"

    LIST = """
set sep to character id 9
if application id "com.mitchellh.ghostty" is running then
  tell application id "com.mitchellh.ghostty"
    set out to ""
    set wi to 0
    repeat with w in windows
      set wi to wi + 1
      repeat with t in tabs of w
        set n to count of terminals of t
        repeat with term in terminals of t
          set out to out & (pid of term) & sep & (tty of term) & sep & (index of t) & sep & wi & sep & (id of t) & sep & (id of term) & sep & n & linefeed
        end repeat
      end repeat
    end repeat
    return out
  end tell
end if
return ""
"""

    FOCUS = """
on run argv
  tell application id "com.mitchellh.ghostty"
    repeat with w in windows
      repeat with t in tabs of w
        if id of t is item 1 of argv then
          select tab t
          repeat with term in terminals of t
            if id of term is item 2 of argv then focus term
          end repeat
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""

    TYPE = """
on run argv
  tell application id "com.mitchellh.ghostty"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with term in terminals of t
          if id of term is item 1 of argv then
            input text (item 2 of argv) to term
            delay 0.4
            send key "enter" to term
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""

    # Characters one by one, as if typed. A pasted "/clear" (input text) opens the command
    # menu and Enter then completes it a second time ("/clear/clear" sent as a message).
    # send key presses physical keys, so a French AZERTY layout turned "/clear" into
    # "!cleqr" ("!" switches Claude Code to bash mode). The text: action writes characters.
    CHARS = """
on run argv
  tell application id "com.mitchellh.ghostty"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with term in terminals of t
          if id of term is item 1 of argv then
            repeat with c in (items 2 thru -1 of argv)
              perform action ("text:" & (c as text)) on term
            end repeat
            delay 0.5
            send key "enter" to term
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""

    FOCUSED = """
if application id "com.mitchellh.ghostty" is running then
  tell application id "com.mitchellh.ghostty"
    if frontmost then return pid of focused terminal of selected tab of front window
  end tell
end if
return ""
"""

    CLOSE = """
on run argv
  tell application id "com.mitchellh.ghostty"
    repeat with w in windows
      repeat with t in tabs of w
        if id of t is item 1 of argv then
          if item 3 of argv is "tab" then
            close tab t
          else
            repeat with term in terminals of t
              if id of term is item 2 of argv then close term
            end repeat
          end if
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""

    def terminals(self):
        out = []
        for line in osascript(self.LIST).splitlines():
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            try:
                pid, tab, window, count = int(parts[0]), int(parts[2]), int(parts[3]), int(parts[6])
            except ValueError:
                continue
            out.append({"pid": pid, "tty": parts[1], "tab": tab, "window": window, "tab_id": parts[4], "term_id": parts[5], "terms": count})
        return out

    def focus(self, t):
        if osascript(self.FOCUS, t["tab_id"], t["term_id"]) != "ok":
            raise RuntimeError("Tab not found")

    def type(self, t, text):
        if osascript(self.TYPE, t["term_id"], text) != "ok":
            raise RuntimeError("Tab not found")

    def command(self, t, command):
        """Run a slash command such as /clear by typing it."""
        # The text: action reads Zig string escapes, so a backslash is doubled.
        chars = [c.replace("\\", "\\\\") for c in command]
        if osascript(self.CHARS, t["term_id"], *chars) != "ok":
            raise RuntimeError("Tab not found")

    def close(self, t):
        osascript(self.CLOSE, t["tab_id"], t["term_id"], "tab" if t["terms"] == 1 else "term")

    def focused_pid(self):
        """The foreground PID of the terminal in front of you, or None when Ghostty is not in front."""
        out = osascript(self.FOCUSED)
        return int(out) if out.isdigit() else None


TERMINALS = [Ghostty()]


def ttys_of(pids):
    """{pid: '/dev/ttys003'} for the given PIDs."""
    pids = [str(p) for p in pids if p]
    if not pids:
        return {}
    result = subprocess.run(["ps", "-o", "pid=,tty=", "-p", ",".join(pids)], capture_output=True, text=True)
    out = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] not in ("??", "-"):
            out[int(parts[0])] = "/dev/" + parts[1] if not parts[1].startswith("/dev/") else parts[1]
    return out


def map_terminals(pids):
    """{pid: terminal} for Claude PIDs: by foreground PID first, then by tty."""
    found = []
    for adapter in TERMINALS:
        try:
            found += [{**t, "adapter": adapter} for t in adapter.terminals()]
        except Exception as exc:  # noqa: BLE001 - a terminal that does not answer just maps nothing
            log(f"{type(adapter).__name__} listing failed: {exc}")
    by_pid = {t["pid"]: t for t in found}
    by_tty = {t["tty"]: t for t in found if t.get("tty")}
    out = {pid: by_pid[pid] for pid in pids if pid in by_pid}
    missing = [pid for pid in pids if pid not in out]
    for pid, tty in ttys_of(missing).items():
        if tty in by_tty:
            out[pid] = by_tty[tty]
    return out


# ---------------------------------------------------------------------- actions


def session_for_pid(pid):
    try:
        session = json.loads((SESSIONS_DIR / f"{int(pid)}.json").read_text())
    except (OSError, ValueError):
        raise RuntimeError("No Claude session with that PID") from None
    if session.get("pid") != int(pid) or not alive(pid):
        raise RuntimeError("That session has ended")
    return session


def terminal_for_pid(pid):
    t = map_terminals([int(pid)]).get(int(pid))
    if not t:
        raise RuntimeError("No terminal tab found for this session")
    return t


def focused():
    """The PID in the terminal tab you are looking at, if a terminal is in front."""
    for adapter in TERMINALS:
        try:
            pid = adapter.focused_pid()
        except Exception as exc:  # noqa: BLE001 - unknown means not looking
            log(f"{type(adapter).__name__} focus check failed: {exc}")
            continue
        if pid:
            return pid
    return None


def focus(pid):
    session_for_pid(pid)
    t = terminal_for_pid(pid)
    t["adapter"].focus(t)


def reply(pid, text):
    text = text.strip()
    if not text:
        raise RuntimeError("Empty message")
    if session_for_pid(pid).get("status") != "idle":
        raise RuntimeError("The session is busy")
    t = terminal_for_pid(pid)
    t["adapter"].type(t, text)


def clear(pid):
    """Start a fresh conversation in the same tab (Claude Code's /clear)."""
    if session_for_pid(pid).get("status") != "idle":
        raise RuntimeError("The session is busy")
    t = terminal_for_pid(pid)
    t["adapter"].command(t, "/clear")


def close(pid):
    pid = int(pid)
    if session_for_pid(pid).get("status") != "idle":
        raise RuntimeError("The session is busy")
    t = map_terminals([pid]).get(pid)
    os.kill(pid, signal.SIGTERM)  # this exact process only
    deadline = time.time() + 10
    while alive(pid) and time.time() < deadline:
        time.sleep(0.25)
    if alive(pid):
        raise RuntimeError("Claude did not exit; the tab stays open")
    if t:
        time.sleep(0.3)
        t["adapter"].close(t)


# --------------------------------------------------------------------- snapshot


def live_sessions():
    out = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        # Since Claude Code 2.1.314, `claude -p` runs (like the judge's) are listed too, as sdk-cli.
        headless = str(s.get("entrypoint") or "").startswith("sdk")
        if s.get("kind") == "interactive" and not headless and s.get("sessionId") and alive(s.get("pid")):
            out.append(s)
    return out


def picker_summary(tool):
    questions = (tool.get("input") or {}).get("questions") or []
    if not questions:
        return None
    q = questions[0]
    return {
        "header": clip(q.get("header"), 30),
        "question": clip(q.get("question"), 300),
        "options": [clip(o.get("label"), 60) for o in q.get("options") or [] if isinstance(o, dict)],
        "multi": bool(q.get("multiSelect")),
        "more": len(questions) - 1,
    }


def attention_live(attention, facts, path, sid, status):
    """Is the permission prompt or notification recorded by the hook still on screen?"""
    if not attention:
        return False
    if attention.get("tool_use_id"):
        tool = next((p for p in facts["pending"] if p["id"] == attention["tool_use_id"]), None)
        if not tool:
            return False
        if tool["name"] in SUBAGENT_TOOLS:  # the prompt came from inside a subagent
            return subagent_bytes(path, sid) == attention.get("subagent_bytes")
        return True
    return status != "idle" and file_size(path) == attention.get("size")


def attention_text(attention, facts):
    if attention.get("kind") == "notification":
        return attention.get("text") or "Waiting for you"
    # A tool call that waits for approval usually reaches the transcript only once answered.
    pending = facts["pending"]
    tool = next((p for p in pending if p["id"] == attention.get("tool_use_id")), None) or (pending[-1] if pending else None)
    return f"Permission: {describe_tool(tool['name'], tool['input'])}" if tool else "Waiting for your permission"


class Snapshotter:
    """Builds snapshots, caching transcript scans until the file changes."""

    def __init__(self, judge=True):
        self.judge = judge
        self.paths = {}
        self.scans = {}
        self.first_prompts = {}
        self.terminals = {}
        self.terminals_at = 0.0
        self.terminals_for = set()

    def refresh_terminals(self, pids):
        """Ask the terminals at most every few seconds, or right away when sessions come or go."""
        if time.time() - self.terminals_at >= TERMINAL_REFRESH or set(pids) != self.terminals_for:
            self.terminals = map_terminals(pids)
            self.terminals_at = time.time()
            self.terminals_for = set(pids)

    def facts(self, sid, path):
        empty = {
            "turn_key": None,
            "turn_start": None,
            "last_prompt": None,
            "last_ts": None,
            "activity": None,
            "pending": [],
            "interrupted": False,
        }
        if not path:
            return empty, None
        try:
            st = os.stat(path)
        except OSError:
            return empty, None
        key = (st.st_size, st.st_mtime)
        cached = self.scans.get(sid)
        if not cached or cached[0] != key:
            cached = (key, scan(path))
            self.scans[sid] = cached
        return cached[1], st.st_mtime

    def name(self, s, path, folder):
        """Claude Code's placeholder name ("folder-26") says nothing: show the first prompt instead."""
        name = s.get("name") or ""
        if name and s.get("nameSource") != "derived":
            return name
        sid = s["sessionId"]
        if sid not in self.first_prompts and path:
            prompt = first_prompt(path)
            if prompt:
                self.first_prompts[sid] = prompt
        return self.first_prompts.get(sid) or name or folder

    def row(self, s):
        sid, pid, cwd = s["sessionId"], s["pid"], s.get("cwd") or ""
        if sid not in self.paths or not self.paths[sid]:
            self.paths[sid] = transcript_path(sid, cwd)
        path = self.paths[sid]
        facts, mtime = self.facts(sid, path)
        state = load_state(sid)
        status = s.get("status") or "idle"
        term = self.terminals.get(pid) or {}
        folder = os.path.basename(cwd.rstrip("/")) or "~"
        row = {
            "pid": pid,
            "sid": sid,
            "name": self.name(s, path, folder),
            "folder": folder,
            "cwd": cwd,
            "tab": term.get("tab"),
            "window": term.get("window"),
            "status": status,
            "since": facts["last_ts"] or (s.get("statusUpdatedAt") or 0) / 1000 or None,
            "last_prompt": clip(facts["last_prompt"], 200) if facts["last_prompt"] else None,
            "group": "working",
            "kind": None,
            "judging": False,
            "question": None,
            "ask": None,
            "activity": None,
            "headline": None,
            "followups": [],
        }
        picker = next((p for p in reversed(facts["pending"]) if p["name"] == "AskUserQuestion"), None)
        plan = next((p for p in reversed(facts["pending"]) if p["name"] == "ExitPlanMode"), None)
        attention = state.get("attention")

        if picker:
            row.update(group="needs_you", kind="picker", question=picker_summary(picker), since=picker["ts"] or row["since"])
        elif plan:
            row.update(group="needs_you", kind="plan", ask="A plan is ready for your approval", since=plan["ts"] or row["since"])
        elif status == "waiting" or attention_live(attention, facts, path, sid, status):
            # Claude Code marks a session "waiting" while a permission prompt is on screen.
            attention = attention if attention_live(attention, facts, path, sid, status) else {"kind": "permission"}
            row.update(group="needs_you", kind=attention["kind"], ask=attention_text(attention, facts))
            if attention.get("at"):
                row["since"] = attention["at"]
        elif status != "idle":
            row.update(activity=facts["activity"] or "Thinking", since=facts["turn_start"] or row["since"])
        elif not facts["turn_key"]:
            row.update(group="ready")  # nothing asked yet, or just cleared
        elif state.get("turn_key") == facts["turn_key"]:
            verdict = state.get("verdict") or "done"
            checked = set(state.get("checked") or [])
            followups = [{**f, "done": f["text"] in checked} for f in state.get("followups") or []]
            if verdict == "followups" and followups and all(f["done"] for f in followups):
                verdict = "close"  # every item ticked: nothing left
            row.update(group=verdict, headline=state.get("headline"), followups=followups, ask=state.get("ask") or None)
            if verdict == "needs_you":
                row["kind"] = "interrupted" if facts["interrupted"] else "asked"
        elif not config()["judge"]:
            row.update(group="done", headline=clip(facts["last_prompt"], 140) if facts["last_prompt"] else None)
        else:
            row.update(judging=True, activity="Finished, judging")
            settled = mtime and time.time() - mtime >= SETTLE
            if self.judge and settled and judge_due(sid, facts["turn_key"]):
                spawn_judge(sid, path, row["name"], cwd, facts["turn_key"])

        idle = status == "idle"
        row["can_reply"] = idle and bool(term) and replyable(row)
        row["can_clear"] = idle and bool(term) and row["group"] in ("done", "close")
        row["can_close"] = idle and row["group"] in ("close", "ready")
        return row

    def snapshot(self):
        sessions = live_sessions()
        self.refresh_terminals([s["pid"] for s in sessions])
        rows = [self.row(s) for s in sessions]
        rows.sort(key=lambda r: -(r["since"] or 0))
        return {"at": time.time(), "sessions": rows}


def replyable(row):
    """Typing into the session makes sense: Claude asked something, or there is a checklist to report on."""
    if row["kind"] in ("picker", "plan", "permission"):
        return False
    return row["group"] in ("needs_you", "followups") or (bool(row["followups"]) and row["group"] in ("done", "close"))


def serve():
    """One JSON line per change (and a heartbeat), until stdout closes."""
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    snapshotter = Snapshotter(judge=True)
    last_body, last_sent = None, 0.0
    while True:
        try:
            snap = snapshotter.snapshot()
        except Exception as exc:  # noqa: BLE001 - keep serving
            log(f"snapshot failed: {exc!r}")
            time.sleep(TICK)
            continue
        body = json.dumps(snap["sessions"], sort_keys=True)
        if body != last_body or time.time() - last_sent >= HEARTBEAT:
            sys.stdout.write(json.dumps(snap, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            last_body, last_sent = body, time.time()
        time.sleep(TICK)


def demo_snapshot():
    """Sample sessions for screenshots and UI work."""
    now = time.time()

    def row(name, folder, group, ago, tab, **extra):
        r = {
            "pid": 40000 + tab,
            "sid": f"demo-{tab}",
            "name": name,
            "folder": folder,
            "cwd": f"~/code/{folder}",
            "tab": tab,
            "window": 1,
            "status": "busy" if group == "working" else "idle",
            "since": now - ago,
            "last_prompt": None,
            "group": group,
            "kind": None,
            "judging": False,
            "question": None,
            "ask": None,
            "activity": None,
            "headline": None,
            "followups": [],
            "can_reply": False,
            "can_clear": False,
            "can_close": False,
        }
        r.update(extra)
        idle = r["status"] == "idle"
        r["can_reply"] = idle and replyable(r)
        r["can_clear"] = idle and group in ("done", "close")
        r["can_close"] = idle and group in ("close", "ready")
        return r

    rows = [
        row(
            "Checkout page redesign",
            "storefront",
            "needs_you",
            420,
            2,
            kind="picker",
            question={
                "header": "Layout",
                "question": "Keep the order summary in a sticky sidebar, or move it above the payment form on mobile?",
                "options": ["Sticky sidebar (Recommended)", "Above the form on mobile", "Collapsible summary"],
                "multi": False,
                "more": 0,
            },
        ),
        row(
            "Postgres 17 upgrade plan",
            "infra",
            "needs_you",
            1500,
            5,
            kind="asked",
            ask="Can I run the upgrade rehearsal against the staging snapshot from last night?",
            headline="Upgrade runbook drafted; rehearsal on staging is the next step",
        ),
        row("Flaky login end-to-end test", "web", "working", 95, 1, activity="Run the login test 20 times in a row"),
        row("Rate limiter for the public API", "api", "working", 600, 3, activity="Editing limiter.go"),
        row(
            "Search indexing backfill",
            "api",
            "followups",
            3100,
            4,
            headline="Backfill script indexed 1.2M documents; 312 failed on oversized payloads",
            followups=[
                {"who": "you", "text": "Review and merge PR #482 (backfill script and retry queue)", "done": True},
                {"who": "you", "text": "Raise the payload size limit to 2 MB in the indexer config", "done": False},
                {"who": "claude", "text": "Re-run the 312 failed documents once the size limit is raised", "done": False},
            ],
        ),
        row(
            "Docs: getting started guide",
            "docs",
            "done",
            5400,
            6,
            headline="Getting started guide rewritten with a 5 minute quickstart; preview builds locally",
        ),
        row(
            "Why is the nightly export slow?",
            "data",
            "close",
            9000,
            7,
            headline="Export slowed by a missing index on events.created_at; index added and merged",
        ),
        row("storefront", "storefront", "ready", 300, 8),
    ]
    return {"at": now, "sessions": rows}


# --------------------------------------------------------------------- settings


def hook_command():
    # Plain `python3` from the session's PATH: an absolute interpreter path (Homebrew's is
    # versioned) would break on the next Python upgrade.
    return f"python3 {shlex.quote(os.path.abspath(__file__))} hook"


def load_settings():
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except ValueError as exc:
        raise RuntimeError(f"{SETTINGS_FILE} is not valid JSON, leaving it alone: {exc}") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"{SETTINGS_FILE} is not a JSON object, leaving it alone")
    return data


def strip_our_hooks(settings):
    """Remove every Claude Cockpit hook entry; returns how many were removed."""
    removed = 0
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for group in hooks[event] or []:
            inner = [h for h in group.get("hooks", []) if not HOOK_PATTERN.search(str(h.get("command", "")))]
            removed += len(group.get("hooks", [])) - len(inner)
            if inner:
                kept.append({**group, "hooks": inner})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if "hooks" in settings and not hooks:
        del settings["hooks"]
    return removed


def hooks_status():
    try:
        settings = load_settings()
    except RuntimeError:
        return "missing"
    command = hook_command()
    commands = {
        event: [h.get("command") for g in (settings.get("hooks") or {}).get(event) or [] for h in g.get("hooks", [])]
        for event in HOOK_EVENTS
    }
    if all(command in commands[e] for e in HOOK_EVENTS):
        return "installed"
    if any(HOOK_PATTERN.search(str(c)) for cs in commands.values() for c in cs):
        return "outdated"
    return "missing"


def install_hooks():
    settings = load_settings()
    if SETTINGS_FILE.exists():
        shutil.copy2(SETTINGS_FILE, SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".bak-claude-cockpit"))
    strip_our_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]})
    write_json_atomic(SETTINGS_FILE, settings)
    return f"Hooks installed in {SETTINGS_FILE}"


def uninstall_hooks():
    settings = load_settings()
    removed = strip_our_hooks(settings)
    if removed:
        shutil.copy2(SETTINGS_FILE, SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".bak-claude-cockpit"))
        write_json_atomic(SETTINGS_FILE, settings)
    return f"Removed {removed} hook entr{'y' if removed == 1 else 'ies'} from {SETTINGS_FILE}"


# -------------------------------------------------------------------------- cli


def judge_now(prefix):
    session = next((s for s in live_sessions() if s["sessionId"].startswith(prefix)), None)
    if not session:
        raise RuntimeError("No open session with that id")
    sid = session["sessionId"]
    path = transcript_path(sid, session.get("cwd"))
    if not path:
        raise RuntimeError("Transcript not found")
    turn_key = scan(path)["turn_key"]
    if not take_lock(sid, turn_key):
        raise RuntimeError("A judge is already running for this session")
    run_judge(sid, path, session.get("name"), session.get("cwd"), turn_key)
    return load_state(sid)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cockpit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "hook", "install-hooks", "uninstall-hooks", "hooks-status", "demo", "focused"):
        sub.add_parser(name)
    sub.add_parser("snapshot").add_argument("--no-judge", action="store_true")
    sub.add_parser("judge").add_argument("session_id")
    for name in ("check", "uncheck"):
        sub.add_parser(name).add_argument("session_id")
    for name in ("focus", "reply", "clear", "close"):
        sub.add_parser(name).add_argument("pid", type=int)
    internal = sub.add_parser("_judge")
    internal.add_argument("args", nargs=5)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "hook":
            try:
                hook()
            except Exception as exc:  # noqa: BLE001 - a hook must never break the session
                log(f"hook error: {exc!r}")
        elif args.cmd == "serve":
            serve()
        elif args.cmd == "_judge":
            run_judge(*args.args)
        elif args.cmd == "snapshot":
            print(json.dumps(Snapshotter(judge=not args.no_judge).snapshot(), ensure_ascii=False, indent=1))
        elif args.cmd == "demo":
            print(json.dumps(demo_snapshot(), ensure_ascii=False, indent=1))
        elif args.cmd == "judge":
            print(json.dumps(judge_now(args.session_id), ensure_ascii=False, indent=1))
        elif args.cmd == "focused":
            print(focused() or "")
        elif args.cmd == "focus":
            focus(args.pid)
        elif args.cmd == "reply":
            reply(args.pid, sys.stdin.read())
        elif args.cmd == "clear":
            clear(args.pid)
        elif args.cmd == "close":
            close(args.pid)
        elif args.cmd in ("check", "uncheck"):
            set_checked(args.session_id, sys.stdin.read(), args.cmd == "check")
        elif args.cmd == "hooks-status":
            print(hooks_status())
        elif args.cmd == "install-hooks":
            print(install_hooks())
        elif args.cmd == "uninstall-hooks":
            print(uninstall_hooks())
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

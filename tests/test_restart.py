"""Unit tests for restart.py (restart Ghostty, tabs restored, Claude sessions resumed)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cockpit"))
import restart  # noqa: E402

import cockpit  # noqa: E402


class Flags(unittest.TestCase):
    def test_keeps_session_flags_and_drops_resume_and_prompt(self):
        args = ["--model", "opus", "--resume", "old-id", "--dangerously-skip-permissions", "--add-dir", "../a", "../b", "-c", "fix the bug"]
        self.assertEqual(restart.resume_flags(args), ["--model", "opus", "--dangerously-skip-permissions", "--add-dir", "../a", "../b"])

    def test_equals_forms_and_short_resume(self):
        self.assertEqual(restart.resume_flags(["-r", "x", "--permission-mode=plan", "--verbose"]), ["--permission-mode=plan", "--verbose"])

    def test_command_is_quoted(self):
        cmd = restart.resume_command("abc-123", ["--append-system-prompt", "be 'brief'"])
        self.assertEqual(cmd, "claude --append-system-prompt 'be '\"'\"'brief'\"'\"'' --resume abc-123")

    def test_env_keeps_only_claude_settings(self):
        env = {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_SUBAGENT_MODEL": "k3",
            "HOME": "/Users/x",
            "CLAUDECODE": "1",
            # Markers of the Claude session this was started from: carried, they turn off transcripts.
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CODE_SESSION_ID": "parent",
            "CLAUDE_CODE_MESSAGING_TOKEN": "t",
        }
        self.assertEqual(
            restart.resume_env(env),
            {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_SUBAGENT_MODEL": "k3"},
        )

    def test_resume_target(self):
        self.assertEqual(restart.resume_target(["--resume", "abc", "--model", "x"]), "abc")
        self.assertEqual(restart.resume_target(["-r", "abc"]), "abc")
        self.assertEqual(restart.resume_target(["--resume=abc"]), "abc")
        self.assertIsNone(restart.resume_target(["--resume", "--model", "x"]))  # the picker
        self.assertIsNone(restart.resume_target(["--model", "x"]))

    def test_prompt_after_resume(self):
        self.assertEqual(restart.resume_command("s", ["--model", "x"], "go on"), "claude --model x --resume s 'go on'")


class Environment(unittest.TestCase):
    def test_ghostty_gets_a_dock_environment(self):
        env = {
            "HOME": "/Users/x",
            "USER": "x",
            "PATH": "/opt/homebrew/bin:/usr/bin",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "TERM": "xterm-ghostty",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(restart.launch_env(), {"HOME": "/Users/x", "USER": "x", "PATH": restart.LAUNCH_PATH})

    def test_main_drops_claude_markers(self):
        env = {"PATH": os.environ.get("PATH", ""), "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CONFIG_DIR": "/c"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(cockpit, "hooks_status", lambda: "installed"):
            cockpit.main(["hooks-status"])
            self.assertNotIn("CLAUDECODE", os.environ)
            self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", os.environ)
            self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], "/c")


class ProcArgs(unittest.TestCase):
    def test_parse_buffer(self):
        data = (3).to_bytes(4, sys.byteorder) + b"/opt/x/claude\0\0\0\0claude\0--resume\0abc\0ANTHROPIC_API_KEY=\0A=b=c\0\0junk\0"
        argv, env = restart.parse_procargs(data)
        self.assertEqual(argv, ["claude", "--resume", "abc"])
        self.assertEqual(env, {"ANTHROPIC_API_KEY": "", "A": "b=c"})

    def test_reads_a_live_process(self):
        # macOS hides the environment of Apple's own programs (/bin/sleep), not of claude's.
        env = {**os.environ, "ANTHROPIC_BASE_URL": "https://example.test"}
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], env=env)
        try:
            time.sleep(0.3)
            argv, penv = restart.proc_args(p.pid)
            self.assertEqual(argv[1:], ["-c", "import time; time.sleep(5)"])
            if penv:
                self.assertEqual(penv.get("ANTHROPIC_BASE_URL"), "https://example.test")
            self.assertTrue(os.path.exists(restart.exe_path(p.pid)))
        finally:
            p.kill()
            p.wait()
        self.assertIs(restart.exe_path(p.pid), False)


class Stale(unittest.TestCase):
    TABLE = {
        1: (0, "Ss", "/sbin/launchd"),
        10: (1, "S", "/Applications/Ghostty.app/Contents/MacOS/ghostty"),
        11: (10, "Ss", "/usr/bin/login"),
        12: (11, "S", "-/bin/zsh"),
        13: (12, "S+", "claude"),
        14: (13, "Z", "jq"),
        15: (12, "S+", "node"),
        20: (1, "S", "/Applications/WebStorm.app/Contents/MacOS/webstorm"),
        21: (20, "S", "claude"),
    }

    def test_only_deleted_programs_under_ghostty(self):
        gone = {13, 14, 21, 10}
        with mock.patch.object(restart, "exe_path", lambda pid: None if pid in gone else "/x"):
            stale = restart.stale_processes(self.TABLE)
        self.assertEqual(sorted(stale), [(10, "Ghostty"), (13, "claude")])  # the zombie and WebStorm's claude are not counted
        self.assertEqual(restart.summarize([(1, "claude"), (2, "claude"), (3, "node")]), ["claude ×2", "node"])

    def test_ancestry(self):
        self.assertEqual(restart.ghostty_pid(self.TABLE), 10)
        self.assertIn(10, restart.ancestors(13, self.TABLE))
        self.assertNotIn(10, restart.ancestors(21, self.TABLE))


class Plan(unittest.TestCase):
    LAYOUT = [
        {"window": 1, "front": False, "tab": 1, "selected": False, "tty": "/dev/ttys001", "cwd": "/w/a"},
        {"window": 1, "front": False, "tab": 2, "selected": True, "tty": "/dev/ttys002", "cwd": "/w/b"},
        {"window": 1, "front": False, "tab": 2, "selected": True, "tty": "/dev/ttys003", "cwd": ""},
        {"window": 2, "front": True, "tab": 1, "selected": True, "tty": "/dev/ttys004", "cwd": '/w/c "q"'},
    ]
    SESSIONS = [
        {"pid": 101, "sessionId": "sid-1", "cwd": "/w/a-root", "status": "idle", "name": "One"},
        {"pid": 104, "sessionId": "sid-4", "cwd": '/w/c "q"', "status": "busy", "name": "Four"},
        {"pid": 999, "sessionId": "sid-9", "cwd": "/elsewhere", "status": "idle"},  # in another terminal
    ]
    TTYS = {101: "/dev/ttys001", 104: "/dev/ttys004", 999: "/dev/ttys099"}

    def args_of(self, pid):
        if pid == 104:
            return ["claude", "--model", "opus"], {
                "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                "ANTHROPIC_AUTH_TOKEN": "secret",
                "PATH": "/bin",
            }
        return ["claude", "--resume", "older"], {}

    def plan(self):
        return restart.make_plan(self.LAYOUT, self.SESSIONS, self.TTYS, self.args_of)

    def test_windows_tabs_splits_and_sessions(self):
        plan = self.plan()
        self.assertEqual([len(w["tabs"]) for w in plan], [2, 1])
        self.assertEqual([w["selected"] for w in plan], [2, 1])
        self.assertEqual([w["front"] for w in plan], [False, True])
        first = plan[0]["tabs"][0][0]
        self.assertEqual(first["cwd"], "/w/a-root")  # Claude resumes from the folder it started in
        self.assertEqual(first["resume"]["command"], "claude --resume sid-1")  # idle: nothing to continue
        split = plan[0]["tabs"][1]
        self.assertEqual([t["cwd"] for t in split], ["/w/b", os.path.expanduser("~")])
        self.assertIsNone(split[0]["resume"])
        other = plan[1]["tabs"][0][0]["resume"]
        # It was working: the restart stops its turn, so it is told to go on.
        self.assertEqual(other["command"], restart.resume_command("sid-4", ["--model", "opus"], restart.CONTINUE_PROMPT))
        self.assertTrue(other["command"].startswith("claude --model opus --resume sid-4 'Ghostty was restarted"))
        self.assertEqual(other["env"], {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api", "ANTHROPIC_AUTH_TOKEN": "secret"})
        self.assertEqual([r["sid"] for r in restart.resumed(plan)], ["sid-1", "sid-4"])

    def test_script(self):
        script = restart.build_script(self.plan())
        self.assertIn(
            'set w1 to new window with configuration {initial working directory:"/w/a-root", initial input:"claude --resume sid-1" & linefeed}',
            script,
        )
        self.assertIn('set t to new tab in w1 with configuration {initial working directory:"/w/b"}', script)
        self.assertIn("set term to split term direction right with configuration", script)
        self.assertIn("select tab (tab 2 of w1)", script)
        self.assertIn('initial working directory:"/w/c \\"q\\""', script)
        self.assertIn('environment variables:{"ANTHROPIC_AUTH_TOKEN=secret", "ANTHROPIC_BASE_URL=https://openrouter.ai/api"}', script)
        self.assertTrue(script.rstrip().endswith("activate window w2\nend tell"))

    def test_saved_plan_has_no_secrets(self):
        saved = restart.redacted(self.plan())
        self.assertNotIn("secret", json.dumps(saved))
        self.assertNotIn("environment variables", restart.build_script(saved))
        self.assertIn("claude --model opus --resume sid-4", restart.build_script(saved))


class Watching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(cockpit, "DATA_DIR", root / "cockpit"),
            mock.patch.object(cockpit, "CONFIG_FILE", root / "cockpit" / "config.json"),
            mock.patch.object(cockpit, "LOG_FILE", root / "cockpit" / "cockpit.log"),
            mock.patch.object(restart, "notify", lambda *a: self.notes.append(a)),
        ]
        for p in self.patches:
            p.start()
        (root / "cockpit").mkdir()
        self.notes, self.spawned = [], []
        self.stale, self.idle = [], 120.0
        self.watcher = restart.Watcher(
            spawn=lambda **kw: self.spawned.append(kw), scan=lambda: [(i, n) for i, n in enumerate(self.stale)], idle=lambda: self.idle
        )
        self.now = 1_000_000.0

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def tick(self, rows=(), after=6.0):
        self.now += after
        return self.watcher.tick(list(rows), now=self.now)

    def test_nothing_deleted_says_nothing(self):
        self.assertIsNone(self.tick())
        self.assertEqual(self.spawned, [])

    def test_restarts_once_settled_idle_and_away(self):
        self.stale = ["claude"]
        self.assertEqual(self.tick()["waiting"], "Checking")
        self.assertEqual(len(self.notes), 1)
        busy = [{"tab": 2, "status": "busy"}, {"tab": None, "status": "busy"}]  # the second runs in another terminal
        self.assertEqual(self.tick(busy, after=11)["waiting"], "Waiting for 1 session to finish")
        self.idle = 5.0
        self.assertIn("leave the keyboard", self.tick([{"tab": 2, "status": "idle"}])["waiting"])
        self.assertEqual(self.spawned, [])
        self.idle = 90.0
        status = self.tick([{"tab": 2, "status": "idle"}])
        self.assertEqual(self.spawned, [{"force": False, "auto": True}])
        self.assertTrue(status["restarting"])
        self.assertEqual(len(self.notes), 1)  # told once

    def test_pending_picker_blocks(self):
        self.stale = ["claude"]
        self.tick()
        status = self.tick([{"tab": 1, "status": "idle", "kind": "picker"}], after=11)
        self.assertEqual(status["waiting"], "Waiting for 1 session to finish")

    def test_other_programs_only_warn(self):
        self.stale = ["turbo", "node"]
        self.tick()
        status = self.tick([{"tab": 1, "status": "idle"}], after=60)
        self.assertEqual(self.spawned, [])
        self.assertEqual(status, {"stale": ["node", "turbo"], "auto": False, "waiting": None, "restarting": False})
        self.assertIn("Restart it", self.notes[0][1])

    def test_claude_among_others_restarts(self):
        self.stale = ["turbo", "claude"]
        self.tick()
        self.tick([{"tab": 1, "status": "idle"}], after=11)
        self.assertEqual(self.spawned, [{"force": False, "auto": True}])

    def test_auto_off_only_reports(self):
        cockpit.CONFIG_FILE.write_text(json.dumps({"auto_restart_ghostty": False}))
        self.stale = ["Ghostty"]
        self.tick()
        status = self.tick(after=30)
        self.assertEqual(self.spawned, [])
        self.assertEqual(status, {"stale": ["Ghostty"], "auto": False, "waiting": None, "restarting": False})

    def test_cooldown_after_a_restart(self):
        restart.save_state({"running": False, "started": self.now, "finished": self.now})
        self.stale = ["claude"]
        self.tick()
        self.assertIn("Restarted a few minutes ago", self.tick(after=30)["waiting"])
        self.assertEqual(self.spawned, [])

    def test_request_from_inside_ghostty(self):
        cockpit.write_json_atomic(cockpit.DATA_DIR / restart.REQUEST_NAME, {"at": time.time(), "force": False, "spare": [41, 42]})
        status = self.tick()
        self.assertEqual(self.spawned, [{"force": False, "auto": False, "spare": [41, 42]}])
        self.assertTrue(status["restarting"])
        self.assertFalse((cockpit.DATA_DIR / restart.REQUEST_NAME).exists())

    def test_forced_request_stays_forced(self):
        cockpit.write_json_atomic(cockpit.DATA_DIR / restart.REQUEST_NAME, {"at": time.time(), "force": True, "spare": [41]})
        self.tick()
        self.assertEqual(self.spawned, [{"force": True, "auto": False, "spare": [41]}])

    def test_old_request_is_ignored(self):
        cockpit.write_json_atomic(cockpit.DATA_DIR / restart.REQUEST_NAME, {"at": time.time() - 300})
        self.tick()
        self.assertEqual(self.spawned, [])


class BusyCheckTest(unittest.TestCase):
    """restart() checks for working sessions itself, right before ending them."""

    class Started(Exception):
        pass

    def setUp(self):
        self.notes, self.logs = [], []
        self.claude = []
        self.patches = [
            mock.patch.object(restart, "restart_running", lambda *a: False),
            mock.patch.object(restart, "process_table", lambda: {}),
            mock.patch.object(restart, "ghostty_pid", lambda table: 1),
            mock.patch.object(restart, "read_layout", lambda: [["tab"]]),
            mock.patch.object(restart, "unregistered_sessions", lambda known: []),
            mock.patch.object(restart, "make_plan", lambda *a: []),
            mock.patch.object(restart, "resumed", lambda plan: self.claude),
            mock.patch.object(cockpit, "live_sessions", lambda: []),
            mock.patch.object(cockpit, "ttys_of", lambda pids: {}),
            mock.patch.object(cockpit, "log", self.logs.append),
            mock.patch.object(restart, "notify", lambda *a: self.notes.append(a)),
            mock.patch.object(restart, "save_state", mock.Mock(side_effect=self.Started)),  # first step past the check
            mock.patch.dict(os.environ),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop(restart.SPARE_ENV, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_requested_restart_spares_only_the_asking_session(self):
        os.environ[restart.SPARE_ENV] = "41,42"
        self.claude = [{"pid": 42, "status": "busy", "name": "asker"}]
        with self.assertRaises(self.Started):
            restart.restart()
        self.claude.append({"pid": 77, "status": "busy", "name": "other work"})
        with self.assertRaisesRegex(RuntimeError, r"1 Claude session still working \(other work\)"):
            restart.restart()
        self.assertEqual(self.notes[0][0], "Ghostty restart canceled")  # nobody sees a spawned restart's error

    def test_force_ends_busy_sessions(self):
        self.claude = [{"pid": 77, "status": "busy", "name": "other work"}]
        with self.assertRaises(self.Started):
            restart.restart(force=True)

    def test_restart_by_hand_refuses_without_notifying(self):
        self.claude = [{"pid": 77, "status": "waiting", "name": "other work"}]
        with self.assertRaises(RuntimeError):
            restart.restart()
        self.assertEqual(self.notes, [])

    def test_spawn_passes_the_asking_session(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            os.environ[restart.SPARE_ENV] = "9"  # never inherited from serve itself
            restart.spawn_restart(force=False)
            self.assertNotIn(restart.SPARE_ENV, popen.call_args.kwargs["env"])
            restart.spawn_restart(force=False, spare=[41, 42])
            self.assertEqual(popen.call_args.kwargs["env"][restart.SPARE_ENV], "41,42")
            self.assertNotIn("--force", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

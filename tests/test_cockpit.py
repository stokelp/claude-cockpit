"""Unit tests for cockpit.py. Run with `make test` (standard library only)."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cockpit"))
import cockpit  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def stamp(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + offset))


def user(text, **extra):
    return {"type": "user", "timestamp": stamp(), "message": {"role": "user", "content": text}, **extra}


def assistant(uuid, *content):
    return {"type": "assistant", "uuid": uuid, "timestamp": stamp(), "message": {"role": "assistant", "content": list(content)}}


def text(t):
    return {"type": "text", "text": t}


def tool_use(tid, name, args=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": args or {}}


def tool_result(tid, content="ok"):
    return {
        "type": "user",
        "timestamp": stamp(),
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]},
    }


PICKER_ARGS = {
    "questions": [
        {
            "question": "Which database?",
            "header": "DB",
            "multiSelect": False,
            "options": [{"label": "Postgres", "description": ""}, {"label": "SQLite", "description": ""}],
        }
    ]
}


class Sandbox(unittest.TestCase):
    """Points every path of the module at a temporary ~/.claude."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "CLAUDE_DIR": root,
            "SESSIONS_DIR": root / "sessions",
            "PROJECTS_DIR": root / "projects",
            "SETTINGS_FILE": root / "settings.json",
            "DATA_DIR": root / "cockpit",
            "STATE_DIR": root / "cockpit" / "state",
            "LOG_FILE": root / "cockpit" / "cockpit.log",
            "CONFIG_FILE": root / "cockpit" / "config.json",
        }
        self.patches = [mock.patch.object(cockpit, k, v) for k, v in self.paths.items()]
        self.patches.append(mock.patch.object(cockpit, "map_terminals", lambda pids: {}))
        self.spawned = []
        self.patches.append(mock.patch.object(cockpit, "spawn_judge", lambda *a: self.spawned.append(a)))
        for p in self.patches:
            p.start()
        for key in ("SESSIONS_DIR", "PROJECTS_DIR", "STATE_DIR"):
            self.paths[key].mkdir(parents=True)
        self.transcript = self.paths["PROJECTS_DIR"] / "-work" / f"{SID}.jsonl"
        self.transcript.parent.mkdir(parents=True)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_transcript(self, *items):
        self.transcript.write_text("\n".join(json.dumps(i) for i in items) + "\n")
        old = time.time() - 60  # settled, so serve may judge it
        os.utime(self.transcript, (old, old))

    def open_session(self, status="idle", name="Demo session", name_source="hook"):
        data = {
            "pid": os.getpid(),
            "sessionId": SID,
            "cwd": "/work",
            "kind": "interactive",
            "name": name,
            "nameSource": name_source,
            "status": status,
        }
        (self.paths["SESSIONS_DIR"] / f"{os.getpid()}.json").write_text(json.dumps(data))

    def row(self):
        rows = cockpit.Snapshotter().snapshot()["sessions"]
        self.assertEqual(len(rows), 1)
        return rows[0]


class PromptText(unittest.TestCase):
    def test_typed_prompt(self):
        self.assertEqual(cockpit.prompt_text(user("fix the login bug")), "fix the login bug")

    def test_ignores_tool_results_meta_and_reminders(self):
        self.assertIsNone(cockpit.prompt_text(tool_result("t1")))
        self.assertIsNone(cockpit.prompt_text(user("hello", isMeta=True)))
        self.assertIsNone(cockpit.prompt_text(user("<system-reminder>x</system-reminder>")))
        self.assertIsNone(cockpit.prompt_text(user("[Request interrupted by user]")))

    def test_slash_command(self):
        cmd = "<command-name>/review</command-name><command-args>42</command-args>"
        self.assertEqual(cockpit.prompt_text(user(cmd)), "/review 42")


class Groups(Sandbox):
    def test_pending_picker_needs_you_even_when_busy(self):
        self.write_transcript(user("set up storage"), assistant("a1", tool_use("t1", "AskUserQuestion", PICKER_ARGS)))
        self.open_session(status="busy")
        row = self.row()
        self.assertEqual(row["group"], "needs_you")
        self.assertEqual(row["kind"], "picker")
        self.assertEqual(row["question"]["options"], ["Postgres", "SQLite"])
        self.assertFalse(row["can_reply"])

    def test_answered_picker_is_not_pending(self):
        self.write_transcript(
            user("set up storage"),
            assistant("a1", tool_use("t1", "AskUserQuestion", PICKER_ARGS)),
            tool_result("t1", "Postgres"),
            assistant("a2", tool_use("t2", "Bash", {"description": "Create the database"})),
        )
        self.open_session(status="busy")
        row = self.row()
        self.assertEqual(row["group"], "working")
        self.assertEqual(row["activity"], "Create the database")

    def test_idle_without_verdict_is_judged(self):
        self.write_transcript(user("explain the cache"), assistant("a1", text("The cache keys on user id.")))
        self.open_session()
        row = self.row()
        self.assertTrue(row["judging"])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][-1], "a1")

    def test_empty_session_is_ready(self):
        self.write_transcript()
        self.open_session()
        row = self.row()
        self.assertEqual(row["group"], "ready")
        self.assertTrue(row["can_close"])
        self.assertFalse(row["can_reply"])
        self.assertEqual(self.spawned, [])

    def test_done_can_be_cleared(self):
        self.write_transcript(user("explain the cache"), assistant("a1", text("The cache keys on user id.")))
        self.open_session()
        cockpit.save_state(SID, {"turn_key": "a1", "verdict": "done", "headline": "Cache explained"})
        with mock.patch.object(cockpit, "map_terminals", lambda pids: {os.getpid(): {"tab": 1}}):
            row = self.row()
        self.assertTrue(row["can_clear"])
        self.assertFalse(row["can_close"])

    def test_fresh_verdict_sets_the_group(self):
        self.write_transcript(user("explain the cache"), assistant("a1", text("The cache keys on user id.")))
        self.open_session()
        cockpit.save_state(SID, {"turn_key": "a1", "verdict": "close", "headline": "Cache explained", "followups": []})
        row = self.row()
        self.assertEqual(row["group"], "close")
        self.assertTrue(row["can_close"])
        self.assertEqual(self.spawned, [])

    def test_stale_verdict_triggers_a_new_judge(self):
        self.write_transcript(user("explain"), assistant("a1", text("Done.")), user("and the TTL?"), assistant("a2", text("One hour.")))
        self.open_session()
        cockpit.save_state(SID, {"turn_key": "a1", "verdict": "close"})
        self.assertTrue(self.row()["judging"])

    def test_judge_disabled_lands_in_done(self):
        self.paths["CONFIG_FILE"].parent.mkdir(parents=True, exist_ok=True)
        self.paths["CONFIG_FILE"].write_text(json.dumps({"judge": False}))
        self.write_transcript(user("explain"), assistant("a1", text("Done.")))
        self.open_session()
        self.assertEqual(self.row()["group"], "done")
        self.assertEqual(self.spawned, [])

    def test_permission_prompt_until_answered(self):
        self.write_transcript(user("deploy"), assistant("a1", tool_use("t1", "Bash", {"description": "Push to production"})))
        self.open_session(status="busy")
        cockpit.save_state(SID, {"attention": {"kind": "permission", "tool_use_id": "t1", "text": "Push to production"}})
        row = self.row()
        self.assertEqual((row["group"], row["kind"]), ("needs_you", "permission"))
        self.assertEqual(row["ask"], "Permission: Push to production")

        self.write_transcript(user("deploy"), assistant("a1", tool_use("t1", "Bash")), tool_result("t1"))
        self.assertEqual(self.row()["group"], "working")

    def test_waiting_status_is_a_permission_prompt(self):
        self.write_transcript(user("deploy"), assistant("a1", text("Deploying now.")))
        self.open_session(status="waiting")
        row = self.row()
        self.assertEqual((row["group"], row["kind"], row["ask"]), ("needs_you", "permission", "Waiting for your permission"))
        self.assertFalse(row["can_reply"])


class Names(Sandbox):
    def test_placeholder_name_shows_the_first_prompt(self):
        self.write_transcript(
            user("/model opus"),
            user("[Image #1] can we turn the follow-ups into a checklist?"),
            assistant("a1", text("Sure.")),
            user("and the title?"),
        )
        self.open_session(name="work-26", name_source="derived")
        self.assertEqual(self.row()["name"], "Can we turn the follow-ups into a checklist?")

    def test_placeholder_kept_until_there_is_a_prompt(self):
        self.write_transcript()
        self.open_session(name="work-26", name_source="derived")
        self.assertEqual(self.row()["name"], "work-26")

    def test_headless_runs_are_not_sessions(self):
        self.write_transcript(user("judge this"))
        self.open_session(name="cockpit-29", name_source="derived")
        f = self.paths["SESSIONS_DIR"] / f"{os.getpid()}.json"
        f.write_text(json.dumps({**json.loads(f.read_text()), "entrypoint": "sdk-cli"}))
        self.assertEqual(cockpit.Snapshotter().snapshot()["sessions"], [])

    def test_real_name_wins(self):
        self.write_transcript(user("fix the login bug"))
        self.open_session(name="Login bug", name_source="hook")
        self.assertEqual(self.row()["name"], "Login bug")


class Checklist(Sandbox):
    FOLLOWUPS = [{"who": "you", "text": "Merge PR #12"}, {"who": "claude", "text": "Re-run the backfill"}]

    def judged(self):
        self.write_transcript(user("backfill"), assistant("a1", text("Backfill done, PR #12 is open.")))
        self.open_session()
        cockpit.save_state(SID, {"turn_key": "a1", "verdict": "followups", "headline": "Backfill done", "followups": self.FOLLOWUPS})

    def test_ticks_mark_items_done(self):
        self.judged()
        cockpit.set_checked(SID, "Merge PR #12\n", True)
        row = self.row()
        self.assertEqual(row["group"], "followups")
        self.assertEqual([f["done"] for f in row["followups"]], [True, False])

        cockpit.set_checked(SID, "Merge PR #12", False)
        self.assertEqual([f["done"] for f in self.row()["followups"]], [False, False])

    def test_every_item_ticked_is_safe_to_close(self):
        self.judged()
        for item in self.FOLLOWUPS:
            cockpit.set_checked(SID, item["text"], True)
        with mock.patch.object(cockpit, "map_terminals", lambda pids: {os.getpid(): {"tab": 1}}):
            row = self.row()
        self.assertEqual(row["group"], "close")
        self.assertTrue(row["can_close"])
        self.assertTrue(row["can_reply"])  # still lets you tell Claude what you did

    def test_ticks_survive_a_new_verdict_and_reach_the_judge(self):
        self.judged()
        cockpit.set_checked(SID, "Merge PR #12", True)
        state = cockpit.load_state(SID)
        state.update(cockpit.clean_verdict({"verdict": "followups", "followups": [{"who": "you", "text": "Merge PR #12"}, "Deploy"]}))
        cockpit.save_state(SID, state)
        self.assertEqual([f["done"] for f in self.row()["followups"]], [True, False])
        context = cockpit.judge_context(self.transcript, "Demo", "/work", cockpit.load_state(SID)["checked"])
        self.assertIn("already ticked off as done:\n- Merge PR #12", context)

    def test_remembers_a_bounded_number(self):
        for i in range(cockpit.MAX_CHECKED + 5):
            cockpit.set_checked(SID, f"item {i}", True)
        checked = cockpit.load_state(SID)["checked"]
        self.assertEqual(len(checked), cockpit.MAX_CHECKED)
        self.assertEqual(checked[-1], f"item {cockpit.MAX_CHECKED + 4}")


class Verdicts(unittest.TestCase):
    def test_unknown_verdict_becomes_done(self):
        self.assertEqual(cockpit.clean_verdict({"verdict": "maybe"})["verdict"], "done")

    def test_dashes_and_close_followups(self):
        v = cockpit.clean_verdict({"verdict": "close", "headline": "Fixed — merged", "followups": ["x"]})
        self.assertEqual(v["headline"], "Fixed: merged")
        self.assertEqual(v["followups"], [])

    def test_followup_owners(self):
        v = cockpit.clean_verdict({"verdict": "followups", "followups": [{"who": "claude", "text": "a"}, {"who": "?", "text": "b"}, "c"]})
        self.assertEqual([f["who"] for f in v["followups"]], ["claude", "you", "you"])


class Hooks(Sandbox):
    def settings(self):
        return json.loads(self.paths["SETTINGS_FILE"].read_text())

    def test_install_keeps_other_hooks_and_is_idempotent(self):
        other = {"hooks": [{"type": "command", "command": "echo stop"}]}
        self.paths["SETTINGS_FILE"].write_text(json.dumps({"model": "opus", "hooks": {"Stop": [other]}}))
        self.assertEqual(cockpit.hooks_status(), "missing")
        cockpit.install_hooks()
        cockpit.install_hooks()
        s = self.settings()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["hooks"]["Stop"][0], other)
        for event in cockpit.HOOK_EVENTS:
            ours = [h for g in s["hooks"][event] for h in g["hooks"] if cockpit.HOOK_PATTERN.search(h["command"])]
            self.assertEqual(len(ours), 1, event)
        self.assertEqual(cockpit.hooks_status(), "installed")

    def test_outdated_copy_is_replaced(self):
        old = {"hooks": [{"type": "command", "command": "python3 /old/place/cockpit.py hook"}]}
        self.paths["SETTINGS_FILE"].write_text(json.dumps({"hooks": {"Stop": [old]}}))
        self.assertEqual(cockpit.hooks_status(), "outdated")
        cockpit.install_hooks()
        commands = [h["command"] for g in self.settings()["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(commands, [cockpit.hook_command()])

    def test_uninstall_removes_only_ours(self):
        other = {"hooks": [{"type": "command", "command": "echo stop"}]}
        self.paths["SETTINGS_FILE"].write_text(json.dumps({"hooks": {"Stop": [other]}}))
        cockpit.install_hooks()
        cockpit.uninstall_hooks()
        self.assertEqual(self.settings(), {"hooks": {"Stop": [other]}})

    def test_invalid_settings_are_left_alone(self):
        self.paths["SETTINGS_FILE"].write_text("{not json")
        with self.assertRaises(RuntimeError):
            cockpit.install_hooks()
        self.assertEqual(self.paths["SETTINGS_FILE"].read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()

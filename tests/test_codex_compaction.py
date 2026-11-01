"""Codex rollouts preserve the same compaction facts as Claude transcripts."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tokenmaxxing" / "src"))

from gutcheck_core import codex_rollout, config, jev, rules  # noqa: E402
from tokenmaxxing import checkpoint, evidence, health, hooks, stopcheck, transitions  # noqa: E402


class CodexCompactionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()
        self.tasks = self.root / "TASKS.md"
        self.tasks.write_text("# Tasks\n\n## Now\n- Goal: retain COPPER-OWL-418\n\n- [ ] Ship the parser\n")
        (self.root / "important.txt").write_text("keep this file\n")
        self.transcript = self.root / "rollout.jsonl"
        self.records = [
            {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"id": "codex-1", "cwd": str(self.root)}},
            self.message("user", "<environment_context>do not capture this</environment_context>"),
            self.message("user", "# AGENTS.md instructions for /project\nDo not capture this generated context."),
            self.message("user", "Always preserve COPPER-OWL-418 and never overwrite important.txt."),
            self.message("assistant", "We decided to keep the existing checkpoint format."),
            self.item({"type": "FileChange", "id": "edit-1", "status": "completed", "changes": {
                str(self.tasks): {"type": "add", "content": "ignored"},
                str(self.root / "demo.py"): {"type": "add", "content": "ignored"}}}),
            self.item({"type": "FileChange", "id": "edit-failed", "status": "failed", "changes": {
                str(self.root / "failed.py"): {"type": "add", "content": "ignored"}}}),
            self.item({"type": "CommandExecution", "id": "exec-1", "status": "completed", "exit_code": 0,
                       "command": ["/bin/zsh", "-lc", "cat TASKS.md"], "cwd": self.root.as_uri(),
                       "parsed_cmd": [{"type": "read", "cmd": "cat TASKS.md", "path": "TASKS.md"}]}),
            {"type": "token_usage_record", "payload": {"usage": {
                "input_tokens": 25000, "cached_input_tokens": 20000, "output_tokens": 10}}},
        ]
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in self.records))
        self.patches = [
            patch.object(config, "STATE_DIR", Path(self.temp.name) / "state"),
            patch.object(config, "RULES_DIR", Path(self.temp.name) / "rules"),
            patch.object(config, "DATA_DIR_OVERRIDE", str(Path(self.temp.name) / "data")),
            patch.object(config, "CONFIG_PATH", Path(self.temp.name) / "config.toml"),
            patch.object(config, "_cache", None),
            patch.object(jev, "have_key", return_value=False),
            patch.object(evidence, "_claude", side_effect=AssertionError("unexpected model call")),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    @staticmethod
    def message(role, text):
        return {"type": "response_item", "timestamp": "2026-01-01T00:00:01Z", "payload": {
            "type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}

    @staticmethod
    def item(item):
        return {"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z",
                "payload": {"type": "item_completed", "item": item}}

    def run_hook(self, event, payload):
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), patch.object(sys, "stdout", out):
            hooks.main([event])
        return out.getvalue()

    def test_rollout_facts_and_task_ownership(self):
        state = checkpoint.scan(str(self.transcript))
        self.assertEqual([item["text"] for item in state["owner"]],
                         ["Always preserve COPPER-OWL-418 and never overwrite important.txt."])
        self.assertEqual([item["text"] for item in state["agent_text"]],
                         ["We decided to keep the existing checkpoint format."])
        self.assertIn(str(self.root / "demo.py"), state["files"])
        self.assertNotIn(str(self.root / "failed.py"), state["files"])
        self.assertEqual(list(state["tasks_files"]), [str(self.tasks)])
        self.assertEqual(stopcheck.find_tasks(str(self.root), str(self.transcript))[0], self.tasks)
        self.assertEqual(hooks.context_tokens(str(self.transcript)), 25000)

    @staticmethod
    def turn_record(kind, turn, cwd=None):
        if kind == "context":
            return {"type": "turn_context", "payload": {"turn_id": turn, "cwd": cwd}}
        return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn}}

    def append(self, *records):
        with self.transcript.open("a") as out:
            out.writelines(json.dumps(record) + "\n" for record in records)

    def test_rule_reader_handles_partial_appends_without_duplication(self):
        messages, position = rules._messages(str(self.transcript))
        self.assertEqual([(item[2], item[3]) for item in messages],
                         [("Always preserve COPPER-OWL-418 and never overwrite important.txt.", str(self.root))])
        additional = json.dumps(self.message("user", "Never remove the demo file."))
        with self.transcript.open("a") as out:
            out.write(additional[:20])
        self.assertEqual(rules._messages(str(self.transcript))[1], position)
        with self.transcript.open("a") as out:
            out.write(additional[20:] + "\n")
        later, final = rules._messages(str(self.transcript))
        self.assertEqual(later[-1][2], "Never remove the demo file.")
        self.assertEqual(final, self.transcript.stat().st_size)

    def test_rescanned_rollout_adds_each_unit_once(self):
        (self.root / "second.txt").write_text("keep this too\n")
        rules.sync("codex-1", str(self.transcript))
        self.append(self.message("user", "Never delete second.txt."))
        rules.sync("codex-1", str(self.transcript))
        recorded = rules.load("codex-1")
        self.assertEqual(len(recorded), 2)
        self.assertIn(str((self.root / "second.txt").resolve()), recorded[-1]["keep"])
        rules.sync("codex-1", str(self.transcript))
        self.assertEqual(len(rules.load("codex-1")), 2)
        self.assertEqual(set(json.loads(rules._paths("codex-1")[2].read_text())), {"transcript", "offset"})

    def test_changed_directory_and_a_new_transcript(self):
        second = self.root / "second"
        second.mkdir()
        (second / "owned.txt").write_text("keep\n")
        rules.sync("codex-1", str(self.transcript))
        self.append({"type": "turn_context", "payload": {"cwd": str(second)}},
                    self.message("user", "Never delete owned.txt."))
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        self.assertIn(str((second / "owned.txt").resolve()), rules.load("codex-1")[-1]["keep"])

        other = self.root / "other.jsonl"
        (self.root / "new.txt").write_text("keep\n")
        other.write_text(json.dumps(self.message("user", "Never delete new.txt.")) + "\n")
        rules.sync("codex-1", str(other), cwd=str(self.root))
        self.assertIn(str((self.root / "new.txt").resolve()), rules.load("codex-1")[-1]["keep"])

    def test_message_before_its_turn_context_after_a_resume_into_another_directory(self):
        # Real rollouts tie a message to its turn by the metadata turn_id; that turn's context can come later.
        new = self.root / "resumed"
        new.mkdir()
        (new / "important.txt").write_text("new place\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        message = self.message("user", "Never delete important.txt.")
        message["payload"]["internal_chat_message_metadata_passthrough"] = {"turn_id": "resumed-turn"}
        self.append(self.turn_record("context", "earlier-turn", str(self.root)),
                    self.turn_record("task_started", "resumed-turn"), message,
                    self.turn_record("context", "resumed-turn", str(new)))
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((new / "important.txt").resolve())])

    def test_open_turn_uses_the_hook_directory_and_the_stored_directory_is_final(self):
        new = self.root / "resumed"
        new.mkdir()
        (new / "important.txt").write_text("new place\n")
        absolute = Path(self.temp.name) / "absolute.txt"
        absolute.write_text("keep\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        self.append(self.turn_record("task_started", "resumed-turn"),
                    self.message("user", f"Never delete important.txt or {absolute}."))
        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        stored = rules.load("codex-1")[-1]["keep"]
        self.assertEqual(stored, [str((new / "important.txt").resolve()), str(absolute.resolve())])
        # Neither the file vanishing nor a later context naming another directory changes what was stored.
        (new / "important.txt").unlink()
        self.append(self.turn_record("context", "resumed-turn", str(self.root)))
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], stored)
        self.assertEqual(len(rules.load("codex-1")), 2)

    def test_consecutive_turns_keep_their_own_directories(self):
        first, second = self.root / "resume-a", self.root / "resume-b"
        first.mkdir()
        second.mkdir()
        (first / "alpha.txt").write_text("keep\n")
        (second / "beta.txt").write_text("keep\n")
        self.append(self.turn_record("task_started", "turn-a"), self.message("user", "Never delete alpha.txt."))
        rules.sync("codex-1", str(self.transcript), cwd=str(first))
        self.append(self.turn_record("task_complete", "turn-a"), self.turn_record("task_started", "turn-b"),
                    self.message("user", "Never delete beta.txt."), self.turn_record("context", "turn-b", str(second)))
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        saved = rules.load("codex-1")
        self.assertEqual(saved[-2]["keep"], [str((first / "alpha.txt").resolve())])
        self.assertEqual(saved[-1]["keep"], [str((second / "beta.txt").resolve())])

    def test_missing_transcript_still_fails_open(self):
        result = rules.sync("missing", str(self.root / "missing.jsonl"))
        self.assertEqual(result["messages"], 0)

    def test_claude_incremental_rules_keep_each_messages_own_cwd(self):
        second = self.root / "claude-second"
        second.mkdir()
        (self.root / "first.txt").write_text("keep\n")
        (second / "last.txt").write_text("keep\n")
        claude = self.root / "claude.jsonl"
        def record(text, cwd):
            return {"type": "user", "timestamp": "2026-01-01T00:00:01Z", "cwd": str(cwd),
                    "message": {"content": text}}
        claude.write_text(json.dumps(record("Never delete first.txt.", self.root)) + "\n")
        rules.sync("claude-1", str(claude))
        with claude.open("a") as out:
            out.write(json.dumps(record("Never delete last.txt.", second)) + "\n")
        rules.sync("claude-1", str(claude))
        saved = rules.load("claude-1")
        self.assertEqual([record["keep"] for record in saved],
                         [[str((self.root / "first.txt").resolve())], [str((second / "last.txt").resolve())]])

    def test_token_usage_preserves_claude_accounting_and_uses_codex_last_input(self):
        self.transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 100, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 25}}}) + "\n")
        self.assertEqual(hooks.context_tokens(str(self.transcript)), 175)
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 500, "cached_input_tokens": 400}}}}) + "\n")
        self.assertEqual(hooks.context_tokens(str(self.transcript)), 500)

    @staticmethod
    def exec_call(call_id):
        return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": call_id,
                "input": "await tools.exec_command({cmd:'sleep 9'})"}}

    @staticmethod
    def exec_output(call_id, result=None):
        return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                "output": json.dumps(result or {"wall_time_seconds": 1, "session_id": 502})}}

    def exec_done(self, status="completed", session="502"):
        return self.item({"type": "CommandExecution", "id": "exec-x", "status": status, "process_id": session,
                          "command": "sleep 9"})

    def running(self, *records):
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        return dict(checkpoint.running(checkpoint.scan(str(self.transcript))))

    def test_active_codex_command_is_recorded_until_its_completion(self):
        result = {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z", "payload": {
            "type": "custom_tool_call_output", "call_id": "call-1", "output": [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": json.dumps({"wall_time_seconds": 1.0, "session_id": 35253,
                                                            "output": ""})}]}}
        self.assertIn("35253", self.running(*self.records, self.exec_call("call-1"), result))
        self.assertNotIn("35253", self.running(*self.records, self.exec_call("call-1"), result,
                                               self.exec_done(session="35253")))

    def test_late_yield_of_a_command_that_already_completed(self):
        # Codex logs the completion while the exec script still runs; the script's output then reports the yield.
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                self.assertNotIn("502", self.running(self.exec_call("a"), self.exec_done(status),
                                                     self.exec_output("a")))
                self.assertNotIn("502", self.running(self.exec_call("a"), self.exec_output("a"),
                                                     self.exec_done(status)))

    def test_session_id_reused_after_exit_is_running_again(self):
        for first in ((self.exec_call("a"), self.exec_output("a"), self.exec_done()),
                      (self.exec_call("a"), self.exec_done(), self.exec_output("a")),
                      (self.exec_call("a"), self.exec_done())):
            with self.subTest(first=len(first)):
                records = [*first, self.exec_call("b"), self.exec_output("b")]
                self.assertIn("502", self.running(*records))
                self.assertNotIn("502", self.running(*records, self.exec_done()))
        direct = [{"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                   "call_id": "d", "arguments": json.dumps({"cmd": "sleep 2"})}},
                  {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "d",
                   "output": json.dumps({"wall_time_seconds": 1, "session_id": 502})}}]
        self.assertIn("502", self.running(self.exec_call("a"), self.exec_output("a"), self.exec_done(), *direct))

    def test_poll_and_intermediate_output_close_nothing(self):
        notify = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "a",
                  "output": [{"type": "input_text", "text": "starting"}]}}
        records = [self.exec_call("a"), notify, self.exec_output("a"), self.exec_call("poll"),
                   self.exec_output("poll")]
        self.assertIn("502", self.running(*records))
        self.assertNotIn("502", self.running(*records, self.exec_call("poll2"), self.exec_done(),
                                             self.exec_output("poll2", {"wall_time_seconds": 1, "exit_code": 0})))

    def test_command_working_directory_does_not_change_the_session_directory(self):
        other = self.root / "command-dir"
        other.mkdir()
        (other / "TASKS.md").write_text("- [ ] other task\n")
        records = [
            {"type": "session_meta", "payload": {"cwd": str(self.root)}},
            self.item({"type": "CommandExecution", "id": "other-cwd", "status": "completed",
                       "command": "cat TASKS.md", "cwd": other.as_uri(),
                       "parsed_cmd": [{"type": "read", "path": "TASKS.md"}]}),
            self.item({"type": "FileChange", "id": "later-edit", "status": "completed",
                       "changes": {"later.py": {"type": "add"}}}),
        ]
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        state = checkpoint.scan(str(self.transcript))
        self.assertEqual(state["cwd"], str(self.root))
        self.assertIn(str(self.root / "later.py"), state["files"])
        self.assertIn(str(other / "TASKS.md"), state["tasks_files"])
        self.assertIn(str(other / "TASKS.md"), stopcheck.session_tasks_files(str(self.transcript)))

    def test_noncompleted_commands_do_not_emit_bash_or_read_facts(self):
        for status in ("failed", "cancelled", "in_progress", None):
            record = self.item({"type": "CommandExecution", "id": "unfinished", "status": status,
                                "command": "git switch -c unwanted", "cwd": str(self.root),
                                "parsed_cmd": [{"type": "read", "path": "TASKS.md"}]})
            with self.subTest(status=status):
                self.assertIsNone(codex_rollout.normalize(record, cwd=str(self.root)))
                self.transcript.write_text(json.dumps(record) + "\n")
                state = checkpoint.scan(str(self.transcript))
                self.assertNotIn("unwanted", state["branches"])
                self.assertFalse(state["tasks_files"])

        completed = self.item({"type": "CommandExecution", "id": "finished", "status": "completed",
                               "exit_code": 1, "command": "cat TASKS.md", "cwd": str(self.root),
                               "parsed_cmd": [{"type": "read", "path": "TASKS.md"}]})
        self.assertEqual([block["name"] for block in codex_rollout.normalize(completed)["message"]["content"]],
                         ["Bash", "Read"])

    def test_yielded_exec_cell_wait_can_report_a_running_command(self):
        cell = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                "output": "Script running with cell ID g3:46\nWall time 1.0 seconds\nOutput:\n"}}
        wait = {"type": "response_item", "payload": {"type": "function_call", "name": "wait", "call_id": "w1",
                "arguments": json.dumps({"cell_id": "g3:46", "yield_time_ms": 6000})}}
        waited = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "w1", "output": [
            {"type": "input_text", "text": "Script completed\nOutput:\n"},
            {"type": "input_text", "text": json.dumps({"wall_time_seconds": 4.0, "session_id": 502})}]}}
        self.assertIn("502", self.running(self.exec_call("c1"), cell, wait, waited))
        # completed while the cell ran, before the wait reported its yield (seen in real rollouts)
        self.assertNotIn("502", self.running(self.exec_call("c1"), cell, self.exec_done(), wait, waited))

    def test_codex_plan_keeps_only_open_items(self):
        plan = {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z", "payload": {
            "type": "function_call", "name": "update_plan", "call_id": "plan-1", "arguments": json.dumps({
                "plan": [{"step": "Finished step", "status": "completed"},
                         {"step": "Finish review", "status": "in_progress"}]})}}
        with self.transcript.open("a") as out:
            out.write(json.dumps(plan) + "\n")
        text, _, _ = checkpoint.build(str(self.transcript), session="codex-plan", cwd=str(self.root))
        self.assertIn("Finish review", text)
        self.assertNotIn("Finished step", text)

    def test_codex_subagent_runs_from_started_to_its_end(self):
        def activity(kind, path="/root/worker"):
            return self.item({"type": "SubAgentActivity", "kind": kind, "agent_path": path,
                              "agent_thread_id": "thread-" + path, "model": "gpt-6.1-terra"})
        self.assertIn("/root/worker", self.running(activity("started")))
        for end in ("completed", "failed", "interrupted", "cancelled"):
            with self.subTest(end=end):
                self.assertNotIn("/root/worker", self.running(activity("started"), activity(end)))
        # interacted (send_message, followup_task, the current thread itself) neither revives nor ends an agent
        self.assertEqual(self.running(activity("started"), activity("completed"), activity("interacted"),
                                      activity("interacted", "/root")), {})
        self.assertIn("/root/worker", self.running(activity("started"), activity("interacted")))

    def test_documented_hooks_run_through_the_linked_cli_across_an_upgrade(self):
        repo = Path(__file__).resolve().parents[1]
        readme = (repo / "tokenmaxxing" / "README.md").read_text()
        section = readme.split("### Codex compaction hooks", 1)[1]
        example = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))
        fake_home = self.root / "marketplace-home"
        python = fake_home / ".local/share/gutcheck/venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        link = fake_home / ".local/bin/tokenmaxxing"
        link.parent.mkdir(parents=True)
        config_dir = fake_home / ".config/tokenmaxxing"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text('[transitions]\nmode = "off"\n')
        transcript = fake_home / "rollout.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(fake_home)}}) + "\n" +
                              json.dumps(self.message("user", "Always preserve MARKETPLACE-CHECKPOINT.")) + "\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GUTCHECK_", "TOKENMAXXING_"))}
        env["HOME"] = str(fake_home)
        cache = fake_home / ".claude/plugins/cache/gutcheck/tokenmaxxing"
        for version, old in (("0.2.old", None), ("0.2.new", "0.2.old")):
            plugin = cache / version
            shutil.copytree(repo / "tokenmaxxing", plugin, symlinks=False,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            # Exercise the real CLI and handlers without using a keychain or network.
            (plugin / "src/sitecustomize.py").write_text("from gutcheck_core import jev\njev.have_key = lambda: False\n")
            link.unlink(missing_ok=True)
            link.symlink_to(plugin / "bin/tokenmaxxing")  # what a Claude session start does after an upgrade
            if old:
                shutil.rmtree(cache / old)
            payload = {"session_id": "marketplace-test", "transcript_path": str(transcript), "cwd": str(fake_home),
                       "hook_event_name": "PreCompact", "trigger": "manual"}
            for event in ("PreCompact", "SessionStart"):
                if event == "SessionStart":
                    payload = {**payload, "hook_event_name": "SessionStart", "source": "compact"}
                result = subprocess.run(example["hooks"][event][0]["hooks"][0]["command"], shell=True, env=env,
                                        input=json.dumps(payload), capture_output=True, text=True, timeout=20)
                with self.subTest(version=version, event=event):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if event == "PreCompact":
                        output = fake_home / ".local/state/tokenmaxxing-codex/data/checkpoints/marketplace-test.md"
                        self.assertIn("MARKETPLACE-CHECKPOINT", output.read_text())
                    else:
                        self.assertIn("MARKETPLACE-CHECKPOINT",
                                      json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_malformed_records_are_ignored(self):
        with self.transcript.open("a") as out:
            for record in ({"type": "session_meta", "payload": "invalid"},
                           {"type": "response_item", "payload": {"type": "custom_tool_call", "input": 12}},
                           {"type": "response_item", "payload": {"type": "function_call_output", "call_id": []}},
                           self.item({"type": "CommandExecution", "id": [], "status": "completed",
                                      "process_id": {}}),
                           self.item({"type": "SubAgentActivity", "kind": "completed", "agent_path": []}),
                           {"type": "event_msg", "payload": {"type": "item_completed", "item": "invalid"}}):
                out.write(json.dumps(record) + "\n")
        state = checkpoint.scan(str(self.transcript))
        self.assertEqual(dict(checkpoint.running(state)), {})
        self.assertEqual(len(state["owner"]), 1)
        self.assertEqual(len(rules._messages(str(self.transcript))[0]), 1)
        self.assertEqual(hooks.context_tokens(str(self.transcript)), 25000)

    def test_task_ownership_follows_recorded_working_directory_changes(self):
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        (first / "TASKS.md").write_text("- [ ] first project\n")
        (second / "TASKS.md").write_text("- [ ] second project\n")
        records = [
            {"type": "session_meta", "payload": {"cwd": str(first)}},
            self.item({"type": "FileChange", "id": "edit-1", "status": "completed",
                       "changes": {"TASKS.md": {"type": "add", "content": "ignored"}}}),
            {"type": "turn_context", "payload": {"cwd": str(second)}},
        ]
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.assertEqual(stopcheck.find_tasks(str(second), str(self.transcript))[0], first / "TASKS.md")
        self.assertEqual(list(checkpoint.scan(str(self.transcript))["tasks_files"]), [str(first / "TASKS.md")])
        self.transcript.write_text(json.dumps(records[1]) + "\n")
        self.assertEqual(stopcheck.session_tasks_files(str(self.transcript)), [])

    def test_checkpoint_is_written_and_compact_only_hook_reinjects_it(self):
        payload = {"session_id": "codex-1", "transcript_path": str(self.transcript), "cwd": str(self.root),
                   "hook_event_name": "PreCompact", "trigger": "manual"}
        self.assertEqual(self.run_hook("PreCompact", payload), "")
        output = checkpoint.paths_for("codex-1")[0]
        self.assertTrue(output.exists())
        text = output.read_text()
        self.assertIn("Always preserve COPPER-OWL-418", text)
        self.assertIn("Ship the parser", text)
        self.assertIn("demo.py", text)
        self.assertIn(str((self.root / "important.txt").resolve()), rules.load("codex-1")[0]["keep"])
        with patch.object(health, "session_start", side_effect=AssertionError("health must not run")) as health_hook, \
                patch.object(transitions, "on_start", side_effect=AssertionError("registry must not run")) as registry:
            result = self.run_hook("CompactSessionStart", {**payload, "hook_event_name": "SessionStart",
                                                           "source": "compact"})
            self.assertIn("Always preserve COPPER-OWL-418", json.loads(result)["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(self.run_hook("CompactSessionStart", {**payload, "source": "startup"}), "")
            health_hook.assert_not_called()
            registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()

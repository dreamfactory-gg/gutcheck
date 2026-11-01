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

    def test_rule_reader_handles_partial_appends_without_duplication(self):
        messages, position, cwd = rules._messages(str(self.transcript))
        self.assertEqual([item[2] for item in messages],
                         ["Always preserve COPPER-OWL-418 and never overwrite important.txt."])
        self.assertEqual(cwd, str(self.root))
        additional = json.dumps(self.message("user", "Never remove the demo file."))
        with self.transcript.open("a") as out:
            out.write(additional[:20])
        empty, unchanged, cwd = rules._messages(str(self.transcript), start=position, cwd=cwd)
        self.assertEqual(empty, [])
        self.assertEqual(unchanged, position)
        self.assertEqual(cwd, str(self.root))
        with self.transcript.open("a") as out:
            out.write(additional[20:] + "\n")
        later, final, cwd = rules._messages(str(self.transcript), start=unchanged, cwd=cwd)
        self.assertEqual([item[2] for item in later], ["Never remove the demo file."])
        self.assertEqual(final, self.transcript.stat().st_size)
        self.assertEqual(later[0][3], str(self.root))

    def test_incremental_rule_sync_retains_the_codex_working_directory(self):
        (self.root / "second.txt").write_text("keep this too\n")
        rules.sync("codex-1", str(self.transcript))
        self.assertEqual(json.loads(rules._paths("codex-1")[2].read_text())["cwd"], str(self.root))
        with self.transcript.open("a") as out:
            out.write(json.dumps(self.message("user", "Never delete second.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript))
        recorded = rules.load("codex-1")
        self.assertEqual(len(recorded), 2)
        self.assertIn(str((self.root / "second.txt").resolve()), recorded[-1]["keep"])
        rules.sync("codex-1", str(self.transcript))
        self.assertEqual(len(rules.load("codex-1")), 2)

    def test_incremental_rule_sync_tracks_changed_cwd_and_resets_for_a_new_transcript(self):
        second = self.root / "second"
        second.mkdir()
        (second / "owned.txt").write_text("keep\n")
        rules.sync("codex-1", str(self.transcript))
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "turn_context", "payload": {"cwd": str(second)}}) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        with self.transcript.open("a") as out:
            out.write(json.dumps(self.message("user", "Never delete owned.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        self.assertIn(str((second / "owned.txt").resolve()), rules.load("codex-1")[-1]["keep"])
        self.assertEqual(json.loads(rules._paths("codex-1")[2].read_text())["cwd"], str(second))

        other = self.root / "other.jsonl"
        (self.root / "new.txt").write_text("keep\n")
        other.write_text(json.dumps(self.message("user", "Never delete new.txt.")) + "\n")
        rules.sync("codex-1", str(other), cwd=str(self.root))
        self.assertIn(str((self.root / "new.txt").resolve()), rules.load("codex-1")[-1]["keep"])
        self.assertEqual(json.loads(rules._paths("codex-1")[2].read_text())["cwd"], str(self.root))

    def test_incremental_rule_sync_recovers_cwd_from_an_old_state_file(self):
        (self.root / "legacy.txt").write_text("keep\n")
        rules.sync("codex-1", str(self.transcript))
        state_path = rules._paths("codex-1")[2]
        state = json.loads(state_path.read_text())
        state.pop("cwd")
        state_path.write_text(json.dumps(state))
        with self.transcript.open("a") as out:
            out.write(json.dumps(self.message("user", "Never delete legacy.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript))
        self.assertIn(str((self.root / "legacy.txt").resolve()), rules.load("codex-1")[-1]["keep"])

    def test_failed_compaction_after_resume_uses_the_new_turn_directory(self):
        new = self.root / "resumed"
        new.mkdir()
        (new / "important.txt").write_text("new place\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "resumed-turn"}}) + "\n")
            out.write(json.dumps(self.message("user", "Never delete important.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        saved = rules.load("codex-1")[-1]
        self.assertEqual(saved["keep"], [str((new / "important.txt").resolve())])

        # Re-reading an unresolved turn must not forget a protected path just
        # because the file temporarily disappeared after it was captured.
        (new / "important.txt").unlink()
        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((new / "important.txt").resolve())])
        (new / "important.txt").write_text("new place\n")

        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "turn_context", "payload": {
                "turn_id": "resumed-turn", "cwd": str(new)}}) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((new / "important.txt").resolve())])
        self.assertEqual(len(rules.load("codex-1")), 2)

    def test_misbound_rule_from_old_state_is_reconciled_after_resume(self):
        new = self.root / "resumed"
        new.mkdir()
        (new / "important.txt").write_text("new place\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        state_path = rules._paths("codex-1")[2]
        message = self.message("user", "Never delete important.txt.")
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "resumed-turn"}}) + "\n")
            out.write(json.dumps(message) + "\n")
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "resumed-turn"}}) + "\n")
        rules.add_message("codex-1", message["timestamp"], "owner", "Never delete important.txt.", cwd=str(self.root))
        state = json.loads(state_path.read_text())
        state.pop("turn_tracking", None)
        state.pop("pending_turn_start", None)
        state["offset"] = self.transcript.stat().st_size
        state_path.write_text(json.dumps(state))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((self.root / "important.txt").resolve())])

        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((new / "important.txt").resolve())])
        self.assertEqual(len(rules.load("codex-1")), 2)

    def test_late_turn_context_corrects_a_provisional_rule_directory(self):
        new = self.root / "resumed"
        new.mkdir()
        (new / "important.txt").write_text("new place\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "resumed-turn"}}) + "\n")
            out.write(json.dumps(self.message("user", "Never delete important.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((self.root / "important.txt").resolve())])
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "turn_context", "payload": {
                "turn_id": "resumed-turn", "cwd": str(new)}}) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((new / "important.txt").resolve())])
        self.assertEqual(len(rules.load("codex-1")), 2)

    def test_consecutive_uncontextualized_turns_keep_their_own_directories(self):
        first, second = self.root / "resume-a", self.root / "resume-b"
        first.mkdir()
        second.mkdir()
        (first / "alpha.txt").write_text("keep\n")
        (second / "beta.txt").write_text("keep\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-a"}}) + "\n")
            out.write(json.dumps(self.message("user", "Never delete alpha.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(first))
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-a"}}) + "\n")
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-b"}}) + "\n")
            out.write(json.dumps(self.message("user", "Never delete beta.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(second))
        saved = rules.load("codex-1")
        self.assertEqual(saved[-2]["keep"], [str((first / "alpha.txt").resolve())])
        self.assertEqual(saved[-1]["keep"], [str((second / "beta.txt").resolve())])

    def test_pending_turn_survives_an_incremental_sync_before_its_prompt(self):
        new = self.root / "resumed"
        new.mkdir()
        (new / "important.txt").write_text("new place\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(self.root))
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "resumed-turn"}}) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        with self.transcript.open("a") as out:
            out.write(json.dumps(self.message("user", "Never delete important.txt.")) + "\n")
        rules.sync("codex-1", str(self.transcript), cwd=str(new))
        self.assertEqual(rules.load("codex-1")[-1]["keep"], [str((new / "important.txt").resolve())])

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

    def test_active_codex_command_is_recorded_until_its_completion(self):
        command = {"type": "response_item", "timestamp": "2026-01-01T00:00:03Z", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "call-1",
            "input": 'const r = await tools.exec_command({cmd:"sleep 25",yield_time_ms:1000}); text(JSON.stringify(r));'}}
        result = {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z", "payload": {
            "type": "custom_tool_call_output", "call_id": "call-1", "output": [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": json.dumps({"wall_time_seconds": 1.0, "session_id": 35253,
                                                            "output": ""})}]}}
        with self.transcript.open("a") as out:
            out.write(json.dumps(command) + "\n" + json.dumps(result) + "\n")
        self.assertIn("35253", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))
        completed = self.item({"type": "CommandExecution", "id": "exec-1", "status": "completed",
                               "process_id": "35253", "command": ["/bin/zsh", "-lc", "sleep 25"],
                               "cwd": self.root.as_uri(), "exit_code": 0})
        with self.transcript.open("a") as out:
            out.write(json.dumps(completed) + "\n")
        self.assertNotIn("35253", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_completed_command_is_not_resurrected_by_a_late_yield(self):
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                "call_id": "call-late", "input": "await tools.exec_command({cmd:'sleep 9'})"}}
        output = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call-late",
                  "output": json.dumps({"wall_time_seconds": 1, "session_id": 502})}}
        for status in ("completed", "failed", "cancelled", "canceled"):
            done = self.item({"type": "CommandExecution", "id": "exec-late", "status": status,
                              "process_id": "502", "command": "sleep 9"})
            for records in ((call, done, output), (call, output, done)):
                with self.subTest(status=status, order=records.index(done)):
                    self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
                    self.assertNotIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_reused_command_session_id_tracks_each_invocation(self):
        def call(call_id):
            return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                    "call_id": call_id, "input": "await tools.exec_command({cmd:'sleep 9'})"}}
        def yielded(call_id):
            return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                    "output": json.dumps({"wall_time_seconds": 1, "session_id": 502})}}
        def ended(item_id):
            return self.item({"type": "CommandExecution", "id": item_id, "status": "completed",
                              "process_id": "502", "command": "sleep 9"})
        second_start = {"type": "event_msg", "payload": {"type": "item_started", "item": {
            "type": "CommandExecution", "id": "exec-b", "process_id": "502", "status": "in_progress"}}}
        for prefix in ((call("a"), yielded("a"), ended("exec-a")),
                       (call("a"), ended("exec-a"), yielded("a")),
                       (call("a"), ended("exec-a"))):
            with self.subTest(prefix_length=len(prefix), yielded_first=prefix[1].get("type") == "response_item"):
                records = [*prefix, call("b"), second_start, yielded("b")]
                if len(prefix) == 2:
                    records.append(yielded("a"))  # an old result arrives after B has already yielded
                self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
                self.assertIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))
                with self.transcript.open("a") as out:
                    out.write(json.dumps(ended("exec-b")) + "\n")
                self.assertNotIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_reused_session_id_inside_one_exec_cell_is_not_blacklisted(self):
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                "call_id": "outer", "input": "await tools.exec_command({cmd:'sleep 1'})"}}
        done = self.item({"type": "CommandExecution", "id": "old-command", "status": "completed",
                          "process_id": "502", "command": "sleep 1"})
        started = {"type": "event_msg", "payload": {"type": "item_started", "item": {
            "type": "CommandExecution", "id": "new-command", "process_id": "502", "status": "in_progress"}}}
        output = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "outer",
                  "output": json.dumps({"wall_time_seconds": 1, "session_id": 502})}}
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in (call, done, started, output)))
        self.assertIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))
        with self.transcript.open("a") as out:
            out.write(json.dumps(self.item({"type": "CommandExecution", "id": "new-command", "status": "completed",
                                           "process_id": "502", "command": "sleep 1"})) + "\n")
        self.assertNotIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_overlapping_exec_calls_correlate_completions_to_their_commands(self):
        def call(call_id, command):
            return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                    "call_id": call_id, "input": f"await tools.exec_command({{cmd:'{command}'}})"}}
        def item_event(event, item_id, command, status):
            return {"type": "event_msg", "payload": {"type": event, "item": {
                "type": "CommandExecution", "id": item_id, "process_id": "502",
                "status": status, "command": command}}}
        def yielded(call_id):
            return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                    "output": json.dumps({"wall_time_seconds": 1, "session_id": 502})}}
        records = [call("a", "sleep 1"), call("b", "sleep 2"),
                   item_event("item_started", "exec-a", "sleep 1", "in_progress"),
                   item_event("item_completed", "exec-a", "sleep 1", "completed"),
                   item_event("item_started", "exec-b", "sleep 2", "in_progress"), yielded("b"),
                   item_event("item_completed", "exec-b", "sleep 2", "completed"), yielded("a")]
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.assertNotIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_ambiguous_overlapping_calls_do_not_hide_a_live_session(self):
        def call(call_id):
            return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                    "call_id": call_id, "input": "await tools.exec_command({cmd:'sleep 1'})"}}
        done = self.item({"type": "CommandExecution", "id": "unknown-command", "status": "completed",
                          "process_id": "502", "command": "sleep 1"})
        output = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "b",
                  "output": json.dumps({"wall_time_seconds": 1, "session_id": 502})}}
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in (call("a"), call("b"), done, output)))
        self.assertIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_direct_exec_command_can_reuse_an_earlier_session_id(self):
        def call(call_id, command):
            return {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                    "call_id": call_id, "arguments": json.dumps({"cmd": command})}}
        def output(call_id):
            return {"type": "response_item", "payload": {"type": "function_call_output", "call_id": call_id,
                    "output": json.dumps({"wall_time_seconds": 1, "session_id": 502})}}
        done = self.item({"type": "CommandExecution", "id": "first", "status": "completed",
                          "process_id": "502", "command": "sleep 1"})
        records = [call("a", "sleep 1"), done, output("a"), call("b", "sleep 2"), output("b")]
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.assertIn("502", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

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
        records = [
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                "input": 'await tools.exec_command({cmd:"sleep 9",yield_time_ms:4000})'}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                "output": "Script running with cell ID 202\nWall time 1.0 seconds\nOutput:\n"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "wait", "call_id": "w1",
                "arguments": json.dumps({"cell_id": "202", "yield_time_ms": 6000})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "w1", "output": [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": json.dumps({"wall_time_seconds": 4.0, "session_id": 26717})}]}},
        ]
        with self.transcript.open("a") as out:
            out.writelines(json.dumps(record) + "\n" for record in records)
        self.assertIn("26717", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

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

    def test_codex_subagent_is_recorded_until_it_finishes(self):
        started = self.item({"type": "SubAgentActivity", "kind": "started", "agent_path": "/root/worker",
                             "agent_thread_id": "thread-1", "model": "gpt-6.1-terra"})
        with self.transcript.open("a") as out:
            out.write(json.dumps(started) + "\n")
        self.assertIn("/root/worker", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))
        completed = self.item({"type": "SubAgentActivity", "kind": "completed", "agent_path": "/root/worker",
                               "agent_thread_id": "thread-1"})
        with self.transcript.open("a") as out:
            out.write(json.dumps(completed) + "\n")
        self.assertNotIn("/root/worker", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_followup_interaction_records_a_descendant_until_it_finishes(self):
        followup = {"type": "response_item", "payload": {"type": "function_call", "name": "followup_task",
                    "namespace": "collaboration", "call_id": "followup-1",
                    "arguments": json.dumps({"target": "/root/reused", "message": "resume work"})}}
        interacted = self.item({"type": "SubAgentActivity", "kind": "interacted", "agent_path": "/root/reused",
                                "agent_thread_id": "thread-2", "model": "gpt-6.1-terra"})
        with self.transcript.open("a") as out:
            out.write(json.dumps(followup) + "\n" + json.dumps(interacted) + "\n")
        self.assertIn("/root/reused", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))
        completed = self.item({"type": "SubAgentActivity", "kind": "completed", "agent_path": "/root/reused"})
        with self.transcript.open("a") as out:
            out.write(json.dumps(completed) + "\n")
        self.assertNotIn("/root/reused", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))
        # A stale followup must not make a later uncorrelated interaction live.
        with self.transcript.open("a") as out:
            out.write(json.dumps(interacted) + "\n")
        self.assertNotIn("/root/reused", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_message_only_and_self_interactions_do_not_revive_agents(self):
        records = [
            {"type": "session_meta", "payload": {"id": "parent-thread", "source": {"subagent": {
                "thread_spawn": {"agent_path": "/root/parent", "parent_thread_id": "root-thread", "depth": 1}}}}},
            self.item({"type": "SubAgentActivity", "kind": "started", "agent_path": "/root/parent/child",
                       "agent_thread_id": "child-thread"}),
            self.item({"type": "SubAgentActivity", "kind": "completed", "agent_path": "/root/parent/child",
                       "agent_thread_id": "child-thread"}),
            {"type": "response_item", "payload": {"type": "function_call", "name": "send_message",
             "namespace": "collaboration", "call_id": "message-1",
             "arguments": json.dumps({"target": "/root/parent/child", "message": "status"})}},
            self.item({"type": "SubAgentActivity", "kind": "interacted", "agent_path": "/root/parent/child",
                       "agent_thread_id": "child-thread"}),
            {"type": "response_item", "payload": {"type": "function_call", "name": "followup_task",
             "namespace": "collaboration", "call_id": "self-followup",
             "arguments": json.dumps({"target": "/root/parent", "message": "continue"})}},
            self.item({"type": "SubAgentActivity", "kind": "interacted", "agent_path": "/root/parent",
                       "agent_thread_id": "parent-thread"}),
            {"type": "response_item", "payload": {"type": "function_call", "name": "followup_task",
             "namespace": "collaboration", "call_id": "sibling-followup",
             "arguments": json.dumps({"target": "/root/sibling", "message": "continue"})}},
            self.item({"type": "SubAgentActivity", "kind": "interacted", "agent_path": "/root/sibling",
                       "agent_thread_id": "sibling-thread"}),
        ]
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.assertEqual(dict(checkpoint.running(checkpoint.scan(str(self.transcript)))), {})

    def test_collab_tool_events_distinguish_followup_from_message(self):
        def activity(kind):
            return self.item({"type": "SubAgentActivity", "kind": kind, "agent_path": "/root/child",
                              "agent_thread_id": "child-thread"})
        def action(tool):
            return {"type": "event_msg", "payload": {"type": "item_started", "item": {
                "type": "CollabAgentToolCall", "tool": tool, "receiver_thread_ids": ["child-thread"]}}}
        records = [activity("completed"), action("followupTask"), activity("interacted")]
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.assertIn("/root/child", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))
        with self.transcript.open("a") as out:
            out.writelines(json.dumps(record) + "\n" for record in
                           (activity("completed"), action("sendMessage"), activity("interacted")))
        self.assertNotIn("/root/child", dict(checkpoint.running(checkpoint.scan(str(self.transcript)))))

    def test_unmatched_followup_does_not_leak_into_the_next_turn(self):
        followup = {"type": "response_item", "payload": {"type": "function_call", "name": "followup_task",
                    "call_id": "followup-1", "arguments": json.dumps({"target": "/root/child"})}}
        turn_end = {"type": "event_msg", "payload": {"type": "task_complete"}}
        interaction = self.item({"type": "SubAgentActivity", "kind": "interacted", "agent_path": "/root/child",
                                 "agent_thread_id": "child-thread"})
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in
                                           (followup, turn_end, interaction)))
        self.assertEqual(dict(checkpoint.running(checkpoint.scan(str(self.transcript)))), {})

    def test_documented_hooks_run_from_a_marketplace_cache_install(self):
        repo = Path(__file__).resolve().parents[1]
        readme = (repo / "tokenmaxxing" / "README.md").read_text()
        section = readme.split("### Codex compaction hooks", 1)[1]
        example = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))
        fake_home = self.root / "marketplace-home"
        plugin = fake_home / ".claude/plugins/cache/gutcheck/tokenmaxxing/0.2.test"
        shutil.copytree(repo / "tokenmaxxing", plugin, symlinks=False,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        python = fake_home / ".local/share/gutcheck/venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        # Exercise the real shim and handlers without using a keychain or network.
        (plugin / "src/sitecustomize.py").write_text("from gutcheck_core import jev\njev.have_key = lambda: False\n")
        config_dir = fake_home / ".config/tokenmaxxing"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text('[transitions]\nmode = "off"\n')
        transcript = fake_home / "rollout.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(fake_home)}}) + "\n" +
                              json.dumps(self.message("user", "Always preserve MARKETPLACE-CHECKPOINT.")) + "\n")
        payload = {"session_id": "marketplace-test", "transcript_path": str(transcript), "cwd": str(fake_home),
                   "hook_event_name": "PreCompact", "trigger": "manual"}
        for event in ("PreCompact", "SessionStart"):
            command = example["hooks"][event][0]["hooks"][0]["command"]
            command = command.replace("$HOME", str(fake_home)).replace("<version>", "0.2.test")
            if event == "SessionStart":
                payload = {**payload, "hook_event_name": "SessionStart", "source": "compact"}
            result = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True,
                                    text=True, timeout=20)
            with self.subTest(event=event):
                self.assertEqual(result.returncode, 0, result.stderr)
                if event == "PreCompact":
                    output = fake_home / ".local/state/tokenmaxxing-codex/data/checkpoints/marketplace-test.md"
                    self.assertIn("MARKETPLACE-CHECKPOINT", output.read_text())
                else:
                    self.assertIn("MARKETPLACE-CHECKPOINT",
                                  json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_unrelated_outputs_and_malformed_records_are_ignored(self):
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                "call_id": "other", "input": "text('not an exec command')"}}
        result = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "other",
                  "output": [{"type": "input_text", "text": json.dumps({"session_id": 123,
                                                                          "wall_time_seconds": 1.0})}]}}
        with self.transcript.open("a") as out:
            for record in (call, result, [], {"type": "session_meta", "payload": "invalid"},
                           {"type": "response_item", "payload": {"type": "custom_tool_call", "input": 12}},
                           {"type": "response_item", "payload": {"type": "function_call_output", "call_id": []}},
                           self.item({"type": "CommandExecution", "id": [], "status": "completed",
                                      "process_id": {}}),
                           self.item({"type": "SubAgentActivity", "kind": "completed", "agent_path": []}),
                           {"type": "event_msg", "payload": {"type": "item_completed", "item": "invalid"}}):
                out.write(json.dumps(record) + "\n")
        state = checkpoint.scan(str(self.transcript))
        self.assertNotIn("123", dict(checkpoint.running(state)))
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

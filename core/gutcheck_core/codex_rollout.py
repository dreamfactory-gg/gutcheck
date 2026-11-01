"""Translate Codex rollout records used by checkpoints into the existing hook record shape."""
import json
import os
import re
from urllib.parse import urlsplit
from urllib.request import url2pathname


_GENERATED_USER = re.compile(
    r"^\s*(?:# AGENTS\.md instructions\b|<(?:environment_context|user_instructions|context_window_guidance|"
    r"permissions|recommended_plugins|codex_internal_context|goal_context|user_shell_command|turn_aborted)\b|"
    r"The following is the Codex agent history)"
)


def _path(value):
    if isinstance(value, str) and value.startswith("file://"):
        parsed = urlsplit(value)
        if not parsed.netloc or parsed.netloc == "localhost":
            return url2pathname(parsed.path)
    return value


def _file_path(value, cwd):
    value = _path(value)
    if isinstance(value, str) and not os.path.isabs(value):
        return os.path.normpath(os.path.join(cwd, value)) if cwd else None
    return value


def normalize(record, cwd=None):
    """Return one Claude-shaped message for a supported Codex record, otherwise None."""
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return None
    payload, timestamp = record["payload"], record.get("timestamp")
    if record.get("type") == "response_item" and payload.get("type") == "function_call" \
            and payload.get("name") == "update_plan":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except (TypeError, ValueError):
            return None
        plan = args.get("plan") if isinstance(args, dict) else None
        if not isinstance(plan, list):
            return None
        todos = [{"content": step["step"], "status": step.get("status")} for step in plan
                 if isinstance(step, dict) and isinstance(step.get("step"), str)]
        return {"type": "assistant", "timestamp": timestamp, "message": {"content": [
            {"type": "tool_use", "id": payload.get("call_id"), "name": "TodoWrite", "input": {"todos": todos}}]}}
    if record.get("type") == "response_item" and payload.get("type") == "message":
        role = payload.get("role")
        if role not in ("user", "assistant") or not isinstance(payload.get("content"), list):
            return None
        blocks = [{"type": "text", "text": block["text"]} for block in payload["content"]
                  if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
                  and isinstance(block.get("text"), str) and block["text"]]
        text = "\n".join(block["text"] for block in blocks)
        if not text.strip() or (role == "user" and _GENERATED_USER.match(text)):
            return None
        return {"type": role, "timestamp": timestamp, "message": {"content": blocks}}

    if record.get("type") != "event_msg" or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    cwd = _path(item.get("cwd")) or cwd
    blocks = []
    if item.get("type") == "CommandExecution":
        command = item.get("command")
        if isinstance(command, list):
            command = command[-1] if command else None
        if isinstance(command, str) and command:
            blocks.append({"type": "tool_use", "id": item.get("id"), "name": "Bash",
                           "input": {"command": command}})
        parsed = item.get("parsed_cmd")
        for index, part in enumerate(parsed if isinstance(parsed, list) else []):
            if isinstance(part, dict) and part.get("type") == "read" and isinstance(part.get("path"), str):
                path = _file_path(part["path"], cwd)
                if path:
                    blocks.append({"type": "tool_use", "id": f"{item.get('id')}:read:{index}", "name": "Read",
                                   "input": {"file_path": path}})
    elif item.get("type") == "FileChange" and item.get("status") == "completed":
        changes = item.get("changes")
        if isinstance(changes, dict):
            for index, path in enumerate(changes):
                if isinstance(path, str):
                    path = _file_path(path, cwd)
                    if path:
                        blocks.append({"type": "tool_use", "id": f"{item.get('id')}:edit:{index}", "name": "Edit",
                                       "input": {"file_path": path}})
    if not blocks:
        return None
    return {"type": "assistant", "timestamp": timestamp, "cwd": cwd,
            "message": {"content": blocks}}


def _result_texts(record):
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict) or payload.get("type") not in ("custom_tool_call_output", "function_call_output"):
        return []
    output = payload.get("output")
    return [output] if isinstance(output, str) else [part.get("text", "") for part in output
                                                     if isinstance(part, dict)] if isinstance(output, list) else []


def yielded_cells(record):
    return [cell for text in _result_texts(record) if isinstance(text, str)
            for cell in re.findall(r"\bScript running with cell ID\s+([\w-]+)", text)]


def running_sessions(record):
    """Extract yielded unified-exec session IDs from a Codex tool result."""
    found = []

    def collect(value):
        if isinstance(value, dict):
            session = value.get("session_id")
            if isinstance(session, (str, int)) and not isinstance(session, bool) and \
                    "wall_time_seconds" in value and "exit_code" not in value:
                found.append(str(session))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for text in _result_texts(record):
        if not isinstance(text, str):
            continue
        try:
            collect(json.loads(text))
        except ValueError:
            found.extend(re.findall(r"\bProcess running with session ID\s+(\d+)", text))
    return list(dict.fromkeys(found))

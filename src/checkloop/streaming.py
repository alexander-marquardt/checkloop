"""JSONL stream parsing and event display for Claude Code subprocess output."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from checkloop.terminal import BLUE, DIM, GREEN, RESET, _format_duration

logger = logging.getLogger(__name__)

_BASH_DISPLAY_LIMIT = 80  # max chars shown for bash commands in tool summaries

_FILE_PATH_TOOL_NAMES: set[str] = {"read", "read_file", "edit", "edit_file", "write", "write_file"}


# --- Tool-use summaries ------------------------------------------------------

def _summarise_tool_use(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Return a short human-readable summary for a tool-use event."""
    normalized_name = tool_name.lower()
    if normalized_name in _FILE_PATH_TOOL_NAMES and "file_path" in tool_input:
        return f" {tool_input['file_path']}"
    if normalized_name == "bash" and "command" in tool_input:
        command = str(tool_input["command"])
        if len(command) > _BASH_DISPLAY_LIMIT:
            return f" $ {command[:_BASH_DISPLAY_LIMIT - 3]}..."
        return f" $ {command}"
    if normalized_name == "glob" and "pattern" in tool_input:
        return f" {tool_input['pattern']}"
    if normalized_name == "grep" and "pattern" in tool_input:
        return f" /{tool_input['pattern']}/"
    return ""


# --- Event printers -----------------------------------------------------------

def _print_assistant_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print text blocks from an assistant response event."""
    content = event.get("message", {}).get("content") or []
    text_blocks = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    for text in text_blocks:
        if text.strip():
            print(f"{elapsed_prefix}{text}")


def _print_tool_use_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print a tool invocation with its name and a short summary of inputs."""
    tool_name = event.get("tool", event.get("name", "unknown"))
    detail = _summarise_tool_use(tool_name, event.get("input") or {})
    print(f"{elapsed_prefix}{BLUE}[{tool_name}]{RESET}{detail}")


def _print_system_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print a system-level message (e.g. initialisation status)."""
    system_message = event.get("message", "")
    if system_message:
        print(f"{elapsed_prefix}{DIM}{system_message}{RESET}")


def _print_result_event(event: dict[str, Any], elapsed_prefix: str) -> None:
    """Print the final result summary from a completed check."""
    result_text = event.get("result", "")
    if result_text:
        print(f"\n{elapsed_prefix}{GREEN}--- Result ---{RESET}")
        print(result_text)


# Type alias for event handler functions used by _print_event dispatch.
_EventHandler = Callable[[dict[str, Any], str], None]

# Maps stream-json event types to their display handlers.
_EVENT_TYPE_HANDLERS: dict[str, _EventHandler] = {
    "assistant": _print_assistant_event,
    "tool_use": _print_tool_use_event,
    "system": _print_system_event,
    "result": _print_result_event,
}


def _print_event(event: dict[str, Any], pass_start_time: float) -> None:
    """Parse a stream-json event and dispatch to the appropriate printer."""
    event_type = event.get("type", "")
    printer = _EVENT_TYPE_HANDLERS.get(event_type)
    if printer is None:
        return
    elapsed_prefix = f"{DIM}[{_format_duration(time.time() - pass_start_time)}]{RESET} "
    printer(event, elapsed_prefix)


def _process_jsonl_buffer(
    output_buffer: bytearray,
    pass_start_time: float,
    debug: bool,
) -> bytearray:
    """Process complete JSONL lines from the buffer, return the remainder.

    Parses each complete line as JSON and dispatches to the appropriate
    event printer.  Incomplete trailing data is left in the buffer for
    the next call.
    """
    # Find the last complete line boundary. Everything before it can be parsed;
    # everything after stays in the buffer for the next call.
    # This single-delete approach avoids O(n²) cost from repeated del [:n].
    last_newline = output_buffer.rfind(b"\n")
    if last_newline == -1:
        return output_buffer  # no complete line yet
    complete_lines_bytes = bytes(output_buffer[:last_newline])
    del output_buffer[:last_newline + 1]
    for line_bytes in complete_lines_bytes.split(b"\n"):
        line_str = line_bytes.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue
        try:
            _print_event(json.loads(line_str), pass_start_time)
        except json.JSONDecodeError:
            if debug:
                print(f"{DIM}{line_str}{RESET}")
    return output_buffer

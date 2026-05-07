"""AI backend subprocess management — Claude and Codex."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def _prepend_juvenal_bin_to_path(proc_env: dict[str, str]) -> None:
    """Prepend the running juvenal's venv bin dir to PATH so spawned shells find the same `python`/`juvenal`.

    Without this, an agent invoking `python -m juvenal.plan_validation` from a `pipx run`-launched
    juvenal would resolve `python` to the system interpreter, which often has a stale juvenal in
    `~/.local/lib/...` and rejects flags added in newer releases.
    """
    bin_dir = str(Path(sys.executable).resolve().parent)
    existing = proc_env.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    if parts and parts[0] == bin_dir:
        return
    proc_env["PATH"] = os.pathsep.join([bin_dir, *[p for p in parts if p != bin_dir]])


@dataclass
class AgentResult:
    """Result from running an agent subprocess."""

    exit_code: int
    output: str  # final assistant messages
    transcript: str  # full transcript including tool calls
    duration: float  # seconds
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str | None = None


@dataclass
class InteractiveResult:
    """Result from an interactive terminal session."""

    session_id: str
    exit_code: int


class Backend(ABC):
    """Abstract base for AI agent backends."""

    def __init__(self):
        self._active_procs: list[subprocess.Popen] = []

    def kill_active(self) -> None:
        """Kill all active agent subprocesses."""
        for proc in self._active_procs:
            try:
                proc.kill()
                proc.wait()
            except (ProcessLookupError, OSError):
                pass
        self._active_procs.clear()

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run_agent(
        self,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        """Run an agent with the given prompt. Returns AgentResult."""
        ...

    def resume_agent(
        self,
        session_id: str,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        """Resume an existing agent session. Default falls back to run_agent."""
        return self.run_agent(prompt, working_dir, display_callback, timeout, env)

    def run_interactive(
        self,
        prompt: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> InteractiveResult:
        """Run an interactive terminal session. Default raises NotImplementedError."""
        raise NotImplementedError(f"{self.name()} backend does not support interactive mode")


class ClaudeBackend(Backend):
    """Claude CLI backend using stream-json output."""

    def name(self) -> str:
        return "claude"

    def run_agent(
        self,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        session_id = str(uuid.uuid4())
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--verbose",
            "--session-id",
            session_id,
            prompt,
        ]
        result = self._run_claude_process(cmd, working_dir, display_callback, timeout, env)
        result.session_id = session_id
        return result

    def resume_agent(
        self,
        session_id: str,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--verbose",
            "--resume",
            session_id,
            prompt,
        ]
        result = self._run_claude_process(cmd, working_dir, display_callback, timeout, env)
        result.session_id = session_id
        return result

    def run_interactive(
        self,
        prompt: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> InteractiveResult:
        session_id = str(uuid.uuid4())
        cmd = [
            "claude",
            "--session-id",
            session_id,
            "--dangerously-skip-permissions",
            "--verbose",
            prompt,
        ]
        proc_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if env:
            proc_env.update(env)
        _prepend_juvenal_bin_to_path(proc_env)

        # Save terminal state before the interactive TUI takes over
        saved_termios = None
        try:
            import termios

            if sys.stdin.isatty():
                saved_termios = termios.tcgetattr(sys.stdin)
        except (ImportError, termios.error):
            pass

        proc = subprocess.Popen(cmd, cwd=working_dir, env=proc_env)
        self._active_procs.append(proc)
        try:
            proc.wait()
        finally:
            if proc in self._active_procs:
                self._active_procs.remove(proc)
            # Restore terminal state so Ctrl-C and normal input work again
            if saved_termios is not None:
                try:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved_termios)
                except termios.error:
                    pass
            # Reclaim foreground process group so Ctrl-C reaches us
            try:
                if sys.stdin.isatty():
                    os.tcsetpgrp(sys.stdin.fileno(), os.getpgrp())
            except OSError:
                pass

        return InteractiveResult(session_id=session_id, exit_code=proc.returncode)

    def _run_claude_process(
        self,
        cmd: list[str],
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        # Strip CLAUDECODE env var so juvenal can be invoked from inside Claude Code
        proc_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if env:
            proc_env.update(env)
        _prepend_juvenal_bin_to_path(proc_env)

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=proc_env,
        )
        self._active_procs.append(proc)

        transcript_lines: list[str] = []
        assistant_messages: list[str] = []
        total_input_tokens = 0
        total_output_tokens = 0

        # Drain stderr in a background thread. A blocking `proc.stderr.read()`
        # after the stdout loop deadlocks when claude has spawned a child that
        # inherited stderr (e.g. a long-running Docker / build subprocess) —
        # the kernel won't deliver EOF until every fd referencing the pipe is
        # closed, so we'd hang indefinitely waiting on a grandchild.
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            try:
                while True:
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)
            except (ValueError, OSError):
                # Pipe closed from the main thread; exit cleanly.
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        try:
            for raw_line in proc.stdout:
                if timeout and (time.time() - start) > timeout:
                    proc.kill()
                    proc.wait()
                    return AgentResult(
                        exit_code=1,
                        output=f"Agent timed out after {timeout}s",
                        transcript="\n".join(transcript_lines),
                        duration=time.time() - start,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    )
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                event = _parse_json_event(line)
                if event:
                    display_text, assistant_text = _process_claude_event(event)
                    inp, out = _extract_claude_tokens(event)
                    total_input_tokens += inp
                    total_output_tokens += out
                    if display_text:
                        transcript_lines.append(display_text)
                        if display_callback:
                            display_callback(display_text)
                    if assistant_text:
                        assistant_messages.append(assistant_text)
                else:
                    transcript_lines.append(line)
                    if display_callback:
                        display_callback(line)
        except Exception:
            proc.kill()
            proc.wait()
            raise

        returncode = proc.wait()
        duration = time.time() - start
        # Give the drain thread a brief grace period to flush buffered stderr.
        # If a grandchild still holds the pipe open, close our end to unblock
        # the read so we don't leak threads on subsequent phases.
        stderr_thread.join(timeout=2.0)
        if stderr_thread.is_alive():
            try:
                proc.stderr.close()
            except Exception:
                pass
            stderr_thread.join(timeout=1.0)
        stderr_output = "".join(stderr_chunks)
        if proc in self._active_procs:
            self._active_procs.remove(proc)

        if stderr_output:
            transcript_lines.append(f"[stderr] {stderr_output}")

        output = "\n".join(assistant_messages)
        if returncode != 0 and not output:
            output = stderr_output or "\n".join(transcript_lines)

        return AgentResult(
            exit_code=returncode,
            output=output,
            transcript="\n".join(transcript_lines),
            duration=duration,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )


class CodexBackend(Backend):
    """Codex CLI backend using NDJSON streaming."""

    def name(self) -> str:
        return "codex"

    def run_agent(
        self,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        cmd = [
            "npx",
            "@openai/codex@latest",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-",
        ]
        return self._run_codex_process(cmd, working_dir, display_callback, timeout, env, stdin_input=prompt)

    def resume_agent(
        self,
        session_id: str,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        cmd = [
            "npx",
            "@openai/codex@latest",
            "exec",
            "resume",
            session_id,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-",
        ]
        result = self._run_codex_process(cmd, working_dir, display_callback, timeout, env, stdin_input=prompt)
        result.session_id = session_id
        return result

    def _run_codex_process(
        self,
        cmd: list[str],
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        stdin_input: str | None = None,
    ) -> AgentResult:
        proc_env = dict(os.environ)
        if env:
            proc_env.update(env)
        _prepend_juvenal_bin_to_path(proc_env)

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdin=subprocess.PIPE if stdin_input else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=proc_env,
        )
        self._active_procs.append(proc)

        if stdin_input:
            proc.stdin.write(stdin_input)
            proc.stdin.close()

        transcript_lines: list[str] = []
        assistant_messages: list[str] = []
        total_input_tokens = 0
        total_output_tokens = 0
        thread_id: str | None = None

        try:
            for raw_line in proc.stdout:
                if timeout and (time.time() - start) > timeout:
                    proc.kill()
                    proc.wait()
                    return AgentResult(
                        exit_code=1,
                        output=f"Agent timed out after {timeout}s",
                        transcript="\n".join(transcript_lines),
                        duration=time.time() - start,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    )
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                event = _parse_json_event(line)
                if event:
                    # Capture thread_id from thread.started event
                    if event.get("type") == "thread.started" and "thread_id" in event:
                        thread_id = event["thread_id"]
                    display_text, assistant_text = _process_codex_event(event)
                    inp, out = _extract_codex_tokens(event)
                    total_input_tokens += inp
                    total_output_tokens += out
                    if display_text:
                        transcript_lines.append(display_text)
                        if display_callback:
                            display_callback(display_text)
                    if assistant_text:
                        assistant_messages.append(assistant_text)
                else:
                    transcript_lines.append(line)
                    if display_callback:
                        display_callback(line)
        except Exception:
            proc.kill()
            proc.wait()
            raise

        returncode = proc.wait()
        duration = time.time() - start
        if proc in self._active_procs:
            self._active_procs.remove(proc)

        output = "\n".join(assistant_messages)
        if returncode != 0 and not output:
            output = "\n".join(transcript_lines)

        return AgentResult(
            exit_code=returncode,
            output=output,
            transcript="\n".join(transcript_lines),
            duration=duration,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            session_id=thread_id,
        )


def create_backend(name: str) -> Backend:
    """Factory to create a backend by name."""
    if name == "claude":
        return ClaudeBackend()
    elif name == "codex":
        return CodexBackend()
    else:
        raise ValueError(f"Unknown backend: {name!r}. Must be 'claude' or 'codex'.")


def _parse_json_event(line: str) -> dict | None:
    """Try to parse a line as a JSON event."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _process_claude_event(event: dict) -> tuple[str, str]:
    """Process a Claude stream-json event.

    Returns (display_text, assistant_text).
    """
    event_type = event.get("type", "")

    # An `assistant` event's `message.content` is a list of blocks:
    # text, tool_use, thinking. We surface text (Claude's prose) and
    # thinking (extended-thinking summaries when enabled) so the live
    # buffer reflects Claude's reasoning. tool_use / tool_result events
    # are intentionally skipped — they're noise in the buffer.
    if event_type == "assistant":
        message = event.get("message", "")
        if isinstance(message, dict):
            content = message.get("content", "")
        else:
            content = message
        display_parts: list[str] = []
        assistant_parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    txt = block.get("text", "")
                    if txt:
                        display_parts.append(txt)
                        assistant_parts.append(txt)
                elif btype == "thinking":
                    thinking = block.get("thinking", "") or block.get("text", "")
                    if thinking:
                        display_parts.append(f"[thinking] {_truncate(thinking, 200)}")
        elif isinstance(content, str) and content:
            display_parts.append(content)
            assistant_parts.append(content)
        return "\n".join(display_parts), "\n".join(assistant_parts)

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        text = delta.get("text", "")
        return text, ""

    if event_type == "result":
        # Final result message — Claude always echoes the last assistant text here,
        # and prior `assistant` events have already streamed it to the display.
        # Keep it in assistant_text as a fallback for `output`, but don't re-emit
        # to display_text (would duplicate the verdict / final message in the buffer).
        text = event.get("result", "")
        return "", text

    if event_type == "system":
        msg = event.get("message", "")
        return f"[system] {msg}" if msg else "", ""

    return "", ""


def _truncate(text: str, limit: int) -> str:
    """Single-line, length-capped rendering for live display."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _process_codex_event(event: dict) -> tuple[str, str]:
    """Process a Codex NDJSON event.

    Returns (display_text, assistant_text).
    """
    event_type = event.get("type", "")

    if event_type == "item.completed":
        item = event.get("item", {})
        item_type = item.get("type", "")
        text = item.get("text", "")

        if item_type == "reasoning":
            return f"[thinking] {text[:200]}", ""
        elif item_type == "agent_message":
            return text, text
        elif item_type == "tool_call":
            tool_name = item.get("name", "unknown")
            return f"[tool: {tool_name}]", ""
        elif text:
            return text, text
        return "", ""

    if event_type == "turn.completed":
        usage = event.get("usage", {})
        if usage:
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            return f"[tokens: {inp} in, {out} out]", ""
        return "", ""

    return "", ""


def _extract_claude_tokens(event: dict) -> tuple[int, int]:
    """Extract token usage from a Claude event. Returns (input_tokens, output_tokens)."""
    if event.get("type") == "result":
        usage = event.get("usage", {})
        if usage:
            return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return 0, 0


def _extract_codex_tokens(event: dict) -> tuple[int, int]:
    """Extract token usage from a Codex event. Returns (input_tokens, output_tokens)."""
    if event.get("type") == "turn.completed":
        usage = event.get("usage", {})
        if usage:
            return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return 0, 0

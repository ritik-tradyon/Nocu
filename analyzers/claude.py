"""Claude Code CLI wrapper for deep root cause analysis.

Used for complex queries (memory spikes, latency investigations) that benefit
from Claude's stronger code reasoning capabilities.

Requires Claude Code CLI to be installed and authenticated:
  npm install -g @anthropic-ai/claude-code
  claude login
"""

import subprocess
import json
import os
import shutil
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nocu.claude")


DEEP_RCA_PROMPT = """You are investigating a production issue in a Python {framework} service called "{service_name}".

QUESTION: {question}

## Production Data from New Relic
{observability_data}

## Service Code Structure
Endpoints:
{endpoints_summary}

## Relevant Code Files
{relevant_code}

---

Return a short developer answer in plain text:
- Max 6 bullets total.
- Each bullet must be 1 sentence.
- Order: likely cause, evidence, code path, fix, prevention.
- Mention files and line numbers only when grounded in the provided code.
- No markdown headers, no bold, no intro, no conclusion, no fluff.
"""


class ClaudeAnalyzer:
    """Deep analysis using Claude Code CLI."""

    def __init__(
        self,
        timeout_seconds: int = 120,
        enabled: bool = True,
        cli_path: Optional[str] = None,
    ):
        self.timeout = timeout_seconds
        self.enabled = enabled
        self.cli_path = cli_path or "claude"
        self._verified = False
        self._node_bin, self._cli_script = self._resolve_invocation(self.cli_path)

    def _resolve_invocation(self, cli_path: str) -> tuple[str, str]:
        """Resolve the node binary and cli.js absolute paths.

        Returns (node_binary, cli_js_path) so subprocess can be called as
        [node_binary, cli_js_path, ...] with no dependency on the process PATH.
        """
        node_candidates = [
            shutil.which("node"),
            "/home/linuxbrew/.linuxbrew/bin/node",
            "/usr/local/bin/node",
            "/usr/bin/node",
        ]
        node_bin = next((p for p in node_candidates if p and os.path.isfile(p)), None)

        cli_script = str(Path(cli_path).resolve())

        return node_bin, cli_script

    def _build_cmd(self, args: list[str]) -> list[str]:
        """Build the subprocess command using resolved absolute paths."""
        if self._node_bin and self._cli_script:
            return [self._node_bin, self._cli_script] + args
        return [self.cli_path] + args

    def is_available(self) -> bool:
        """Check if Claude Code CLI is installed and accessible."""
        if not self.enabled:
            return False
        try:
            result = subprocess.run(
                self._build_cmd(["--version"]),
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"},
            )
            self._verified = result.returncode == 0
            return self._verified
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def analyze(
        self,
        question: str,
        service_name: str,
        framework: str,
        observability_data: str,
        endpoints_summary: str,
        relevant_code: str,
        repo_path: Optional[str] = None,
    ) -> str:
        """Run deep analysis using Claude Code in non-interactive mode.

        Args:
            question: The user's original question.
            service_name: Name of the service being analyzed.
            framework: Web framework (fastapi, flask, django).
            observability_data: Formatted New Relic data.
            endpoints_summary: Summary of service endpoints.
            relevant_code: Relevant source code snippets.
            repo_path: Path to the repo (Claude Code will run in this directory).

        Returns:
            Analysis text from Claude Code.
        """
        if not self.enabled:
            return "Claude Code analysis is disabled in configuration."

        if not self._verified and not self.is_available():
            return (f"Claude Code CLI is not available at '{self.cli_path}'. "
                    "Install it with: npm install -g @anthropic-ai/claude-code && claude login")

        prompt = DEEP_RCA_PROMPT.format(
            question=question,
            service_name=service_name,
            framework=framework,
            observability_data=observability_data,
            endpoints_summary=endpoints_summary or "(no endpoints indexed)",
            relevant_code=relevant_code or "(no relevant code loaded)",
        )

        try:
            cmd = self._build_cmd([
                "-p",
                prompt,
                "--verbose",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
            ])
            start_time = time.time()
            logger.info(
                "Claude start service=%s repo=%s timeout=%ss question_chars=%d observability_chars=%d endpoints_chars=%d code_chars=%d prompt_chars=%d",
                service_name,
                repo_path or "(none)",
                self.timeout,
                len(question or ""),
                len(observability_data or ""),
                len(endpoints_summary or ""),
                len(relevant_code or ""),
                len(prompt),
            )

            effective_cwd = repo_path if (repo_path and os.path.isdir(repo_path)) else None
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=effective_cwd,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"},
            )
            elapsed = time.time() - start_time
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            self._log_stream_events(stdout, service_name, elapsed)

            if result.returncode == 0:
                output = self._extract_text_output(stdout)
                logger.info(
                    "Claude finished service=%s elapsed=%.1fs returncode=%s output_chars=%d stderr_chars=%d",
                    service_name,
                    elapsed,
                    result.returncode,
                    len(output),
                    len(stderr),
                )
                if output:
                    return output
                return "Claude Code returned empty output. Check authentication with: claude status"
            else:
                error_output = self._extract_error_output(stdout, stderr)
                logger.warning(
                    "Claude failed service=%s elapsed=%.1fs returncode=%s error=%s stderr=%s",
                    service_name,
                    elapsed,
                    result.returncode,
                    self._truncate(error_output),
                    self._truncate(stderr),
                )
                lower_error = error_output.lower()
                if "rate limit" in lower_error or "usage limit" in lower_error:
                    return error_output
                return f"Claude Code error: {error_output}"

        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - start_time
            partial_stdout = (e.stdout or "").strip()
            partial_stderr = (e.stderr or "").strip()
            self._log_stream_events(partial_stdout, service_name, elapsed, timed_out=True)
            logger.warning(
                "Claude timeout service=%s elapsed=%.1fs timeout=%ss partial_stdout_chars=%d partial_stderr_chars=%d partial_stderr=%s",
                service_name,
                elapsed,
                self.timeout,
                len(partial_stdout),
                len(partial_stderr),
                self._truncate(partial_stderr),
            )
            return (f"Claude Code analysis timed out after {self.timeout}s. "
                    "The query may be too complex. Try a more specific question.")
        except FileNotFoundError as e:
            return f"Claude Code launch failed: {e}"
        except Exception as e:
            return f"Claude Code error: {e}"

    def _log_stream_events(
        self,
        stdout: str,
        service_name: str,
        elapsed: float,
        timed_out: bool = False,
    ) -> None:
        """Log Claude stream-json event flow for diagnosis."""
        if not stdout:
            logger.info(
                "Claude stream service=%s elapsed=%.1fs events=0 timed_out=%s",
                service_name,
                elapsed,
                timed_out,
            )
            return

        event_summaries = []
        raw_lines = 0

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            raw_lines += 1
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                event_summaries.append(f"raw:{self._truncate(line, 80)}")
                continue

            event_type = payload.get("type", "unknown")
            summary = self._event_summary(payload)
            event_summaries.append(f"{event_type}:{summary}" if summary else event_type)

        logger.info(
            "Claude stream service=%s elapsed=%.1fs events=%d timed_out=%s flow=%s",
            service_name,
            elapsed,
            raw_lines,
            timed_out,
            " | ".join(event_summaries[:20]),
        )

    def _extract_text_output(self, stdout: str) -> str:
        """Extract the best text answer from Claude stream-json output."""
        if not stdout:
            return ""

        text_parts = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            candidate = self._extract_text_candidate(payload)
            if candidate:
                text_parts.append(candidate)

        if not text_parts:
            return ""

        # Deduplicate repeated partials while preserving order.
        seen = set()
        unique_parts = []
        for part in text_parts:
            if part not in seen:
                seen.add(part)
                unique_parts.append(part)

        return "\n".join(unique_parts).strip()

    def _extract_error_output(self, stdout: str, stderr: str) -> str:
        """Extract a useful error message from Claude stream-json output."""
        if stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = payload.get("type", "")

                if event_type == "rate_limit_event":
                    message = self._extract_rate_limit_message(payload)
                    if message:
                        return message

                if event_type == "error":
                    message = self._extract_payload_message(payload)
                    if message:
                        return message

                if event_type == "result":
                    for key in ("error", "message", "result"):
                        value = payload.get(key)
                        if isinstance(value, str) and value.strip() and value.strip().lower() != "success":
                            return value.strip()

        if stderr:
            return stderr
        return "Unknown Claude Code failure"

    def _extract_text_candidate(self, payload: dict) -> str:
        """Pull human-readable text from a Claude stream-json event."""
        event_type = payload.get("type", "")

        if event_type == "result":
            return str(payload.get("result", "")).strip()

        if event_type in {"assistant", "message", "content_block_delta"}:
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                if parts:
                    return "\n".join(parts)

        delta = payload.get("delta")
        if isinstance(delta, dict):
            text = delta.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

        return ""

    def _event_summary(self, payload: dict) -> str:
        """Build a short log summary for a Claude stream-json event."""
        for key in ("subtype", "role", "session_id"):
            value = payload.get(key)
            if value:
                return self._truncate(str(value), 40)

        text = self._extract_text_candidate(payload)
        if text:
            return self._truncate(text.replace("\n", " "), 60)

        return ""

    def _extract_rate_limit_message(self, payload: dict) -> str:
        """Build a readable message from a Claude rate limit event."""
        parts = []
        for key in (
            "message",
            "error",
            "type",
            "reset_at",
            "retry_after",
            "retry_after_seconds",
        ):
            value = payload.get(key)
            if value and value != "rate_limit_event":
                parts.append(f"{key}={value}")

        for nested_key in ("rate_limit", "details"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                for key, value in nested.items():
                    if value:
                        parts.append(f"{key}={value}")

        if parts:
            return "Claude Code rate limit reached: " + ", ".join(parts)
        return "Claude Code rate limit reached. Try again later."

    def _extract_payload_message(self, payload: dict) -> str:
        """Extract a human-readable message from a stream-json payload."""
        for key in ("message", "error", "details"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for nested_key in ("message", "error"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, str) and nested_value.strip():
                        return nested_value.strip()
        return ""

    def _truncate(self, text: str, limit: int = 200) -> str:
        """Truncate long log values."""
        if len(text) <= limit:
            return text
        return text[: limit - 4] + " ..."

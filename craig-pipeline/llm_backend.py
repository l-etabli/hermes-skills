from __future__ import annotations

import json
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LlmBackendConfig:
    hermes_cli: pathlib.Path
    cwd: pathlib.Path
    timeout_s: int


def extract_json_object(text: str) -> tuple[str | None, str | None]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.MULTILINE).strip()
    try:
        json.loads(stripped)
        return stripped, None
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    if start == -1:
        return None, "no-json-object"

    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(stripped[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start:index + 1]
                try:
                    json.loads(candidate)
                    return candidate, None
                except json.JSONDecodeError as exc:
                    return None, f"bad-json: {exc}"
    return None, "unterminated-json-object"


def messages_to_hermes_prompt(messages: list[dict], purpose: str) -> str:
    rendered = [
        f"Tâche: {purpose}",
        "Réponds uniquement avec le résultat demandé. Pas de préambule.",
        "",
    ]
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        rendered.append(f"## {role}\n{content}")
    return "\n\n".join(rendered)


def hermes_complete_text(
    config: LlmBackendConfig,
    messages: list[dict],
    purpose: str,
    run_command: Callable = subprocess.run,
) -> tuple[str | None, str | None]:
    prompt = messages_to_hermes_prompt(messages, purpose)
    try:
        proc = run_command(
            [str(config.hermes_cli), "-z", prompt],
            cwd=str(config.cwd),
            capture_output=True,
            text=True,
            timeout=config.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None, f"hermes-timeout: {config.timeout_s}s"
    except OSError as exc:
        return None, f"hermes-exec: {exc}"

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = (stderr or stdout)[:500]
        return None, f"hermes-{proc.returncode}: {detail}"
    if not stdout:
        return None, f"hermes-empty-output: {stderr[:300]}"
    return stdout, None


def complete_text(
    config: LlmBackendConfig,
    messages: list[dict],
    purpose: str,
    run_command: Callable = subprocess.run,
) -> tuple[str | None, str | None]:
    return hermes_complete_text(config, messages, purpose, run_command=run_command)

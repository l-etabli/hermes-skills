from __future__ import annotations

import json
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LlmBackendConfig:
    backend: str
    hermes_cli: pathlib.Path
    cwd: pathlib.Path
    timeout_s: int
    openrouter_api_key: str
    openrouter_api: str
    openrouter_model: str


SUPPORTED_LLM_BACKENDS = frozenset(("hermes", "openrouter"))


def validate_backend(backend: str) -> str | None:
    if backend in SUPPORTED_LLM_BACKENDS:
        return None
    supported = ", ".join(sorted(SUPPORTED_LLM_BACKENDS))
    return f"unsupported-llm-backend: {backend} (expected one of: {supported})"


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


def _default_post_request(url: str, **kwargs):
    import requests

    return requests.post(url, **kwargs)


def openrouter_chat(
    config: LlmBackendConfig,
    messages: list[dict],
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    model: str | None = None,
    post_request: Callable = _default_post_request,
) -> tuple[dict | None, str | None]:
    if not config.openrouter_api_key:
        return None, "missing-env: OPENROUTER_API_KEY"
    body: dict = {
        "model": model or config.openrouter_model,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if response_format:
        body["response_format"] = response_format
    try:
        response = post_request(
            config.openrouter_api,
            headers={
                "Authorization": f"Bearer {config.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/l-etabli/hermes-skills",
                "X-Title": "craig-pipeline",
            },
            json=body,
            timeout=config.timeout_s,
        )
    except Exception as exc:
        return None, f"network: {type(exc).__name__}: {exc}"
    if not response.ok:
        return None, f"openrouter-{response.status_code}: {response.text[:400]}"
    try:
        return response.json(), None
    except ValueError:
        return None, f"openrouter-bad-json: {response.text[:200]}"


def openrouter_complete_text(
    config: LlmBackendConfig,
    messages: list[dict],
    response_format: dict | None = None,
) -> tuple[str | None, str | None]:
    response, err = openrouter_chat(config, messages, response_format=response_format)
    if err:
        return None, err
    try:
        return response["choices"][0]["message"]["content"], None
    except (KeyError, IndexError, TypeError):
        return None, f"bad-response: {str(response)[:300]}"


def complete_text(
    config: LlmBackendConfig,
    messages: list[dict],
    purpose: str,
    response_format: dict | None = None,
    run_command: Callable = subprocess.run,
) -> tuple[str | None, str | None]:
    backend_err = validate_backend(config.backend)
    if backend_err:
        return None, backend_err
    if config.backend == "hermes":
        return hermes_complete_text(config, messages, purpose, run_command=run_command)
    return openrouter_complete_text(config, messages, response_format=response_format)

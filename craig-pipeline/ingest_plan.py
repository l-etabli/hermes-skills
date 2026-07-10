"""Two-phase JSON edit-plan ingest for the Hermes oneshot backend.

`hermes -z` can't offer a restricted tool set without unleashing a full
YOLO agent on the container — the exact failure mode craig-pipeline was
built to eliminate. The ingest therefore embeds all context in the prompt
and asks for bounded JSON:

  Phase 1 (discovery): wiki tree + transcript excerpt -> {"reads": [paths]}
  Phase 2 (plan): transcript + selected file contents -> {"edits": [...], "rationale"}

The pipeline then validates each edit through propose_edit checks,
guards, and a transactional apply.

Everything here is a pure function over strings/dicts — no env, no
filesystem, no network — so tests don't need the pipeline's prod env.

Byte budgets: `hermes -z` takes the prompt via a single argv argument,
which Linux caps at 128KB (MAX_ARG_STRLEN). The caps below keep the
worst-case phase-2 prompt near ~110KB.
"""

from __future__ import annotations

import json

from llm_backend import extract_json_object

DISCOVERY_TRANSCRIPT_BYTES = 20_000
DISCOVERY_TREE_ENTRIES = 400
PLAN_TRANSCRIPT_BYTES = 55_000
PLAN_FILE_BYTES = 5_000
PLAN_SKILL_BRIEF_BYTES = 8_000
# Bound on how much of a rejected LLM response gets echoed back in the
# retry round. The first plan prompt is ~110KB by design; replaying a
# large malformed response verbatim can push the retry argv past
# MAX_ARG_STRLEN (128KB) → execve E2BIG, burning the attempt that
# mattered most.
RETRY_ECHO_BYTES = 4_000
MAX_READS = 8
MAX_EDITS = 50

EDIT_ACTIONS = frozenset(("create", "append", "replace", "patch"))


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate to at most max_bytes of UTF-8 without splitting a rune."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


PLAN_RULES = """\
Règles strictes pour le plan d'édition :
- N'écris JAMAIS dans `raw/` ni `.craig-*/` (gérés par le code).
- N'écris JAMAIS de chatter de tool-call, de `<system-reminder>`, ou de
  paraphrase brute d'un fichier lu dans le contenu d'un fichier wiki.
- Préfère `append`/`patch` à `replace` (replace écrase tout le fichier).
- `replace` est INTERDIT sur un fichier marqué « tronqué » : tu n'en
  vois qu'un extrait, un replace détruirait la partie invisible.
- `patch` : `find` doit être une sous-chaîne EXACTE du fichier tel que
  fourni ci-dessus. Ne patche pas un fichier dont tu n'as pas le contenu.
- Les patches sont appliqués DANS L'ORDRE : sur un même fichier, le
  `find` d'un patch ne doit PAS chevaucher le texte modifié par un patch
  précédent (sinon il ne matchera plus). Un `find` court et disjoint par
  patch, ou un seul patch plus large.
- `append`/`patch`/`replace` uniquement sur un fichier existant,
  `create` uniquement sur un fichier inexistant.
- Si le transcript ne mérite pas d'ingest (smalltalk, test technique,
  recording vide), retourne `"edits": []`. Un ingest vide vaut mieux
  qu'un ingest halluciné.
"""


def build_discovery_messages(wiki_tree: list[str], transcript_rel: str,
                             transcript_body: str) -> list[dict]:
    tree = wiki_tree[:DISCOVERY_TREE_ENTRIES]
    tree_note = ""
    if len(wiki_tree) > DISCOVERY_TREE_ENTRIES:
        tree_note = f"\n(... {len(wiki_tree) - DISCOVERY_TREE_ENTRIES} fichiers omis)"
    excerpt, truncated = truncate_utf8(transcript_body, DISCOVERY_TRANSCRIPT_BYTES)
    excerpt_note = "\n(... transcript tronqué pour la sélection)" if truncated else ""
    user = (
        f"Un transcript de meeting (`{transcript_rel}`) va être ingéré dans "
        f"un wiki Markdown personnel (style Obsidian, liens [[X]]).\n\n"
        f"Fichiers du wiki :\n" + "\n".join(tree) + tree_note + "\n\n"
        f"Début du transcript :\n\n{excerpt}{excerpt_note}\n\n"
        f"Choisis les fichiers wiki existants (max {MAX_READS}) dont le "
        f"contenu est nécessaire pour préparer les éditions : pages "
        f"people/concepts des entités mentionnées, log, index.\n"
        f'Réponds UNIQUEMENT avec {{"reads": ["chemin", ...]}} — chemins '
        f"pris tels quels dans la liste ci-dessus, sans texte autour."
    )
    return [{"role": "user", "content": user}]


def parse_reads(text: str, valid_paths: set[str]) -> tuple[list[str] | None, str | None]:
    """Parse {"reads": [...]}. Unknown paths are dropped, not fatal —
    the plan phase degrades gracefully with fewer files."""
    obj_text, err = extract_json_object(text)
    if err or not obj_text:
        return None, err or "no-json-object"
    obj = json.loads(obj_text)
    if not isinstance(obj, dict):
        return None, "not-an-object"
    reads = obj.get("reads")
    if not isinstance(reads, list):
        return None, "reads-not-a-list"
    out: list[str] = []
    for p in reads:
        if isinstance(p, str) and p in valid_paths and p not in out:
            out.append(p)
        if len(out) >= MAX_READS:
            break
    return out, None


def build_plan_messages(skill_brief: str, transcript_rel: str,
                        transcript_body: str,
                        files: dict[str, str]) -> tuple[list[dict], set[str]]:
    """Returns (messages, truncated_paths). `files` must hold FULL file
    contents: this function owns the single truncation point
    (PLAN_FILE_BYTES), so `truncated_paths` is authoritative — the
    caller uses it to refuse `replace` on files the LLM only partially
    saw. Feeding pre-truncated content here would make the flag lie."""
    brief, _ = truncate_utf8(skill_brief, PLAN_SKILL_BRIEF_BYTES)
    body, truncated = truncate_utf8(transcript_body, PLAN_TRANSCRIPT_BYTES)
    body_note = "\n(... transcript tronqué)" if truncated else ""
    sections = []
    truncated_paths: set[str] = set()
    for path, content in list(files.items())[:MAX_READS]:
        excerpt, trunc = truncate_utf8(content, PLAN_FILE_BYTES)
        note = ""
        if trunc:
            truncated_paths.add(path)
            note = ("\n(... fichier tronqué — ne PAS le patcher au-delà de cet "
                    "extrait ; `replace` est interdit sur ce fichier)")
        sections.append(f"### `{path}`\n````\n{excerpt}{note}\n````")
    files_block = "\n\n".join(sections) if sections else "(aucun fichier fourni)"
    user = (
        f"## Contexte wiki\n{brief}\n\n"
        f"## Fichiers wiki existants fournis\n{files_block}\n\n"
        f"## Transcript à ingérer (`{transcript_rel}`)\n\n{body}{body_note}\n\n"
        f"## Tâche\n"
        f"Produis le plan d'édition du wiki pour ingérer ce transcript : "
        f"entrée de log, mise à jour des pages people/concepts mentionnées "
        f"(liens vers `{transcript_rel}`), nouvelles pages concept si "
        f"pertinent, mise à jour des index.\n\n{PLAN_RULES}\n"
        f"Réponds UNIQUEMENT avec un objet JSON, sans fence ni texte autour :\n"
        f'{{"edits": [{{"action": "create|append|replace|patch", "path": "...", '
        f'"content": "...", "find": "...", "replace_with": "..."}}, ...], '
        f'"rationale": "résumé court de ce qui change"}}\n'
        f"`content` requis pour create/append/replace ; `find`+`replace_with` "
        f"requis pour patch. Max {MAX_EDITS} éditions."
    )
    return [{"role": "user", "content": user}], truncated_paths


def parse_edit_plan(text: str) -> tuple[list[dict] | None, str | None, list[str], str | None]:
    """Returns (edits, rationale, dropped, err). Shape validation only —
    path safety and exists/not-exists checks belong to the pipeline's
    propose_edit validation.

    Structural failures (no JSON object, edits not a list) set `err`
    and trigger the caller's feedback retry. Per-edit problems (bad
    action, whitespace-only content, missing find) drop that edit into
    `dropped` and keep the rest — same drop-not-abort philosophy as
    propose_edit and dry_run_edits: one spacer-line append must not
    burn a whole attempt when 29 other edits were fine. `err` is also
    set when a non-empty plan lost EVERY edit, so a fully-invalid plan
    still gets its retry round."""
    obj_text, err = extract_json_object(text)
    if err or not obj_text:
        return None, None, [], err or "no-json-object"
    obj = json.loads(obj_text)
    if not isinstance(obj, dict):
        return None, None, [], "not-an-object"
    edits = obj.get("edits")
    if not isinstance(edits, list):
        return None, None, [], "edits-not-a-list"
    rationale = obj.get("rationale") if isinstance(obj.get("rationale"), str) else None
    out: list[dict] = []
    dropped: list[str] = []
    for i, edit in enumerate(edits):
        if len(out) >= MAX_EDITS:
            dropped.append(f"edit-{i}: over-max-edits ({MAX_EDITS})")
            continue
        if not isinstance(edit, dict):
            dropped.append(f"edit-{i}: not-an-object")
            continue
        action = edit.get("action")
        path = edit.get("path")
        if action not in EDIT_ACTIONS:
            dropped.append(f"edit-{i}: bad-action: {action}")
            continue
        if not isinstance(path, str) or not path:
            dropped.append(f"edit-{i}: bad-path")
            continue
        if action == "patch":
            if not isinstance(edit.get("find"), str) or not edit.get("find"):
                dropped.append(f"edit-{i} ({path}): patch-missing-find")
                continue
            if not isinstance(edit.get("replace_with"), str):
                dropped.append(f"edit-{i} ({path}): patch-missing-replace_with")
                continue
        else:
            if not isinstance(edit.get("content"), str) or not edit.get("content", "").strip():
                dropped.append(f"edit-{i} ({path}): missing-content")
                continue
        out.append(edit)
    if edits and not out:
        return None, rationale, dropped, "all-edits-invalid: " + "; ".join(dropped[:3])
    return out, rationale, dropped, None


def apply_edit(cur: str | None, edit: dict) -> tuple[str | None, str | None]:
    """The one place edit semantics live: `cur` is the file's current
    content (None if absent), returns (new_content, reject_reason) —
    exactly one is None. Both dry_run_edits and the pipeline's real
    apply MUST go through here; the previous hand-mirrored copies
    drifted and re-triggered the transactional abort the dry-run was
    built to prevent.

    Semantics: create/replace normalize to a trailing newline, append
    separates with one, patch replaces the FIRST occurrence only —
    repeated substrings in a wiki page are usually intentional (links
    across sections), forcing the LLM to enlarge `find` is safer than
    silently rewriting all occurrences."""
    action = edit["action"]
    if action == "create":
        if cur is not None:
            return None, "create but file exists"
        content = edit["content"]
        return (content if content.endswith("\n") else content + "\n"), None
    if action == "append":
        if cur is None:
            return None, "append but file missing"
        content = edit["content"]
        if not content.endswith("\n"):
            content += "\n"
        sep = "" if (not cur or cur.endswith("\n")) else "\n"
        return cur + sep + content, None
    if action == "replace":
        if cur is None:
            return None, "replace but file missing"
        content = edit["content"]
        return (content if content.endswith("\n") else content + "\n"), None
    if action == "patch":
        if cur is None:
            return None, "patch but file missing"
        if edit["find"] not in cur:
            return None, "patch find absent (chevauche un edit précédent ?)"
        return cur.replace(edit["find"], edit["replace_with"], 1), None
    return None, f"unknown action: {action}"


def dry_run_edits(edits: list[dict], read_file) -> tuple[list[dict], list[str]]:
    """Simulate the pipeline's sequential apply over in-memory contents
    and drop edits that would fail — typically a patch whose `find` was
    consumed by an earlier patch on the same file (observed live on
    2026-07-01: 4 patches on index.md, #2's find spanned the line #1 had
    rewritten, aborting the whole transactional apply).

    `read_file(path) -> str | None` returns current content or None if
    the file doesn't exist. Semantics come from apply_edit — the same
    function the real apply uses. Returns (accepted, rejected_reasons)."""
    contents: dict[str, str | None] = {}

    def current(path: str) -> str | None:
        if path not in contents:
            contents[path] = read_file(path)
        return contents[path]

    accepted: list[dict] = []
    rejected: list[str] = []
    for edit in edits:
        path = edit["path"]
        new, reject = apply_edit(current(path), edit)
        if reject is not None:
            rejected.append(f"{path}: {reject}")
            continue
        contents[path] = new
        accepted.append(edit)
    return accepted, rejected

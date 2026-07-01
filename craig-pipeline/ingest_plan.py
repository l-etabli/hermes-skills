"""Two-phase JSON edit-plan ingest for the Hermes oneshot backend.

The OpenRouter ingest path drives an in-process tool-calling loop
(read_file/list_dir/propose_edit/done). `hermes -z` can't offer those
tools without unleashing a full YOLO agent on the container — the exact
failure mode craig-pipeline was built to eliminate. Instead the Hermes
path embeds all context in the prompt and asks for bounded JSON:

  Phase 1 (discovery): wiki tree + transcript excerpt -> {"reads": [paths]}
  Phase 2 (plan): transcript + selected file contents -> {"edits": [...], "rationale"}

The pipeline then validates each edit through the same propose_edit
checks, guards, and transactional apply as the tool loop.

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
                        transcript_body: str, files: dict[str, str]) -> list[dict]:
    brief, _ = truncate_utf8(skill_brief, PLAN_SKILL_BRIEF_BYTES)
    body, truncated = truncate_utf8(transcript_body, PLAN_TRANSCRIPT_BYTES)
    body_note = "\n(... transcript tronqué)" if truncated else ""
    sections = []
    for path, content in list(files.items())[:MAX_READS]:
        excerpt, trunc = truncate_utf8(content, PLAN_FILE_BYTES)
        note = "\n(... fichier tronqué — ne PAS le patcher au-delà de cet extrait)" if trunc else ""
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
    return [{"role": "user", "content": user}]


def parse_edit_plan(text: str) -> tuple[list[dict] | None, str | None, str | None]:
    """Returns (edits, rationale, err). Shape validation only — path
    safety and exists/not-exists checks belong to the pipeline's
    propose_edit validation."""
    obj_text, err = extract_json_object(text)
    if err or not obj_text:
        return None, None, err or "no-json-object"
    obj = json.loads(obj_text)
    if not isinstance(obj, dict):
        return None, None, "not-an-object"
    edits = obj.get("edits")
    if not isinstance(edits, list):
        return None, None, "edits-not-a-list"
    if len(edits) > MAX_EDITS:
        return None, None, f"too-many-edits: {len(edits)} > {MAX_EDITS}"
    rationale = obj.get("rationale") if isinstance(obj.get("rationale"), str) else None
    out: list[dict] = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return None, None, f"edit-{i}-not-an-object"
        action = edit.get("action")
        path = edit.get("path")
        if action not in EDIT_ACTIONS:
            return None, None, f"edit-{i}-bad-action: {action}"
        if not isinstance(path, str) or not path:
            return None, None, f"edit-{i}-bad-path"
        if action == "patch":
            if not isinstance(edit.get("find"), str) or not edit.get("find"):
                return None, None, f"edit-{i}-patch-missing-find"
            if not isinstance(edit.get("replace_with"), str):
                return None, None, f"edit-{i}-patch-missing-replace_with"
        else:
            if not isinstance(edit.get("content"), str) or not edit.get("content", "").strip():
                return None, None, f"edit-{i}-missing-content"
        out.append(edit)
    return out, rationale, None


def dry_run_edits(edits: list[dict], read_file) -> tuple[list[dict], list[str]]:
    """Simulate the pipeline's sequential apply over in-memory contents
    and drop edits that would fail — typically a patch whose `find` was
    consumed by an earlier patch on the same file (observed live on
    2026-07-01: 4 patches on index.md, #2's find spanned the line #1 had
    rewritten, aborting the whole transactional apply).

    `read_file(path) -> str | None` returns current content or None if
    the file doesn't exist. Mirrors apply semantics: create/replace add
    a trailing newline, append separates with one, patch replaces the
    first occurrence. Returns (accepted, rejected_reasons)."""
    contents: dict[str, str | None] = {}

    def current(path: str) -> str | None:
        if path not in contents:
            contents[path] = read_file(path)
        return contents[path]

    accepted: list[dict] = []
    rejected: list[str] = []
    for edit in edits:
        path = edit["path"]
        action = edit["action"]
        cur = current(path)
        if action == "create":
            if cur is not None:
                rejected.append(f"{path}: create but file exists")
                continue
            content = edit["content"]
            contents[path] = content if content.endswith("\n") else content + "\n"
        elif action == "append":
            if cur is None:
                rejected.append(f"{path}: append but file missing")
                continue
            content = edit["content"]
            if not content.endswith("\n"):
                content += "\n"
            sep = "" if (not cur or cur.endswith("\n")) else "\n"
            contents[path] = cur + sep + content
        elif action == "replace":
            if cur is None:
                rejected.append(f"{path}: replace but file missing")
                continue
            content = edit["content"]
            contents[path] = content if content.endswith("\n") else content + "\n"
        elif action == "patch":
            if cur is None:
                rejected.append(f"{path}: patch but file missing")
                continue
            if edit["find"] not in cur:
                rejected.append(f"{path}: patch find absent (chevauche un edit précédent ?)")
                continue
            contents[path] = cur.replace(edit["find"], edit["replace_with"], 1)
        accepted.append(edit)
    return accepted, rejected

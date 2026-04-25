---
name: meeting-debrief
description: "Après chaque transcript Craig ingéré dans le wiki (post-craig-watch processed), génère un debrief structuré (TLDR + décisions + open questions + action items typés) et le poste dans DISCORD_HOME_CHANNEL avec un thread de validation. Phase 2 : dispatche les actions validées vers github-issues / hermes-cron / google-workspace / obsidian / linear."
version: 0.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, debrief, discord, llm-wiki]
    category: voice
    related_skills: [craig-watch, llm-wiki, github-issues]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Lit raw/transcripts/, écrit raw/debriefs/ et .craig-debriefs-pending/."
    required_for: full functionality
  - name: DISCORD_BOT_TOKEN
    prompt: "Token bot pour POSTer le recap, créer le thread, éditer le message recap au moment du dispatch."
    required_for: full functionality
  - name: DISCORD_HOME_CHANNEL
    prompt: "ID du canal Discord humain où poster le recap (PAS #craig-events, qui est log technique)."
    required_for: full functionality
  - name: MEETING_DEBRIEF_MIN_DURATION_S
    prompt: "Durée min en secondes pour qu'un recording soit débriefé (défaut 180 = 3 min). Sous ce seuil, debrief.py skippe sans rien poster."
    required_for: optional
---

# Meeting Debrief

Transforme un transcript Craig (déjà ingéré dans le wiki par `craig-watch` + `llm-wiki`) en un debrief humain (TLDR + décisions + actions à valider) posté dans `DISCORD_HOME_CHANNEL`. Un thread est ouvert sous le recap : l'utilisateur valide / refuse les actions ; à sa réponse, ce skill dispatche chaque action validée vers le bon skill cible (`github-issues`, `hermes-cron` natif, `google-workspace`, `obsidian`, `linear`).

## When to Use

Ce skill ne s'auto-trigger pas. Il est invoqué :

1. **Phase 1 (génération)** — par `craig-watch` après un `processed[i]` réussi (cf. `craig-watch/SKILL.md` step 5). Tu peux aussi le lancer manuellement sur un transcript existant : génère le JSON debrief, écris-le dans `raw/debriefs/`, puis `meeting-debrief/debrief.py --debrief-path <path>`.
2. **Phase 2 (dispatch)** — quand un message arrive dans un thread Discord dont le parent message est un recap posté précédemment (concrètement : `parent_id` du thread ∈ liste des fichiers `.craig-debriefs-pending/<thread_id>.json`).

## Schéma JSON debrief

Validé strictement par `meeting-debrief/schema.json` (Draft 2020-12). Toute déviation → `debrief.py` retourne `error/schema-mismatch` et exit 1, à toi de corriger et relancer.

```json
{
  "transcript_path": "raw/transcripts/2026-04-25-craig-xxx-2026-04-25.md",
  "tldr": "- Réunion produit avec X, Y\n- Décision sur Z\n- 3 actions à valider",
  "decisions": [
    "On part sur l'architecture A pour le module B."
  ],
  "open_questions": [
    "Qui prend en charge l'audit sécu ?"
  ],
  "action_items": [
    {
      "id": 1,
      "type": "bug",
      "description": "Le dropdown du form étalonnage saute au scroll",
      "suggested_action": {
        "kind": "github-issue",
        "target": "l-etabli/pilotis",
        "body": "## Repro\n…"
      },
      "confidence": 0.85
    },
    {
      "id": 2,
      "type": "relance",
      "description": "Relancer Acme dans 10 jours si pas de retour",
      "suggested_action": {
        "kind": "hermes-cron",
        "target": "relance-acme-2026-05-05",
        "when": "0 9 5 5 *"
      },
      "confidence": 0.7
    }
  ]
}
```

Champs clés :

- `type` ∈ `bug | contact | relance | task | decision` — taxonomie sémantique.
- `suggested_action.kind` ∈ `github-issue | hermes-cron | gmail-draft | obsidian-note | linear-task` — cible technique.
- `suggested_action.target` — `owner/repo` pour github, nom de cron pour hermes-cron, email pour gmail, chemin vault pour obsidian, team key pour linear.
- `suggested_action.when` — requis pour `hermes-cron`. ISO ou cron.
- `suggested_action.body` — optionnel, markdown pré-rempli (utile pour github-issue / obsidian-note).
- `confidence` ∈ [0, 1] — auto-évaluation, < 0.6 = signale au user.

## Procedure (génération, Phase 1)

Quand tu es invoqué par `craig-watch` (step 5) ou manuellement sur un transcript :

1. **Lis le transcript** : `raw/transcripts/<file>.md`. Récupère le frontmatter (`duration_s`, `recorded_at`, `source_url`) et le body (segments timestampés).

2. **Génère le JSON debrief** conforme au schéma ci-dessus. Le contexte wiki déjà chargé par `llm-wiki` t'aide naturellement à inférer le bon `target` (ex. `bug du dropdown étalonnage` → `l-etabli/pilotis`).

3. **Écris-le** dans `$WIKI_PATH/raw/debriefs/<date>-<slug>.json` (le slug est dérivé du nom du transcript, sans l'extension `.md`). Crée le dossier si besoin.

4. **Invoke le script** :

   ```bash
   uv run --with requests --with jsonschema --with pyyaml \
       /opt/data/skills-shared/meeting-debrief/debrief.py \
       --debrief-path raw/debriefs/<date>-<slug>.json
   ```

5. **Lis le JSON de sortie** :
   - `posted` → silence. Le recap est déjà sur Discord, tu n'as rien à dire de plus.
   - `skipped` (`reason: too-short` ou `already-debriefed` ou `no-actions`) → silence.
   - `error` (`reason: schema-mismatch` notamment) → corrige le JSON et relance UNE fois. Si ça échoue encore, signale le problème au user avec `reason` + `detail`.

## Procedure (réponse thread, Phase 2)

Quand tu reçois un message Discord :

1. **Filtre** : ne traite que les messages dont le canal parent (Discord renvoie `channel_id` = id du thread, et tu peux retrouver le thread parent via `GET /channels/{channel_id}` → `parent_id`) correspond à un thread connu, c.-à-d. `$WIKI_PATH/.craig-debriefs-pending/<thread_id>.json` existe (où `<thread_id>` = id du thread, pas du channel parent). Si pas de pending → ignore le message.

2. **Parse l'intent user**. Cas typiques :
   - `tous` / `valides tout` → `--validated 1,2,…,N`.
   - `aucun` → `--refused 1,2,…,N`.
   - `1, 3` → `--validated 1,3`.
   - `tout sauf 2` → `--validated <tous sauf 2>`.
   - Correction de cible : `2 → l-etabli/other-repo` → ajoute à un correction JSON `{"2": {"target": "l-etabli/other-repo"}}`.

3. **Invoke dispatch** :

   ```bash
   uv run --with requests \
       /opt/data/skills-shared/meeting-debrief/dispatch.py \
       --thread-id <id> \
       --validated 1,3 \
       --refused 2 \
       [--correction-json /tmp/corrections.json]
   ```

4. **Lis le `to_invoke[]`** retourné. Pour chaque action, invoque le skill cible correspondant à `suggested_action.kind` :
   - `github-issue` → skill `github-issues` (titre = `description`, body = `suggested_action.body` si fourni).
   - `hermes-cron` → CLI `hermes` natif (`hermes cron add ...`) avec `name = target`, `cron = when`.
   - `gmail-draft` → skill `google-workspace` (compose draft).
   - `obsidian-note` → skill `obsidian` (write note at `target`).
   - `linear-task` → skill `linear` (create task in team `target`).

5. **Reporte chaque succès dans le thread** : un message court par action invoquée (« ✅ Issue créée : `l-etabli/pilotis#42` »). En cas d'échec d'invocation downstream, ⚠️ + détail.

## Pitfalls

- **Pas de `pip install`** : tout passe par `uv run --with …`.
- **Pas d'`import anthropic` / `openai`** côté script : la génération du JSON debrief est faite par toi (le LLM Hermes), pas par `debrief.py`. `debrief.py` est strictement de la plomberie.
- **Idempotence** : `debrief.py` skippe si un autre debrief référence déjà le même `transcript_sha256` (matché sur le body, pas le frontmatter). Ne re-débriefe pas un transcript déjà traité.
- **Durée < `MEETING_DEBRIEF_MIN_DURATION_S`** : `debrief.py` skippe avec `too-short`. Évite de générer le JSON pour rien : si tu connais `duration_s` (exposé dans le `processed[]` de craig-watch), saute la génération avant d'invoker.
- **Auto-archive Discord** : threads créés avec `auto_archive_duration: 1440` (24 h). Au-delà, le user devra rouvrir manuellement le thread pour valider.
- **`.craig-debriefs-pending/`** est gitignoré côté wiki (state runtime, pas knowledge). À ajouter au `.gitignore` du vault si pas déjà fait — voir hermes-infra.
- **Threads != channels** : Discord traite un thread comme un sous-channel ; le `thread_id` qu'on stocke dans le pending est l'`id` retourné par l'API au moment de la création (`POST /channels/{ch}/messages/{msg}/threads`). C'est ce même id qui apparaît comme `channel_id` sur les messages postés dans le thread.
- **Pas de re-post du recap** : si le user répond plusieurs fois dans le thread, tu peux dispatcher plusieurs fois (les IDs déjà traités auront été retirés du pending). Ne re-poste JAMAIS le recap initial.

## Verification

### Phase 1

1. `$WIKI_PATH/raw/debriefs/<date>-<slug>.json` existe et passe `jsonschema` :

   ```bash
   uv run --with jsonschema python3 -c '
   import json, jsonschema, sys
   schema = json.load(open("/opt/data/skills-shared/meeting-debrief/schema.json"))
   debrief = json.load(open(sys.argv[1]))
   jsonschema.Draft202012Validator(schema).validate(debrief)
   print("OK")
   ' "$WIKI_PATH/raw/debriefs/<file>.json"
   ```

2. Un message est visible dans `DISCORD_HOME_CHANNEL` avec le recap markdown.
3. Un thread est ouvert sous ce message, contenant le prompt de validation.
4. `$WIKI_PATH/.craig-debriefs-pending/<thread_id>.json` existe avec les `action_items`, `recap_markdown`, `transcript_sha256`.
5. Relance manuelle de `debrief.py --debrief-path <même path>` → `skipped/already-debriefed`.

### Phase 2

1. Après dispatch happy path (`tous`) : recap édité avec ✅ devant chaque action, `to_invoke[]` contient toutes les actions, pending supprimé.
2. Après refus partiel (`1, 3` validés, `2` refusé) : recap montre ✅ 1, ❌ 2, ✅ 3, pending supprimé.
3. Après refus total (`aucun`) : recap montre ❌ partout, pending supprimé.
4. Chaque skill cible a effectivement créé son artefact (issue github, cron, draft gmail, …) — vérifications externes.

## Notes

- **Phasing** : Phase 1 (debrief.py) peut shipper sans Phase 2 (dispatch.py). En Phase 1 seule, l'utilisateur lit le recap et le thread reste passif — c'est déjà utile.
- **Coût LLM** : un appel par recording > 3 min. Mesurer sur 2-3 vrais meetings avant de scaler.
- **False positives** sur `action_items[]` : la validation thread est la mitigation native. Pas besoin de sur-engineerer.

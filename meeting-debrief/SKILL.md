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

La structure canonique est `meeting-debrief/schema.json` (JSON Schema Draft 2020-12). Lis-le pour connaître la liste exacte des champs, leurs types, et les enums autorisés. `debrief.py` valide strictement contre ce schéma — toute déviation retourne `error/schema-mismatch` et exit 1.

> ⚠️ **Pas d'exemple JSON volontairement dans ce SKILL.md.** Vu en prod : le LLM copie verbatim tout exemple présent ici, même très clairement marqué synthétique, et le sert comme "vrai" debrief. La règle stricte : tu génères le JSON **uniquement** à partir du transcript que tu lis au step 1, en respectant la structure du `schema.json`. Aucune autre source.

Champs clés (résumé pour la rédaction — la spec complète est dans schema.json) :

- `transcript_path` — wiki-relatif (`raw/transcripts/<file>.md`).
- `tldr` — 2-5 puces markdown qui résument ce qui a été RÉELLEMENT dit dans CE transcript.
- `decisions[]` — décisions concrètes prises dans CE meeting. Liste vide si aucune.
- `open_questions[]` — questions soulevées mais non résolues. Liste vide si aucune.
- `action_items[]` — actions concrètes mentionnées dans CE transcript. Pour chacune :
  - `id` — entier ≥ 1, unique dans la liste.
  - `type` ∈ `bug | contact | relance | task | decision` — taxonomie sémantique.
  - `description` — UNE phrase décrivant l'action.
  - `suggested_action.kind` ∈ `github-issue | hermes-cron | gmail-draft | obsidian-note | linear-task` — cible technique.
  - `suggested_action.target` — routing concret (`owner/repo`, nom-cron, email, chemin-vault, team-key — INFÉRÉ depuis le contexte du transcript et du contenu wiki déjà chargé par `llm-wiki`).
  - `suggested_action.when` — requis pour `hermes-cron`. ISO ou cron.
  - `suggested_action.body` — optionnel, markdown pré-rempli (github-issue, obsidian-note).
  - `confidence` ∈ [0, 1] — auto-évaluation honnête. Si < 0.6, `debrief.py` le signalera au user.

**Si le transcript ne mentionne aucune action concrète** (meeting de status, brainstorming sans engagement, test technique, smalltalk) : retourne `"action_items": []`. `debrief.py` skippera proprement avec `reason: no-actions`. **Toujours préférer un debrief vide à un debrief halluciné.** Une action inventée pollue le wiki et fait perdre confiance dans l'outil.

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

2. **Génère le JSON debrief** conforme au `schema.json`. Le contexte wiki déjà chargé par `llm-wiki` t'aide naturellement à inférer le bon `target` (mappe le sujet d'une action mentionnée dans le transcript vers le repo / cron / vault path qui le couvre dans le wiki — pas d'invention de cible).

   **Contrainte de longueur** : le recap markdown rendu (TLDR + decisions + open_questions + lignes d'action_items) ne doit PAS dépasser **~1800 caractères** une fois formaté — Discord cape le `content` d'un message à 2000 chars et `debrief.py` tronque silencieusement au-delà (avec un footer « recap tronqué »). Pour rester sous la limite :
   - TLDR concis (3-5 puces, pas un paragraphe entier — l'utilisateur a le transcript complet dans le wiki s'il veut creuser).
   - Limite-toi aux décisions/open_questions vraiment saillantes (pas de paraphrase exhaustive).
   - Pour les `action_items`, le `description` doit tenir en 1 phrase ; pousse les détails (repro steps, body issue, etc.) dans `suggested_action.body` qui n'apparaît PAS dans le recap Discord — il sera utilisé seulement par dispatch downstream (issue github, draft mail, …).
   - Si tu sens que le recap dépasse, retire des `action_items` faibles (`confidence < 0.6`) plutôt que tronquer du contenu high-signal.

3. **Écris-le** dans `$WIKI_PATH/raw/debriefs/<basename>.json` où `<basename>` est le nom du transcript sans l'extension `.md`. Exemple : transcript `raw/transcripts/2026-04-26-1240-craig.md` → debrief `raw/debriefs/2026-04-26-1240-craig.json`. Le naming est strictement un mirror du transcript pour faciliter la corrélation et le tri chronologique. Crée le dossier si besoin.

4. **Invoke le script** :

   ```bash
   uv run --with requests --with jsonschema --with pyyaml \
       /opt/data/skills-shared/meeting-debrief/debrief.py \
       --debrief-path raw/debriefs/<basename>.json
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

## Anti-patterns LLM (leçons des skills craig-*)

- ❌ **Ne pas inventer un `thread_id`** quand tu invoques `dispatch.py`. Le `thread_id` doit venir de ton contexte Discord réel (le message du user que tu traites est dans un thread, son `channel_id` côté Discord = ce `thread_id`). Si tu ne le retrouves pas, lis directement la liste des pending : `ls $WIKI_PATH/.craig-debriefs-pending/` te donne les `thread_id` actifs. NE devine JAMAIS de snowflake — préfère échouer loud.
- ❌ **Ne pas écrire le pending JSON ou éditer le recap toi-même** quand `debrief.py` ou `dispatch.py` retourne `error`. Surface l'erreur, point. Un état corrompu écrit par toi est pire qu'une erreur affichée.
- ❌ **Ne pas wrapper dans `sh -c` / `printf %q`** : si jamais tu dois passer du texte LLM-généré (ex. `body` dans une correction JSON), écris-le via `Path("/tmp/...").write_text(...)` puis passe le chemin. Le scanner sécurité Hermes bloque les zero-width chars sur cmdline.
- ❌ **Ne pas faire des appels Discord depuis `execute_code` sans le `User-Agent` `DiscordBot (...)`** — Cloudflare bloque le UA Python par défaut avec `error code: 1010`. Les scripts du repo l'envoient déjà ; si tu dois faire un call REST direct, copie-leur ce header.
- ❌ **Ne pas trust ton parsing du content Discord** : Craig (et d'autres bots modernes) utilisent **Components V2** (`flags & 32768`) — le texte vit dans `components[].components[].content` récursif, pas dans `content`/`embeds`. Pour Craig, on a un `extract_message_text` qui walk dans `craig-listener/listener.py` — réutilise-le si tu dois lire un msg Craig depuis l'API.

## Verification

### Phase 1

1. `$WIKI_PATH/raw/debriefs/<basename>.json` existe et passe `jsonschema` :

   ```bash
   uv run --with jsonschema python3 -c '
   import json, jsonschema, sys
   schema = json.load(open("/opt/data/skills-shared/meeting-debrief/schema.json"))
   debrief = json.load(open(sys.argv[1]))
   jsonschema.Draft202012Validator(schema).validate(debrief)
   print("OK")
   ' "$WIKI_PATH/raw/debriefs/<file>.json"
   ```

2. Un ou plusieurs messages sont visibles dans `DISCORD_HOME_CHANNEL` avec le recap markdown. Si le debrief dépasse ~1900 chars, il est splitté en plusieurs messages aux frontières de section (`### TLDR` / `### Décisions` / `### Questions ouvertes` / `### Actions proposées`) ; la section `Actions` est toujours dans son propre message pour que dispatch.py puisse l'éditer sur place.
3. Un thread est ouvert sous le PREMIER message (`parent_message_id`), contenant le prompt de validation.
4. `$WIKI_PATH/.craig-debriefs-pending/<thread_id>.json` existe avec `action_items`, `actions_message_id`, `actions_message_content`, `parent_message_id`, `transcript_sha256`.
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

---
name: craig-pipeline
description: "Orchestrateur déterministe Python qui pilote toute la chaîne post-Craig avec checkpoints SHA-256, circuit breaker et quarantaine. Ingest et debrief passent par `hermes -z`; le cron lance directement le runner no-agent."
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, craig, pipeline, cron]
    category: voice
    related_skills: [craig-listener, craig-watch, craig-transcript-record, meeting-debrief]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki. Le pipeline lit .craig-pending/, écrit raw/transcripts/, raw/debriefs/, .craig-pipeline-debug/."
    required_for: full functionality
  - name: CRAIG_DISCORD_BOT_TOKEN
    prompt: "Token du bot Discord dédié aux skills Craig pour POST/PATCH le status message + GET du panel Craig."
    required_for: full functionality
  - name: CRAIG_EVENTS_CHANNEL_ID
    prompt: "ID du canal #craig-events (où le status message live edité est posté en reply au panel Craig)."
    required_for: full functionality
  - name: CRAIG_HOME_CHANNEL
    prompt: "ID du canal humain où le recap meeting-debrief est posté (et le ✅ final quand .craig-pending/ se vide)."
    required_for: full functionality
  - name: CRAIG_GROQ_API_KEY
    prompt: "Hérité par scan.py (transcription Groq Whisper)."
    required_for: full functionality
  - name: GOOGLE_APPLICATION_CREDENTIALS
    prompt: "Hérité par scan.py (Drive service account)."
    required_for: full functionality
  - name: AUDIO_DRIVE_FOLDER_ID
    prompt: "Hérité par scan.py (dossier Drive Craig)."
    required_for: full functionality
  - name: MEETING_DEBRIEF_MIN_DURATION_S
    prompt: "Durée min d'un recording pour mériter un debrief (défaut 180). Sous le seuil : pipeline post le ✅ final 'recording court' et supprime le pending sans appel LLM debrief."
    required_for: optional
  - name: CRAIG_PIPELINE_MAX_AGE_HOURS
    prompt: "Durée de vie absolue d'un pending avant quarantaine (défaut 6 heures)."
    required_for: optional
  - name: HERMES_SKILLS_DIR
    prompt: "Racine des skills partagés (défaut /opt/data/skills-shared). Le pipeline appelle ./craig-transcript-record/scan.py et ./meeting-debrief/debrief.py en subprocess à partir de cette racine."
    required_for: optional
  - name: HERMES_CLI_PATH
    prompt: "Chemin du CLI hermes (défaut /opt/hermes/.venv/bin/hermes). Utilisé pour `cron list` + `cron remove` quand .craig-pending/ se vide, ainsi que pour l'ingest et le debrief via `hermes -z`."
    required_for: optional
  - name: PIPELINE_LLM_TIMEOUT_S
    prompt: "Timeout de chaque appel LLM pipeline (ingest + debrief, défaut 300)."
    required_for: optional
  - name: LLM_WIKI_SKILL_PATH
    prompt: "Chemin du SKILL.md llm-wiki upstream (défaut /opt/hermes/skills/research/llm-wiki/SKILL.md). Embarqué tronqué à 8KB dans le prompt du plan GPT. Si absent, le pipeline utilise un fallback brief minimal."
    required_for: optional
---

# Craig Pipeline (orchestrateur déterministe)

## When to Use

⚠️ **Ce skill ne doit JAMAIS être invoqué par mention utilisateur.** Il est invoqué exclusivement par le cron `craig-watch-followup` (auto-créé par `craig-listener` quand un nouveau recording arrive, auto-supprimé par ce script quand `.craig-pending/` se vide).

Le cron est un job upstream **sans agent**. Il exécute directement :

```bash
/opt/data/scripts/craig-pipeline-runner.sh
```

Le runner garde le succès silencieux, journalise localement une sortie bornée
et propage tout code non nul au scheduler. Aucun LLM n'orchestre ou ne
« rattrape » une phase.

## Pourquoi ce skill existe

Avant ce skill, le LLM était orchestrateur direct (via les skills `craig-watch` + `llm-wiki` + `meeting-debrief` enchaînés à chaque tick). Bugs récurrents observés en prod :

- Le LLM **commit mais ne push pas** (récurrent, plusieurs ticks).
- Le LLM **skip silencieusement** des étapes (ingest llm-wiki sauté sans explication).
- Le LLM **vandalise `index.md`** lors de l'ingest (écrit du chatter de tool-call type "File unchanged since last read" dans le contenu de la page, supprime 80% de lignes lors d'un "refactor").
- Le LLM **ment** sur ce qu'il a fait ("cron auto-créé" alors que faux).
- **Pas de visibilité** pendant les 1-15 min de Drive poll + Groq → user en aveugle.

Décision : passer à un orchestrateur Python déterministe. Les appels LLM (ingest + debrief) passent exclusivement par `hermes -z` et le provider GPT/OAuth configuré. Tout le reste est code.

## Architecture

```
.craig-pending/<craig_id>.json (écrit par craig-listener)
    │
    ▼   pipeline.py (every 5m via cron tick)
    │
    ├─ 0. GET panel Discord → "Recording ended." présent ?
    │       non → silence, garde pending
    │       oui → continue
    ├─ 1. POST status message dans #craig-events (reply au panel)
    │       "🎙️ Recording <date>  ⏳ Téléchargement Drive…"
    ├─ 2. subprocess scan.py → progress[] streamé en live
    │       chaque event PATCHe le status message
    ├─ 3. git add transcript + commit + push (en code)
    ├─ 4. llm-wiki ingest (bufferisé, jamais d'écriture LLM directe)
    │       - backend hermes (défaut) : plan d'édition JSON en 2 phases
    │         via `hermes -z` — discovery (tree + extrait transcript →
    │         fichiers à lire) puis plan (contexte embarqué → edits JSON
    │         bornés), cf. ingest_plan.py ; `replace` refusé sur un
    │         fichier fourni tronqué au LLM
    │       - validation propose_edit identique pour chaque édition
    │       - dry-run en mémoire : les edits individuellement invalides
    │         (patch chevauchant, cible absente) sont écartés — reportés
    │         dans dropped_edits + dump debug + message Discord, pas
    │         fatals ; « tout écarté » est signalé ⚠️, pas comme un no-op
    │       - guards : line-loss >30%, regex hallucination, >5000 lignes,
    │         >20 fichiers
    │       - apply transactionnel (des survivants) + commit + push
    │       - guard fail → revert via git checkout, dump debug, continue
    ├─ 5. génération debrief JSON via backend LLM configurable (Hermes par défaut)
    │       - validate vs meeting-debrief/schema.json
    │       - retry 3× sur erreur
    ├─ 6. subprocess debrief.py → POST recap + thread dans #hermes-perso
    └─ 7. delete pending JSON

Quand .craig-pending/ vide :
    - POST ✅ dans #hermes-perso
    - hermes cron remove <self>
```

## Idempotence et reprise sur crash

Le pending schema v2 enregistre atomiquement chaque phase réussie et le SHA-256
du transcript. Au tick suivant, le pipeline reprend à la première phase
incomplète :

- un pending v1 est migré depuis le transcript existant et l'historique Git ;
- un checkpoint ingest `completed` pour le même SHA interdit tout second ingest ;
- `generate_debrief` skip si `raw/debriefs/<basename>.json` existe et valide.
- `debrief.py` skip via `find_existing_debrief()` (matché sur `transcript_sha256`).

Une erreur non retryable met immédiatement le recording dans `.craig-failed/`.
Trois fingerprints consécutifs identiques font de même. Un pending âgé de plus
de six heures expire avant scan/ingest/debrief. Chaque quarantaine produit une
seule alerte et conserve les dix dernières erreurs.

## Format du status message

```
🎙️ Recording 2026-04-26 1437 UTC
✅ Transcrit (3m 43s).
✅ Pushed: [a1b2c3d4](https://github.com/.../commit/a1b2c3d4).
✅ Wiki: 4 fichier(s) (1 créé(s), 3 maj).
✅ Debrief posté: thread `1234567890`.
```

⏳ pour la phase courante, ✅ pour les phases passées, ⚠️ + raison brève sur erreur. Le status message reste en place après le ✅ final (pas d'effacement) — log technique léger qui complète le recap meeting-debrief posté dans `#hermes-perso`.

## Pitfalls

- **Pas de `pip install`** : tout passe par `uv run --with …`.
- **Pas d'appel API LLM direct** : ingest et debrief passent par `hermes -z` avec le provider GPT/OAuth configuré.
- **Pourquoi l'ingest hermes n'a pas de tools** : `hermes -z` tourne en YOLO mode avec le toolset config complet — lui donner des tools = relâcher l'agent-orchestrateur qu'on a banni. Le contexte est embarqué dans le prompt (limité par MAX_ARG_STRLEN 128KB argv → budgets de bytes dans ingest_plan.py) et la sortie est un plan JSON borné, validé et appliqué par le code.
- **Le brief llm-wiki est lu à runtime** depuis `LLM_WIKI_SKILL_PATH` (`/opt/hermes/skills/research/llm-wiki/SKILL.md` upstream) et embarqué (tronqué à 8KB) dans le prompt du plan. Si le fichier est absent (mount manquant), un fallback minimal embarqué dans `pipeline.py` prend le relais — l'ingest sera moins riche mais pas cassé.
- **Le LLM d'ingest n'a PAS access à `write_file`, `git`, `bash`** : l'override injecté en system prompt le précise, mais c'est aussi mécaniquement vrai (les seuls tools déclarés sont `read_file`, `list_dir`, `propose_edit`, `done`). `propose_edit` BUFFERE — aucune écriture filesystem possible côté LLM.
- **Dry-run avant apply** : les edits individuellement invalides (patch dont le `find` chevauche un edit précédent, cible manquante, `replace` sur fichier tronqué) sont écartés AVANT l'apply — reportés dans `dropped_edits`, dumpés dans `.craig-pipeline-debug/` et mentionnés dans le message Discord. Un plan entièrement écarté est signalé ⚠️ (pas confondu avec « aucune édition proposée »).
- **Guards anti-vandalisme** : line-loss >30% sur un fichier existant, regex hallucination (`File unchanged since last read`, `<system-reminder>`, etc.), nouveau fichier >5000 lignes, total >20 fichiers touchés. Si **un seul** guard échoue, **tous** les edits appliqués (les survivants du dry-run) sont revertés via `git checkout` (working tree restauré, transcript déjà committé reste committé).
- **Discord rate-limits** : ~1 PATCH par phase boundary par recording, on est très loin des limites.

## Verification

Après un tick réussi sur 1 recording :

1. `git log --oneline -n 2` côté wiki montre `wiki: ingest <basename>` et `raw: capture transcript <basename>`.
2. `$WIKI_PATH/.craig-pending/<craig_id>.json` n'existe plus.
3. Status message dans `#craig-events` 100% en ✅.
4. Recap meeting-debrief posté dans `#hermes-perso` avec thread.
5. Si dernier pending : ✅ final dans `#hermes-perso` + cron removed.

En cas d'ingest aborté par guard : `$WIKI_PATH/.craig-pipeline-debug/<timestamp>.json` contient le buffer d'edits + raison du guard fail. Working tree wiki est intact (commit transcript OK, pas de commit ingest). Les edits écartés au dry-run produisent aussi un dump (`dry-run-dropped: …`) même quand le reste de l'ingest committe.

## Anti-patterns LLM (côté tick orchestrateur, PAS côté pipeline.py)

- ❌ Ne **pas** invoquer ce script en mode @mention user : il fait des effets de bord (POST Discord, push git, suppression cron). Si l'user pose une question status, lis directement `ls $WIKI_PATH/.craig-pending/`.
- ❌ Ne **pas** « optimiser » en activant `craig-watch` ou `llm-wiki` ou `meeting-debrief` en parallèle : doublon d'effets de bord, race conditions garanties.
- ❌ Ne **pas** parser le JSON de sortie pour « réessayer si erreur » côté LLM : le script gère ses retries en interne. Si une étape garde le pending, le tick suivant la reprendra naturellement.
- ❌ Ne **pas** invoquer `--ingest-only <path>` depuis un tick cron ou une session LLM : flag opérateur (replay manuel d'un ingest raté, humain au clavier). Il court-circuite le flow pending/Discord et dépense du LLM ; un path inventé échoue fast (`transcript-not-found`, exit 2), et l'exit code reflète l'ingest interne (0 committed/no-op, 1 aborted/llm-error).

## Notes

- **Pourquoi un script déterministe et pas plus de prompts LLM ?** Push-fail, ingest-skip, et vandalisme d'index.md sont des modes d'échec LLM observés en prod le 2026-04-26. Code can't lie.
- **`.craig-pipeline-debug/`** est gitignoré (state runtime, pas knowledge).

---
name: craig-transcript-record
description: "Transcrit UN recording Craig (par recording ID) depuis Google Drive vers raw/transcripts/ d'un wiki llm-wiki, puis enchaîne llm-wiki ingest. Idempotent via drive_id. Invoqué par craig-listener (auto, sur événement Discord) ou craig-scan (manuel, rattrapage backlog) — pas en mode auto-scan."
version: 3.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, raw-capture, llm-wiki, gdrive, groq, craig]
    category: voice
    related_skills: [llm-wiki, craig-listener, craig-scan]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki (ex. /opt/data/wiki). Le skill llm-wiki utilise la même variable."
    required_for: full functionality
  - name: GITHUB_TOKEN
    prompt: "Token GitHub avec droit d'écriture sur le repo KB (refresh auto via la GitHub App de l'instance). Utilisé pour le push final."
    required_for: full functionality
  - name: GOOGLE_APPLICATION_CREDENTIALS
    prompt: "Chemin absolu vers le JSON du Service Account Google (ex. /opt/data/google-drive-sa.json). Le SA doit avoir le dossier Drive partagé avec lui (rôle Viewer minimum)."
    required_for: full functionality
  - name: AUDIO_DRIVE_FOLDER_ID
    prompt: "ID du dossier Google Drive scanné par le skill. Copie depuis l'URL Drive: drive.google.com/drive/folders/<ID>. Tout zip Craig auto-uploadé là sera transcrit (à condition qu'on connaisse son recording ID)."
    required_for: full functionality
  - name: GROQ_API_KEY
    prompt: "Clé API Groq (https://console.groq.com/keys) pour la transcription Whisper Large v3 Turbo."
    required_for: full functionality
---

# Voice Transcript

Transcrit **un** recording Craig (identifié par son recording ID) depuis le dossier Google Drive d'auto-upload Craig vers la couche **raw/** d'un wiki [llm-wiki](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/llm-wiki/SKILL.md), puis enchaîne immédiatement l'opération `ingest` du skill `llm-wiki`.

Idempotent : un raw existant pour ce recording (matché par `drive_id` ou `craig_id`) est skippé silencieusement.

## Architecture (V3)

Ce skill est une **primitive de transcription**. Il n'écoute rien, ne scanne rien tout seul. Il est invoqué par :

- **`craig-listener`** — auto, sur événement Discord. Quand Craig poste un message « Recording ID: X » dans le `#craig-events` de cette instance, `craig-listener` extrait l'ID et appelle ce skill.
- **`craig-scan`** — manuel, rattrapage backlog. L'utilisateur demande « scanne les recordings qu'on a ratés » → `craig-scan` lit l'historique du `#craig-events`, dédupe, et appelle ce skill pour chaque ID nouveau.

Le **routing entre instances** Hermes (perso / piloti / telluris) se fait au niveau Discord : chaque instance a un rôle qui ne voit que SON `#craig-events`. Pas de filtrage côté skill — si tu as été invoqué, ce recording est pour toi.

## When to Use

Active-toi **uniquement** quand un autre skill (`craig-listener` ou `craig-scan`) ou l'utilisateur fournit un `recording_id` Craig explicite. Cas typiques :

- `craig-listener` te passe un ID extrait d'un événement Discord.
- `craig-scan` te passe un ID issu de l'historique d'un `#craig-events`.
- L'utilisateur mentionne une URL `craig.horse/rec/<id>` ou colle un message Craig en chat — extrais l'ID (regex `Recording ID:\s*([A-Za-z0-9_-]+)` ou `craig\.horse/rec/([A-Za-z0-9_-]+)` — Craig accepte `-` et `_` dans les IDs) et invoque-toi.

**Ne pas utiliser** pour de l'audio non-Craig (mp3/m4a/wav direct, voice memos, exports Zoom). La V3 ne supporte que les zips Craig — un audio direct n'a pas de recording ID, donc rien à appeler. (Si ce besoin revient, on créera un skill séparé.)

## Procedure

### Conventions runtime

Avant de lancer, observe ces règles propres à l'environnement Hermes :

- **Python via `uv run`, jamais `pip`.** L'image upstream n'a pas pip. Pour le script : `uv run --with google-auth --with google-api-python-client --with requests /opt/data/skills-shared/craig-transcript-record/scan.py --craig-id <ID>`.
- **Workdir = `tempfile.mkdtemp()`.** Ne jamais écrire un audio ou un script dans `/opt/data/`, `/opt/hermes/`, ou `$WIKI_PATH`. Le script gère ça lui-même.
- **Auth git = transparente.** Un credential helper côté infra lit `$GITHUB_TOKEN` à chaque op. Tu fais juste `git pull / commit / push` — pas de `git remote set-url`, pas d'`export GITHUB_TOKEN`. Ces commandes déclenchent le scanner de sécurité Hermes pour rien.
- **Env vars chargées au container start** via Docker `env_file:`. Accès via `os.environ[...]`. Ne tente pas de les recharger d'un .env.

### 1. Pre-flight

```bash
command -v uv && command -v ffmpeg && \
  for v in WIKI_PATH GOOGLE_APPLICATION_CREDENTIALS AUDIO_DRIVE_FOLDER_ID GROQ_API_KEY GITHUB_TOKEN; do \
    [ -n "${!v}" ] || { echo "missing $v"; exit 1; }; \
  done && echo "ok"
```

Si une étape fail, abandonne et signale ce qui manque.

### 2. Lance `scan.py`

Le script est versionné à côté de ce SKILL.md, monté dans le container à `/opt/data/skills-shared/craig-transcript-record/scan.py`. **Ne le réécris pas, ne le copie pas, ne fais pas de variante.**

```bash
uv run --with google-auth --with google-api-python-client --with requests \
    /opt/data/skills-shared/craig-transcript-record/scan.py --craig-id <RECORDING_ID>
```

Le script :

1. Vérifie l'idempotence locale (raw existant pour ce `craig_id`) — skip cheap si déjà fait.
2. Poll Drive jusqu'à voir `craig_<id>_*.flac.zip` apparaître. Backoff 30→60→120 s, **timeout 20 min** (couvre le cook + upload Craig worst-case).
3. Re-vérifie l'idempotence par `drive_id`.
4. Download le zip, extrait les `.flac` (un par speaker), mixe via `ffmpeg amix` si N>1.
5. Transcrit via Groq Whisper Large v3 Turbo. Si l'audio mixé dépasse la limite Groq (25 MB), le script découpe automatiquement le FLAC en chunks ~20 MB via `ffmpeg -f segment`, transcrit chaque chunk séparément, puis fusionne les `segments` en ré-appliquant le décalage temporel — la timeline finale reste alignée sur l'enregistrement original.
6. Écrit `raw/transcripts/<date>-craig-<id>-<date>.md` avec frontmatter complet.
7. Émet **un seul** objet JSON sur stdout :

```json
{"status": "processed", "path": "raw/transcripts/2026-04-25-craig-xxx.md", "drive_id": "...", "name": "craig_xxx_2026-04-25_14-37-58.flac.zip"}
{"status": "skipped",   "reason": "already-transcribed", "as": "raw/transcripts/..."}
{"status": "skipped",   "reason": "race-already-written"}
{"status": "error",     "reason": "drive-poll-timeout|empty-transcription|ffmpeg-failed|groq-http-error|bad-craig-id|unexpected", "detail": "..."}
```

Exit codes : `0` pour `processed` ou `skipped`, `1` pour `error` runtime, `2` pour erreur d'argument.

### 3. Si `status == "processed"` : commit + push

```bash
cd "$WIKI_PATH"
git pull --rebase
git add "<path renvoyé par scan.py>"
git commit -m "raw: capture transcript $(basename '<path>' .md)"
git push
```

Si le push échoue avec une erreur d'auth, attends 60 s (le sidecar refresh le token toutes les 45 min) et retry une fois. Au-delà, log et stoppe — le raw est commité localement, le push se rattrape au run suivant.

### 4. Si `status == "processed"` : enchaîne `llm-wiki ingest`

Active le skill `llm-wiki` (`skill_view("llm-wiki")` si pas en contexte) et applique son opération **`ingest`** sur le fichier renvoyé par `scan.py`. En automated mode (déclenchement par `craig-listener` ou cron), saute le step de validation utilisateur.

Si l'ingest échoue, **ne fail pas tout** : le raw est déjà commité. Notifie ⚠️ et signale ce qui a bloqué.

### 5. Si `status == "skipped"`

Silence côté Discord (déjà transcrit, rien à faire). Log et stop.

### 6. Si `status == "error"`

Notifie l'utilisateur (s'il y a un interlocuteur) avec `reason` + `detail`. Cas particulier `drive-poll-timeout` : suggère de vérifier que Craig a bien fini le recording (le bot a peut-être planté, ou l'auto-upload Drive est cassé).

### 7. Confirme

Quand tout est OK :

```
✅ Transcript intégré : `<filename>`
   📥 Capture : raw/transcripts/<filename>
   🧠 Ingest : <N> page(s) wiki créée(s)/updatée(s) — [[page-a]], [[page-b]]
```

## Pitfalls

- **Ne touche jamais à `raw/` après écriture** — sources immuables (cf. SKILL.md llm-wiki). Les corrections vivent dans des pages wiki.
- **Ne touche pas à `index.md` ni à `log.md`** — c'est le job de l'opération `ingest` du skill `llm-wiki`.
- **Frontmatter raw ≠ frontmatter wiki** — n'utilise PAS `title`, `tags`, `type`, `sources` dans `raw/`. Le frontmatter raw est minimal (source / source_url / drive_id / drive_md5 / recorded_at / ingested / sha256).
- **Le `sha256` se calcule sur le body uniquement** (après le `---` de fermeture). Sinon le hash dérive avec `ingested:` et la dédup est cassée.
- **Pas de diarisation** : Groq Whisper ne sépare pas les speakers. Tout le body est `???` sauf si un seul speaker est connu.
- **Audio sans paroles** : Groq retourne `segments: []` → `status: error / empty-transcription`. Aucun raw n'est écrit.
- **Audio > 25 MB** : géré automatiquement par découpe ffmpeg en chunks ~20 MB, transcription par chunk, puis fusion des segments avec offset temporel. Aucune action côté agent — surveiller juste les progress events `groq-split` / `groq-chunk` (utiles pour un message de statut Discord live).
- **SA sans accès au dossier** : `files.list` renvoie une liste vide (pas une erreur de permission). Le script va donc poll 20 min puis renvoyer `drive-poll-timeout`. Si ça arrive systématiquement, vérifier que `AUDIO_DRIVE_FOLDER_ID` est partagé avec l'email du SA.
- **Pas de filtrage par channel/instance** : V3 ne fait AUCUNE décision de routing. Le routing est garanti par les permissions Discord (chaque instance ne voit que son `#craig-events`). Si tu reçois un recording ID, il est pour toi.
- **Pas de `git remote set-url` avec token, ni `export GITHUB_TOKEN=...`** : le credential helper infra fait le boulot. Ces commandes déclenchent le scanner de sécurité Hermes inutilement.
- **Pas de `pip install`** : indispo dans l'image upstream. Toujours `uv run --with <pkg>`.
- **Pas d'écriture de fichiers dans `/opt/data/`, `/opt/hermes/`** : le script utilise `tempfile.TemporaryDirectory()`.

## Verification

Après exécution, vérifie pour le recording traité :

1. `$WIKI_PATH/raw/transcripts/<filename>.md` existe avec le frontmatter complet (`source`, `source_url`, `drive_id`, `drive_md5`, `recorded_at`, `ingested`, `sha256`).
2. Le `sha256` correspond bien au hash du body (re-calcule).
3. `git log` dans `$WIKI_PATH` montre le commit "raw: capture transcript …".
4. Au moins une page dans `entities/` ou `concepts/` mentionne le fichier raw dans son `sources:` (sauf si l'ingest n'a rien jugé promotable).
5. `log.md` contient une entrée `## [YYYY-MM-DD] ingest | <titre>`.
6. `git push` a réussi pour les commits raw et ingest.

## Notes

- **Auth Drive** : Service Account via `GOOGLE_APPLICATION_CREDENTIALS`, scoppé aux dossiers explicitement partagés avec l'email du SA.
- **Coût** : Craig $8/mois Patreon Tier 3 (recording + auto-upload Drive) ; Groq ~$0.04/h audio (free tier généreux pour usage perso).
- **Latence end-to-end** : upload Drive (1-15 min post-recording) + Groq (~0.1× realtime) ≈ 2-20 min pour 1h d'audio. Le script bloque pendant le poll Drive — c'est normal.
- **Pourquoi pas d'auto-ingest sans seuil** : `llm-wiki` impose des seuils stricts (≥ 2 mentions ou central à 1 source). Cette décision relève de l'agent au moment de l'ingest, pas de ce skill.

---
name: voice-transcript
description: "Scanne un dossier Google Drive, transcrit via Groq Whisper tout audio nouveau (zips Craig, voice notes, exports Zoom, mp3/m4a/wav/flac/ogg), dépose le résultat dans raw/transcripts/ d'un wiki llm-wiki et enchaîne l'opération ingest du skill llm-wiki. Idempotent via sha256."
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, raw-capture, llm-wiki, gdrive, groq]
    category: voice
    related_skills: [llm-wiki]
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
    prompt: "ID du dossier Google Drive scanné par le skill. Copie depuis l'URL Drive: drive.google.com/drive/folders/<ID>. Tout audio déposé là (par Craig en auto-upload OU par l'utilisateur manuellement) sera transcrit."
    required_for: full functionality
  - name: GROQ_API_KEY
    prompt: "Clé API Groq (https://console.groq.com/keys) pour la transcription Whisper Large v3 Turbo."
    required_for: full functionality
  - name: HERMES_OWNED_CHANNEL_IDS
    prompt: "Liste CSV d'IDs de channels Discord 'possédés' par cette instance (ex. 1497178585759485952,1497178585759485953). Chaque zip Craig contient un info.txt qui mentionne le Channel ID; le skill ne traite que les zips dont le Channel ID est dans cette liste. On filtre sur les channels et non sur la guild car plusieurs instances Hermes peuvent vivre sur le même serveur Discord, chacune écoutant un sous-ensemble de salons. Vide ou non défini = pas de filtrage (mode mono-instance)."
    required_for: multi-instance setups
---

# Voice Transcript

Scanne un dossier Google Drive, transcrit via **Groq Whisper Large v3 Turbo** tout fichier audio nouveau, dépose le résultat dans la couche **raw/** d'un wiki [llm-wiki](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/llm-wiki/SKILL.md), puis enchaîne immédiatement l'opération `ingest` du skill `llm-wiki` pour promouvoir le transcript en pages wiki.

Rationale : un transcript capturé sans être ingéré, c'est de la connaissance dormante. Et un audio dans Drive non transcrit, c'est encore pire — invisible au système.

Le skill est **idempotent** : il calcule un identifiant déterministe par fichier Drive et skip ceux déjà transcrits. Tu peux le ré-activer N fois, il ne refera pas le travail.

## Sources d'audio supportées

Tout fichier déposé dans le dossier Drive `AUDIO_DRIVE_FOLDER_ID` :

- **Zip Craig** (`craig_<id>_*.flac.zip`) — auto-uploadé par Craig quand un recording Discord se termine. Contient un ou plusieurs `.flac` (un par speaker), `info.txt`, `raw.dat`. Le skill extrait les `.flac`, les mixe si >1 (via `ffmpeg`), et envoie le mix à Groq.
- **Audio direct** (`.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`) — déposé manuellement par l'utilisateur (export Zoom, voice memo, podcast, etc.). Envoyé à Groq tel quel.

Pas besoin d'URL ni de nom de fichier : le skill liste, filtre, et traite tout ce qui est nouveau.

## Cohabitation multi-instances

Plusieurs instances Hermes (`perso`, `piloti`, `telluris`) peuvent partager le **même dossier Drive** (Craig n'a qu'une seule destination d'upload par compte Patreon, et plusieurs instances peuvent vivre sur le même serveur Discord avec des channels distincts). Le filtrage par "instance owner" se fait au niveau de `scan.py` via `HERMES_OWNED_CHANNEL_IDS` — **purement déterministe, pas de décision LLM** :

- Pour chaque zip Craig, `scan.py` ouvre `info.txt` (présent à l'intérieur du zip) et y lit le **Channel ID Discord** (ligne `Channel: <name> (<id>)` — la regex capture uniquement l'ID numérique, pas le nom, donc renommer un channel ne casse rien).
- Si le Channel ID n'est pas dans `HERMES_OWNED_CHANNEL_IDS`, le zip est marqué `foreign-channel` et ignoré silencieusement — c'est le boulot d'une autre instance.
- Si `HERMES_OWNED_CHANNEL_IDS` est vide ou non défini, **aucun filtrage** — utile en mono-instance ou pour debug.

Pour les **audio directs** (sans `info.txt`), il n'y a pas de discriminator. Conséquence :
- En **mode auto-scan** (cron, ou demande générique « scanne le drive »), les audio directs sont **ignorés** quand `HERMES_OWNED_CHANNEL_IDS` est non vide (`manual-audio-no-discriminator`), pour éviter le traitement parallèle par plusieurs instances.
- En **mode trigger explicite** (`--filename meeting.mp3`, ou demande utilisateur ciblée sur un fichier précis), le filtrage Channel est court-circuité et le fichier est traité par l'instance qui reçoit la demande.

## When to Use

Active-toi dans l'un des cas suivants :

1. **Demande explicite** de l'utilisateur dans Discord (DM ou un salon où Hermes écoute) :
   - « transcris ce qui est dans Drive »
   - « scanne le dossier audio »
   - « il y a un fichier dans Drive, ingère-le »
   - « check les nouveaux enregistrements »
2. **Trigger automatique** par un cron Hermes (s'il en existe un sur cette instance — voir `cron/` du vault). Le cron passe l'argument `mode=scan` qui équivaut au cas 1 mais sans interlocuteur à notifier.
3. **Mention d'une URL craig.horse** dans un message : extrais le `recording_id` (regex `craig\.horse/rec/([A-Za-z0-9]+)`) et utilise-le comme filtre de nom dans la requête Drive (`name contains 'craig_<id>_'`). L'URL n'est PAS suivie — la source réelle reste Drive.

## Scope (deux phases enchaînées)

**Phase A — Capture (ce skill)** : scan Drive → identifie nouveautés → télécharge → transcrit via Groq → dépose dans `raw/transcripts/` → commit + push.

**Phase B — Promotion en pages wiki (skill `llm-wiki`)** : activée immédiatement après la phase A si un nouveau raw a été écrit. Ce skill ne touche PAS aux pages wiki, c'est `llm-wiki ingest` qui le fait.

→ L'utilisateur n'a rien d'autre à faire que de déposer un audio dans le dossier Drive.

## Procedure

### Conventions runtime

Avant de coder quoi que ce soit, observe ces 4 règles propres à l'environnement Hermes — elles t'éviteront 80 % des tâtonnements :

- **Python via `uv run`, jamais `pip`.** `pip` n'est pas dispo dans l'image upstream. Pour exécuter un script avec dépendances : `uv run --with google-auth --with google-api-python-client --with requests --with python-dotenv ./script.py`. Pour un one-liner : `uv run --with requests python -c '...'`.
- **Workdir = `tempfile.mkdtemp()`.** N'écris jamais de script ou de fichier audio dans `/opt/data/`, `/opt/hermes/`, ou `$WIKI_PATH`. Tout dans un tmpdir, supprimé en fin de run.
- **Auth git = transparente.** Un credential helper est posé sur l'instance via `~/.gitconfig` (vit dans `/opt/data/home/.gitconfig`) qui lit `$GITHUB_TOKEN` à chaque op et fournit l'auth pour github.com. **Tu fais juste `git pull`, `git commit`, `git push` — sans token, sans `remote set-url`, sans header.** Tout fonctionne. NE FAIT PAS `export GITHUB_TOKEN=...` ni `git remote set-url ...@github.com/...` — ça déclenche les scanners de sécurité pour rien et ça ne sert à rien.
- **Env vars chargées au container start.** `GROQ_API_KEY`, `AUDIO_DRIVE_FOLDER_ID`, `GITHUB_TOKEN`, etc. sont injectées via Docker `env_file:`. Tu y accèdes par `os.environ[...]`. Ne tente pas de les recharger d'un .env.

### 1. Acknowledge et oriente-toi

- Si la trigger est un message utilisateur Discord, réagis ⏳ pour signaler que c'est en cours.
- Lis `$WIKI_PATH/SCHEMA.md` (taxonomie raw, sous-dossiers de `raw/`).
- Vérifie que `$WIKI_PATH` est un wiki llm-wiki valide (présence de `SCHEMA.md`, `index.md`, `log.md`). Si non, abandonne et signale-le.

Pre-flight check (1 commande, doit passer avant le scan) :

```bash
command -v uv && command -v ffmpeg && \
  for v in WIKI_PATH GOOGLE_APPLICATION_CREDENTIALS AUDIO_DRIVE_FOLDER_ID GROQ_API_KEY GITHUB_TOKEN; do \
    [ -n "${!v}" ] || { echo "missing $v"; exit 1; }; \
  done && echo "ok"
```

Si une étape fail, abandonne et signale précisément quoi manque.

### 2. Lance `scan.py` (script versionné, livré avec le skill)

Le script de capture (Phase A) est **versionné dans le repo skills**, à côté de ce SKILL.md : `voice-transcript/scan.py`. Il fait tout en code déterministe — listing Drive, dédup via `drive_id`, lecture de `info.txt` pour le filtrage par Channel ID, mix ffmpeg multi-pistes, Groq, écriture du raw avec frontmatter complet. **Ne le réécris pas, ne le copie pas dans un tmpdir, ne fais pas de variante.** Tu l'invoques tel quel.

Chemin canonique côté container : `/opt/data/skills-shared/voice-transcript/scan.py`.

```bash
# Mode auto-scan (cron ou demande générique « scanne le drive »)
uv run --with google-auth --with google-api-python-client --with requests \
    /opt/data/skills-shared/voice-transcript/scan.py

# Mode trigger explicite (l'utilisateur cite un nom de fichier précis)
uv run --with google-auth --with google-api-python-client --with requests \
    /opt/data/skills-shared/voice-transcript/scan.py --filename "meeting.mp3"

# Mode URL Craig (extrait le recording_id de l'URL et restreint au zip correspondant)
uv run --with google-auth --with google-api-python-client --with requests \
    /opt/data/skills-shared/voice-transcript/scan.py --craig-id "ZLY9jcKKaL5x"
```

La sortie est un JSON :

```json
{
  "new":     ["raw/transcripts/2026-04-25-craig-xxx.md", ...],
  "skipped": [{"name": "...", "reason": "already-transcribed", "as": "..."}, ...],
  "errors":  [{"name": "...", "error": "..."}]
}
```

Sémantique des `reason` côté `skipped` :
- `already-transcribed` : `drive_id` déjà présent dans un raw existant, idempotence OK
- `foreign-channel` : zip Craig provenant d'un Channel Discord pas dans `HERMES_OWNED_CHANNEL_IDS` → c'est le boulot d'une autre instance Hermes
- `no-channel-info` : `info.txt` absent ou ne mentionne pas de Channel ID → on ne sait pas à qui ça appartient, on skip par sécurité (uniquement en auto-scan, pas en trigger explicite)
- `manual-audio-no-discriminator` : audio direct (sans `info.txt`) en mode auto-scan multi-instance → on évite que les 3 instances le transcrivent en parallèle. À traiter via trigger explicite (`--filename`).
- `empty-transcription` : Groq a rendu 0 segment (silence)
- `race-already-written` : un autre process a écrit le fichier entre la dédup et l'écriture (rare)

Parse le JSON pour la suite. Si `new` est vide et il n'y a que des `skipped` (raisons normales), signale « rien de nouveau » et stoppe (silencieux côté Discord si trigger = cron).

Si `skipped` contient des `"error"` → log les erreurs, continue avec les `new` malgré tout.

### 3. Commit + push des nouveaux raws

Pour chaque entrée de `new` :

```bash
cd "$WIKI_PATH"
git pull --rebase
git add "<chemin>"
git commit -m "raw: capture transcript $(basename '<chemin>' .md)"
git push
```

L'auth est gérée par le credential helper installé côté infra (lit `$GITHUB_TOKEN` à chaque op), aucune manipulation de token n'est nécessaire. Si le push échoue avec une erreur d'auth, attends 60 s (le sidecar refresh le token toutes les 45 min) et retry une fois. Au-delà, log et passe au candidat suivant — le raw est déjà committé localement, le push se rattrape au prochain run.

### 4. Enchaîne l'ingestion (Phase B)

Pour chaque entrée de `new`, active le skill `llm-wiki` (`skill_view("llm-wiki")` si pas en contexte) et applique son opération **`ingest`** sur le fichier.

Suis intégralement la procédure d'ingest du skill `llm-wiki` (section `## Core Operations → 1. Ingest`). En automated mode, tu enchaînes sans demander de validation à l'utilisateur (le skill llm-wiki prévoit ce mode au step ② : "Skip this in automated/cron contexts — proceed directly").

Si l'ingest échoue : ne fail pas tout. Le raw a déjà été commité. Notifie ⚠️ et signale clairement à l'utilisateur ce qui a bloqué.

### 5. Confirme l'enchaînement

Une fois la phase A et B réussies pour un audio, poste un message court (Discord ou stdout selon trigger) :

```
✅ Transcript intégré : `<filename>`
   📥 Capture : raw/transcripts/<filename>
   🧠 Ingest : <N> page(s) wiki créée(s)/updatée(s) — [[page-a]], [[page-b]]
```

Si plusieurs candidats ont été traités dans le même run (mode scan), regroupe en une notification finale :

```
✅ <N> transcripts intégrés.
   - [[2026-04-25-meeting-piloti]] (3 pages wiki)
   - [[2026-04-25-voice-memo]] (1 page wiki)
```

Si rien de nouveau dans Drive : message court silencieux côté Discord (ou rien si trigger cron).

## Pitfalls

- **Ne touche jamais à `raw/` après écriture** — sources immuables (cf. SKILL.md llm-wiki). Les corrections vivent dans des pages wiki.
- **Ne touche pas à `index.md` ni à `log.md`** — c'est le job de l'opération `ingest` du skill `llm-wiki`.
- **Frontmatter raw ≠ frontmatter wiki** — n'utilise PAS `title`, `tags`, `type`, `sources` dans `raw/`. Le frontmatter raw est minimal (source / drive_id / drive_md5 / recorded_at / ingested / sha256).
- **Le `sha256` se calcule sur le body uniquement** (après le `---` de fermeture du frontmatter). Sinon le hash dérive avec `ingested:` et la dédup est cassée.
- **Pas de diarisation** : Groq Whisper ne sépare pas les speakers. Tout le body est `???` sauf si un seul speaker est connu. Ne bluffe pas l'attribution.
- **Audio sans paroles** (silence, noise) : Groq retourne `segments: []`. Notifie, n'écris rien dans `raw/`.
- **Audio > 25 MB** : abandonne avec erreur claire. Suggère re-encoding (`ffmpeg -ab 96k -ar 16000`) ou découpe.
- **SA sans accès au dossier** : `files.list` renvoie une liste vide (pas une erreur de permission). Si tu trouves systématiquement zéro candidat, vérifie que `AUDIO_DRIVE_FOLDER_ID` est partagé avec l'email du SA avant de paniquer.
- **Rate limit Groq (429)** : back-off 30s, retry 3 fois max, puis abandonne et passe au candidat suivant.
- **Réseau qui drop** (Drive ou Groq) : retry 3 fois avec backoff exponentiel (30s → 60s → 120s).
- **Mode scan automatique sans trigger user** (cron) : pas de réaction emoji ni de message Discord si rien de nouveau, pour éviter le bruit.
- **Pas de `git remote set-url` avec token embarqué, ni `export GITHUB_TOKEN=...`** : le credential helper installé côté infra fait le boulot. Ces commandes déclenchent inutilement le scanner de sécurité de Hermes (qui te demandera une approbation à chaque fois) et n'apportent rien. Fais simplement `git pull/commit/push`.
- **Pas de `pip install`** : indispo dans l'image upstream. Toujours `uv run --with <pkg>` pour exécuter avec deps.
- **Pas d'écriture de fichiers dans `/opt/data/`, `/opt/hermes/`** : tout dans un `tempfile.mkdtemp()` ou `mktemp -d`, supprimé en fin de run.

## Verification

Après exécution, vérifie pour chaque audio traité :

1. `$WIKI_PATH/raw/transcripts/<filename>.md` existe avec le bon frontmatter (`source`, `drive_id`, `drive_md5`, `recorded_at`, `ingested`, `sha256`).
2. Le `sha256` dans le frontmatter correspond bien au hash du body (re-calcule).
3. `git log` dans `$WIKI_PATH` montre le commit "raw: capture transcript …".
4. Au moins une page dans `entities/` ou `concepts/` mentionne le fichier raw dans son frontmatter `sources:` (sauf si l'ingest n'a rien jugé promotable selon les seuils).
5. `log.md` contient une entrée `## [YYYY-MM-DD] ingest | <titre>` listant les pages touchées.
6. `git push` a réussi pour les commits raw et ingest.

Si un point échoue, signale-le clairement plutôt que de prétendre que ça a marché.

## Notes

- **Auth Drive** : Service Account via `GOOGLE_APPLICATION_CREDENTIALS`. Accès scoppé aux dossiers explicitement partagés avec l'email du SA — pas d'accès aux autres dossiers Drive de l'utilisateur.
- **Coût** :
  - Côté Craig (si utilisé) : $8/mois Patreon Tier 3 (recording + auto-upload Drive). La transcription Craig interne est ignorée par ce skill.
  - Côté Groq : ~$0.04/h audio (ou free tier généreux pour usage perso).
- **Latence end-to-end** :
  - Source Craig : upload Drive (1-5 min post-recording) + Groq (~0.1× realtime) ≈ 2-7 min pour 1h d'audio.
  - Source manual : Groq seul ≈ 6 min pour 1h.
- **Pourquoi pas d'auto-ingest sans seuil** : le skill llm-wiki impose des seuils stricts (≥ 2 mentions ou central à 1 source pour créer une page). Cette décision relève de l'agent au moment de l'ingest, pas du skill de capture.

## Cron (optionnel)

Pour que le scan tourne tout seul après chaque call (sans que l'utilisateur ait à demander), configure un cron Hermes :

```yaml
# Exemple, à placer dans le système cron de Hermes (vault cron/ ou config.yaml selon version)
- name: voice-transcript-scan
  cron: "*/10 * * * *"   # toutes les 10 min
  prompt: "Active le skill voice-transcript en mode scan : check le dossier Drive et transcris/ingère tout ce qui est nouveau. Si rien, ne notifie rien."
```

Cadence raisonnable : 5-10 min. Plus serré que ça, c'est inutile (Craig met 1-5 min à uploader). Plus large, c'est juste de la latence.

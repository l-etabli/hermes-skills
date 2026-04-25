---
name: voice-transcript
description: "Scanne un dossier Google Drive, transcrit via Groq Whisper tout audio nouveau (zips Craig, voice notes, exports Zoom, mp3/m4a/wav/flac/ogg), dépose le résultat dans raw/transcripts/ d'un wiki llm-wiki et enchaîne l'opération ingest du skill llm-wiki. Idempotent via sha256."
version: 1.0.0
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

## When to Use

Active-toi dans l'un des cas suivants :

1. **Demande explicite** de l'utilisateur dans Discord (DM ou un salon où Hermes écoute) :
   - « transcris ce qui est dans Drive »
   - « scanne le dossier audio »
   - « il y a un fichier dans Drive, ingère-le »
   - « check les nouveaux enregistrements »
2. **Trigger automatique** par un cron Hermes (s'il en existe un sur cette instance — voir `cron/` du vault). Le cron passe l'argument `mode=scan` qui équivaut au cas 1 mais sans interlocuteur à notifier.
3. **Mention d'une URL craig.horse** dans un message (legacy) : extrais le `recording_id` de l'URL pour cibler spécifiquement ce zip dans Drive (au cas où plusieurs zips matchent et que tu veux le bon). L'URL n'est PAS suivie — la source réelle reste Drive.

## Scope (deux phases enchaînées)

**Phase A — Capture (ce skill)** : scan Drive → identifie nouveautés → télécharge → transcrit via Groq → dépose dans `raw/transcripts/` → commit + push.

**Phase B — Promotion en pages wiki (skill `llm-wiki`)** : activée immédiatement après la phase A si un nouveau raw a été écrit. Ce skill ne touche PAS aux pages wiki, c'est `llm-wiki ingest` qui le fait.

→ L'utilisateur n'a rien d'autre à faire que de déposer un audio dans le dossier Drive.

## Procedure

### 1. Acknowledge et oriente-toi

- Si la trigger est un message utilisateur Discord, réagis ⏳ pour signaler que c'est en cours.
- Lis `$WIKI_PATH/SCHEMA.md` (taxonomie raw, sous-dossiers de `raw/`).
- Vérifie que `$WIKI_PATH` est un wiki llm-wiki valide (présence de `SCHEMA.md`, `index.md`, `log.md`). Si non, abandonne et signale-le.

### 2. Liste les audios candidats dans Drive

**Auth** : libs `google-auth` + `google-api-python-client` lisent `GOOGLE_APPLICATION_CREDENTIALS` automatiquement. Installe-les si absentes : `pip install --quiet google-auth google-api-python-client`.

```python
from googleapiclient.discovery import build
from google.oauth2 import service_account
import os

creds = service_account.Credentials.from_service_account_file(
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
    scopes=["https://www.googleapis.com/auth/drive.readonly"],
)
drive = build("drive", "v3", credentials=creds, cache_discovery=False)
folder_id = os.environ["AUDIO_DRIVE_FOLDER_ID"]

q = (
    f"'{folder_id}' in parents and trashed = false and ("
    "  name contains '.flac.zip' or "
    "  mimeType contains 'audio/' or "
    "  name contains '.mp3' or name contains '.m4a' or "
    "  name contains '.wav' or name contains '.flac' or "
    "  name contains '.ogg'"
    ")"
)
resp = drive.files().list(
    q=q, orderBy="createdTime desc",
    fields="files(id,name,createdTime,size,mimeType,md5Checksum)",
    pageSize=50,
).execute()
candidates = resp.get("files", [])
```

Si une URL craig.horse a été fournie, restreins à `name contains 'craig_<recording_id>_'`.

### 3. Filtre les audios déjà transcrits (idempotence)

Pour chaque fichier candidat, calcule un **drive_marker** déterministe et regarde s'il apparaît déjà dans le frontmatter d'un fichier de `raw/transcripts/`.

Convention : le marker est l'ID Drive du fichier source (`drive_id` court) + son `md5Checksum` Drive. Stocké dans le frontmatter raw.

```python
import pathlib
def already_transcribed(drive_id: str, transcripts_dir: pathlib.Path) -> bool:
    for md in transcripts_dir.glob("*.md"):
        head = md.read_text(encoding="utf-8", errors="replace")[:1500]
        if f"drive_id: {drive_id}" in head:
            return True
    return False
```

Si le candidat est déjà transcrit → skip silencieusement. Si tous le sont, signale « rien de nouveau » et stoppe (pas d'erreur).

Sinon, traite chaque nouveau dans l'ordre `createdTime desc` (du plus récent au plus ancien). Si un fail, log l'erreur et passe au suivant — ne block pas le batch.

### 4. Récupère le binaire audio à transcrire

**Cas A — Zip Craig** : télécharge le zip, extrait tous les `.flac`. Si 1 seul, mixe = lui-même. Si N>1, mixe via `ffmpeg`.

```python
from googleapiclient.http import MediaIoBaseDownload
import io, zipfile, subprocess, tempfile, pathlib

def download_blob(file_id):
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, drive.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf

if name.endswith(".flac.zip"):
    zip_buf = download_blob(file_id)
    flacs = {}
    with zipfile.ZipFile(zip_buf) as zf:
        for zi in zf.infolist():
            if zi.filename.lower().endswith(".flac"):
                flacs[zi.filename] = zf.read(zi)
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    paths = []
    for k, data in flacs.items():
        p = tmpdir / pathlib.Path(k).name
        p.write_bytes(data)
        paths.append(p)
    if len(paths) == 1:
        audio_path = paths[0]
    else:
        audio_path = tmpdir / "mix.flac"
        cmd = ["ffmpeg", "-y"]
        for p in paths:
            cmd += ["-i", str(p)]
        cmd += [
            "-filter_complex", f"amix=inputs={len(paths)}:normalize=0",
            "-c:a", "flac", str(audio_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    speakers = [pathlib.Path(p.name).stem.split("-", 1)[-1] for p in paths]
else:
    # Cas B — audio direct
    audio_buf = download_blob(file_id)
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    audio_path = tmpdir / name
    audio_path.write_bytes(audio_buf.getvalue())
    speakers = []  # inconnus jusqu'à demande utilisateur ou heuristique sur le nom
```

### 5. Transcris via Groq

```python
import requests

if audio_path.stat().st_size > 25 * 1024 * 1024:
    raise RuntimeError(
        f"Audio {audio_path.stat().st_size/1e6:.1f} MB > 25 MB Groq sync limit. "
        "Suggère à l'utilisateur de re-encoder plus bas (ex: ffmpeg -ab 96k -ar 16000) ou découper."
    )

resp = requests.post(
    "https://api.groq.com/openai/v1/audio/transcriptions",
    headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
    files={"file": (audio_path.name, audio_path.read_bytes(), "audio/flac")},
    data={
        "model": "whisper-large-v3-turbo",
        "response_format": "verbose_json",
        "language": "fr",     # ou en si l'utilisateur indique l'anglais
        "temperature": "0",
    },
    timeout=300,
)
resp.raise_for_status()
transcription = resp.json()
# transcription["text"], ["segments"], ["duration"], ["language"]
```

**Pas de diarisation** : Groq retourne un seul flux de texte. Pour les zips Craig, on connaît la liste `speakers` extraite des filenames `.flac` → on l'annote dans les métadonnées du body, mais chaque tour de parole reste préfixé `???` sauf si un seul speaker connu (alors préfixe = ce nom).

### 6. Compose et écris le Markdown raw

```markdown
---
source: <voir ci-dessous>
drive_id: <Google Drive file ID>
drive_md5: <md5Checksum Drive>
recorded_at: <YYYY-MM-DD HH:MM>
ingested: <YYYY-MM-DD>
sha256: <hex digest du body, sans frontmatter>
---

# Transcription <titre humain>

- **Source audio** : `<nom du fichier Drive>`
- **Date** : <YYYY-MM-DD HH:MM>
- **Durée** : <Xm Ys>
- **Participants** : <liste extraite ou ???>
- **Transcription** : Groq Whisper Large v3 Turbo (langue détectée : `<lang>`)

[00:03] **???**: premier segment...

[00:18] **???**: segment suivant...
```

**Frontmatter** :
- `source` : `craig` si le fichier est un zip Craig, `manual` si dépôt utilisateur
- `drive_id` + `drive_md5` : pour la dédup (voir section 3)
- `recorded_at` : extraction depuis le nom du zip Craig (pattern `craig_<id>_<YYYY-M-D>_<HH-MM-SS>`), sinon `createdTime` du fichier Drive
- `ingested` : date du jour
- `sha256` : hash du body uniquement (tout après le `---` de fermeture)

**Body** :

```python
def _seconds_to_mmss(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

lines = []
single_speaker = speakers[0] if len(speakers) == 1 else None
for seg in transcription["segments"]:
    ts = _seconds_to_mmss(seg["start"])
    speaker = single_speaker or "???"
    text = seg["text"].strip()
    lines.append(f"[{ts}] **{speaker}**: {text}\n")
body = "\n".join(lines)
```

### 7. Dédup et chemin final

**Chemin** : `$WIKI_PATH/raw/transcripts/<YYYY-MM-DD>-<slug>.md`

- `<YYYY-MM-DD>` = date du recording (frontmatter `recorded_at`)
- `<slug>` (par ordre de préférence) :
  - Source `craig` : `info.txt` du zip contient un nom de salon Discord lisible → slugifier (minuscules, accents retirés, espaces → tirets)
  - Source `manual` : nom du fichier Drive sans extension, slugifié
  - Sinon : `audio-<drive_id_8>` (8 premiers caractères du Drive ID)

**Avant d'écrire** :

1. `git -C "$WIKI_PATH" pull --rebase`
2. Idempotence stricte (déjà filtrée en section 3, double check ici par sécurité) :
   - Si un fichier `<date>-<slug>.md` existe déjà avec le même `drive_id` + `drive_md5` dans son frontmatter → skip
   - Si même nom mais `drive_md5` différent → l'utilisateur a remplacé un fichier ; suffixe le slug `-2`, `-bis`
3. Écris le fichier.

### 8. Commit + push

```bash
cd "$WIKI_PATH"
git add "raw/transcripts/<filename>"
git commit -m "raw: capture transcript <date> <slug>"
git push
```

`$GITHUB_TOKEN` est rafraîchi par le sidecar GitHub App. Si le push échoue avec une erreur d'auth, attends 60s et retry une fois. Au-delà, log et passe au candidat suivant.

### 9. Enchaîne l'ingestion (Phase B)

Active le skill `llm-wiki` (`skill_view("llm-wiki")` si pas en contexte) et applique son opération **`ingest`** sur le fichier que tu viens d'écrire.

Suis intégralement la procédure d'ingest du skill `llm-wiki` (section `## Core Operations → 1. Ingest`). En automated mode, tu enchaînes sans demander de validation à l'utilisateur (le skill llm-wiki prévoit ce mode au step ② : "Skip this in automated/cron contexts — proceed directly").

Si l'ingest échoue : ne fail pas tout. Le raw a déjà été commité. Notifie ⚠️ et signale clairement à l'utilisateur ce qui a bloqué.

### 10. Confirme l'enchaînement

Une fois la phase A et B réussies pour un audio, poste un message court (Discord ou stdout selon trigger) :

```
✅ Transcript intégré : `<filename>`
   📥 Capture : raw/transcripts/<filename> (sha256: <8 premiers>)
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

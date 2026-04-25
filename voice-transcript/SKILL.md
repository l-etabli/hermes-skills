---
name: voice-transcript
description: "Scanne un dossier Google Drive, transcrit via Groq Whisper tout audio nouveau (zips Craig, voice notes, exports Zoom, mp3/m4a/wav/flac/ogg), dépose le résultat dans raw/transcripts/ d'un wiki llm-wiki et enchaîne l'opération ingest du skill llm-wiki. Idempotent via sha256."
version: 1.2.0
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

### 2. Écris le script de scan dans un tmpdir

Tout le travail de capture (Phase A) tient dans un seul script Python lancé via `uv run`. Voici le template — adapte si besoin mais n'invente pas de variantes :

```python
# Écris ce contenu dans <tmpdir>/scan.py via write_file
"""
voice-transcript scan: list Drive folder, transcribe new audio via Groq,
write to $WIKI_PATH/raw/transcripts/, return JSON summary on stdout.
"""
import hashlib, io, json, os, pathlib, re, subprocess, sys, tempfile, time, zipfile, unicodedata
from datetime import datetime, timezone
import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account

WIKI_PATH = pathlib.Path(os.environ["WIKI_PATH"])
TRANSCRIPTS_DIR = WIKI_PATH / "raw" / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

CRAIG_RECORDING_ID = os.environ.get("CRAIG_RECORDING_ID")  # optional filter

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "audio"

def s_to_ts(s):
    s = int(s); h, r = divmod(s, 3600); m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

def already_transcribed(drive_id):
    for md in TRANSCRIPTS_DIR.glob("*.md"):
        if f"drive_id: {drive_id}" in md.read_text(encoding="utf-8", errors="replace")[:1500]:
            return md.name
    return None

def list_candidates(drive, folder_id):
    name_clauses = [
        "name contains '.flac.zip'", "mimeType contains 'audio/'",
        "name contains '.mp3'", "name contains '.m4a'",
        "name contains '.wav'", "name contains '.flac'", "name contains '.ogg'",
    ]
    q = f"'{folder_id}' in parents and trashed=false and ({' or '.join(name_clauses)})"
    if CRAIG_RECORDING_ID:
        q += f" and name contains 'craig_{CRAIG_RECORDING_ID}_'"
    return drive.files().list(
        q=q, orderBy="createdTime desc",
        fields="files(id,name,createdTime,size,mimeType,md5Checksum)",
        pageSize=50,
    ).execute().get("files", [])

def download_to(drive, file_id, dst: pathlib.Path):
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    dst.write_bytes(buf.getvalue())

def prepare_audio(drive, f, tmpdir: pathlib.Path):
    """Returns (audio_path, speakers, source) where source ∈ {'craig','manual'}."""
    name = f["name"]
    if name.endswith(".flac.zip"):
        zip_path = tmpdir / name
        download_to(drive, f["id"], zip_path)
        flac_paths = []
        with zipfile.ZipFile(zip_path) as zf:
            for zi in zf.infolist():
                if zi.filename.lower().endswith(".flac"):
                    p = tmpdir / pathlib.Path(zi.filename).name
                    p.write_bytes(zf.read(zi))
                    flac_paths.append(p)
        speakers = [p.stem.split("-", 1)[-1] for p in flac_paths]
        if len(flac_paths) == 1:
            return flac_paths[0], speakers, "craig"
        out = tmpdir / "mix.flac"
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for p in flac_paths:
            cmd += ["-i", str(p)]
        cmd += ["-filter_complex", f"amix=inputs={len(flac_paths)}:normalize=0",
                "-c:a", "flac", str(out)]
        subprocess.run(cmd, check=True)
        return out, speakers, "craig"
    out = tmpdir / name
    download_to(drive, f["id"], out)
    return out, [], "manual"

def transcribe_groq(audio_path: pathlib.Path):
    size = audio_path.stat().st_size
    if size > 25 * 1024 * 1024:
        raise RuntimeError(f"audio {size/1e6:.1f}MB > 25MB Groq limit; re-encode lower or split")
    with open(audio_path, "rb") as fh:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            files={"file": (audio_path.name, fh, "audio/flac")},
            data={
                "model": "whisper-large-v3-turbo",
                "response_format": "verbose_json",
                "language": "fr",
                "temperature": "0",
            },
            timeout=300,
        )
    r.raise_for_status()
    return r.json()

def parse_recorded_at(name, source, drive_created):
    if source == "craig":
        m = re.search(r"craig_[A-Za-z0-9]+_(\d{4})-(\d{1,2})-(\d{1,2})_(\d{1,2})-(\d{1,2})-(\d{1,2})", name)
        if m:
            y, mo, d, h, mi, s = map(int, m.groups())
            return datetime(y, mo, d, h, mi, s).strftime("%Y-%m-%d %H:%M")
    # fallback drive createdTime (RFC3339, e.g. 2026-04-25T08:14:32.000Z)
    try:
        return datetime.fromisoformat(drive_created.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M")

def write_transcript(f, audio_path, transcription, speakers, source, tmpdir):
    name = f["name"]
    recorded_at = parse_recorded_at(name, source, f.get("createdTime", ""))
    date = recorded_at.split(" ")[0]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    duration_s = int(transcription.get("duration", 0))
    dur_text = f"{duration_s // 60}m {duration_s % 60}s"
    lang = transcription.get("language", "?")

    # slug: source-aware
    if source == "craig":
        m = re.search(r"craig_([A-Za-z0-9]+)_", name)
        slug = f"craig-{m.group(1).lower()}-{slugify(date)}" if m else f"audio-{f['id'][:8]}"
    else:
        stem = pathlib.Path(name).stem
        slug = slugify(stem) or f"audio-{f['id'][:8]}"

    src_url = (f"https://craig.horse/rec/{re.search(r'craig_([A-Za-z0-9]+)_', name).group(1)}"
               if source == "craig" else
               f"drive://{os.environ['AUDIO_DRIVE_FOLDER_ID']}/{name}")

    speakers_line = ", ".join(speakers) if speakers else "???"
    single_speaker = speakers[0] if len(speakers) == 1 else None
    body_lines = []
    for seg in transcription.get("segments", []):
        ts = s_to_ts(seg["start"])
        sp = single_speaker or "???"
        body_lines.append(f"[{ts}] **{sp}**: {seg['text'].strip()}\n")
    body = "\n".join(body_lines).rstrip() + "\n"

    body_full = (
        f"# Transcription {slug}\n\n"
        f"- **Source audio** : `{name}`\n"
        f"- **Date** : {recorded_at}\n"
        f"- **Durée** : {dur_text}\n"
        f"- **Participants** : {speakers_line}\n"
        f"- **Transcription** : Groq Whisper Large v3 Turbo (langue détectée : `{lang}`)\n\n"
        f"{body}"
    )
    sha = hashlib.sha256(body_full.encode("utf-8")).hexdigest()
    fm = (
        f"---\n"
        f"source: {source}\n"
        f"drive_id: {f['id']}\n"
        f"drive_md5: {f.get('md5Checksum','')}\n"
        f"recorded_at: {recorded_at}\n"
        f"ingested: {today}\n"
        f"sha256: {sha}\n"
        f"---\n\n"
    )
    out_path = TRANSCRIPTS_DIR / f"{date}-{slug}.md"
    if out_path.exists():
        # idempotent shield (the loop already filters by drive_id, but in
        # case the user replaced a file with the same name we fork the slug)
        existing = out_path.read_text(encoding="utf-8", errors="replace")[:1500]
        if f"drive_id: {f['id']}" in existing:
            return None  # already there
        out_path = TRANSCRIPTS_DIR / f"{date}-{slug}-2.md"
    out_path.write_text(fm + body_full, encoding="utf-8")
    return out_path

def main():
    folder_id = os.environ["AUDIO_DRIVE_FOLDER_ID"]
    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    candidates = list_candidates(drive, folder_id)
    if not candidates:
        print(json.dumps({"new": [], "skipped": [], "reason": "drive folder is empty or SA has no access"}))
        return
    new_files, skipped = [], []
    with tempfile.TemporaryDirectory() as td:
        tmpdir = pathlib.Path(td)
        for f in candidates:
            existing = already_transcribed(f["id"])
            if existing:
                skipped.append({"name": f["name"], "as": existing})
                continue
            try:
                audio, speakers, source = prepare_audio(drive, f, tmpdir)
                tx = transcribe_groq(audio)
                out = write_transcript(f, audio, tx, speakers, source, tmpdir)
                if out:
                    new_files.append(str(out.relative_to(WIKI_PATH)))
            except Exception as exc:
                skipped.append({"name": f["name"], "error": str(exc)})
    print(json.dumps({"new": new_files, "skipped": skipped}))

if __name__ == "__main__":
    main()
```

### 3. Lance le script via `uv run`

```bash
TMPDIR=$(mktemp -d)
write_file "$TMPDIR/scan.py"   # avec le contenu ci-dessus
uv run --with requests --with google-auth --with google-api-python-client \
    "$TMPDIR/scan.py"
rm -rf "$TMPDIR"
```

La sortie est un JSON `{"new": ["raw/transcripts/...", ...], "skipped": [{"name": "...", "as": "..."}, ...]}`. Parse-le pour la suite.

Si `new` est vide et `skipped` ne contient que des entrées `"as"` (déjà transcrits) → tout est à jour, signale-le et stoppe ici (sans message bruyant si trigger = cron).

Si `skipped` contient des `"error"` → log les erreurs, continue avec les `new` malgré tout.

### 4. Commit + push des nouveaux raws

Pour chaque entrée de `new` :

```bash
cd "$WIKI_PATH"
git pull --rebase
git add "<chemin>"
git commit -m "raw: capture transcript $(basename '<chemin>' .md)"
git push
```

L'auth est gérée par le credential helper installé côté infra (lit `$GITHUB_TOKEN` à chaque op), aucune manipulation de token n'est nécessaire. Si le push échoue avec une erreur d'auth, attends 60 s (le sidecar refresh le token toutes les 45 min) et retry une fois. Au-delà, log et passe au candidat suivant — le raw est déjà committé localement, le push se rattrape au prochain run.

### 5. Enchaîne l'ingestion (Phase B)

Pour chaque entrée de `new`, active le skill `llm-wiki` (`skill_view("llm-wiki")` si pas en contexte) et applique son opération **`ingest`** sur le fichier.

Suis intégralement la procédure d'ingest du skill `llm-wiki` (section `## Core Operations → 1. Ingest`). En automated mode, tu enchaînes sans demander de validation à l'utilisateur (le skill llm-wiki prévoit ce mode au step ② : "Skip this in automated/cron contexts — proceed directly").

Si l'ingest échoue : ne fail pas tout. Le raw a déjà été commité. Notifie ⚠️ et signale clairement à l'utilisateur ce qui a bloqué.

### 6. Confirme l'enchaînement

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

---
name: craig-transcript
description: "Transcrit un enregistrement audio via Groq Whisper et dépose le résultat dans raw/transcripts/ d'un wiki llm-wiki, puis enchaîne l'opération ingest du skill llm-wiki. Deux sources: (A) un zip Craig auto-uploadé dans un dossier Google Drive (trigger: URL craig.horse), (B) un fichier audio déposé manuellement par l'utilisateur dans le même dossier."
version: 0.4.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, discord, transcript, raw-capture, llm-wiki, gdrive]
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
    prompt: "Chemin absolu vers le JSON du Service Account Google (ex. /opt/data/google-drive-sa.json). Le SA doit avoir le dossier Drive Craig partagé avec lui (rôle Viewer minimum)."
    required_for: full functionality
  - name: CRAIG_DRIVE_FOLDER_ID
    prompt: "ID du dossier Google Drive où Craig dépose les zips de recordings. Copie depuis l'URL Drive: drive.google.com/drive/folders/<ID>."
    required_for: full functionality
  - name: GROQ_API_KEY
    prompt: "Clé API Groq (https://console.groq.com/keys). Requise uniquement pour le mode Groq (transcription d'audio brut uploadé par l'utilisateur). Non requise si tu n'utilises que le mode Craig."
    required_for: Groq mode only
---

# Craig Transcript

Capture une transcription audio dans la couche **raw/** d'un wiki [llm-wiki](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/llm-wiki/SKILL.md), puis chaîne immédiatement l'opération `ingest` du skill `llm-wiki` pour promouvoir le transcript en pages wiki (entités, concepts, cross-références).

Rationale : un transcript capturé sans être ingéré, c'est de la connaissance dormante. La capture brute n'a de valeur que si elle alimente le réseau de pages wiki.

**Moteur de transcription : Groq** (`whisper-large-v3-turbo`, rapide, ~$0.04/h audio). La transcription Craig-internal (Faster Whisper Runpod) n'est PAS utilisée — observée comme peu fiable.

Le skill supporte deux sources d'audio, converties toutes les deux en texte via Groq :

- **Source A (Craig zip)** : un enregistrement Discord via Craig, uploadé automatiquement dans le dossier Drive sous forme de zip (`craig_<id>_*.flac.zip`). Le skill extrait le(s) `.flac` du zip, les mixe si plusieurs speakers, envoie à Groq.
- **Source B (audio direct)** : l'utilisateur dépose un fichier audio (`mp3`, `m4a`, `wav`, `flac`, `ogg`) dans le même dossier Drive (ex. export Zoom, voice note). Le skill l'envoie à Groq tel quel.

## When to Use

### Mode Craig — URL craig.horse

Active-toi quand un message dans un canal `*-scribe-inbox` (ou un message direct de l'utilisateur) contient une URL au format :

```
https://craig.horse/rec/<recording_id>?key=<key>
```

Régex : `https://craig\.horse/rec/([A-Za-z0-9]+)\?key=([A-Za-z0-9_-]+)`

Exemple : `https://craig.horse/rec/nhWUc76htgaR?key=mohN17`

Ignore les paramètres supplémentaires (`&delete=...`). Si plusieurs URLs sont collées, traite-les séquentiellement.

### Mode Groq — demande de transcription d'un audio Drive

Active-toi quand l'utilisateur demande explicitement de transcrire un fichier audio, en référence à un fichier du dossier Drive. Exemples d'énoncés déclencheurs :

- « transcris le dernier audio dans Drive »
- « il y a un fichier `meeting-X.m4a` dans le dossier Drive, ingère-le »
- « transcris `<nom-de-fichier>.mp3` »

L'utilisateur doit avoir déposé le fichier dans le même dossier Drive (`CRAIG_DRIVE_FOLDER_ID`) avant. Le skill va scanner ce dossier, identifier le fichier à transcrire (par nom si fourni, sinon le plus récent non-zip), et passer en mode Groq (voir section 2b).

## Scope (deux phases distinctes, exécutées en chaîne)

**Phase A — Capture (responsabilité directe de ce skill)** :
- Attend que Craig dépose le zip dans le dossier Drive (Craig est configuré côté Discord pour auto-upload chaque recording)
- Localise la source audio dans Drive (zip Craig via l'URL craig.horse, ou fichier audio déposé par l'utilisateur), télécharge-la via l'API Drive, envoie à Groq Whisper pour transcription
- Parse + reformate en Markdown lisible avec speakers
- Dépose dans `$WIKI_PATH/raw/transcripts/<date>-<slug>.md` avec **frontmatter raw** (sha256, source_url, ingested)
- Dédup via sha256 (skip toute la suite si déjà présent à l'identique)
- `git pull --rebase` + `commit` + `push` du raw

**Phase B — Promotion en pages wiki (déléguée au skill `llm-wiki`)** :
- Activé immédiatement après la phase A si un nouveau raw a été écrit
- Ce skill **n'implémente pas** l'extraction d'entités/concepts ni les writes dans `entities/` `concepts/` `index.md` `log.md` — c'est tout le boulot du skill `llm-wiki` opération `ingest`
- Ce skill se contente de l'**activer** sur le fichier raw fraîchement déposé

→ L'utilisateur n'a qu'une seule action à faire (coller l'URL). Les deux phases s'enchaînent.

## Procedure

### 1. Acknowledge et oriente-toi

- Réagis ⏳ au message d'origine
- Lis `$WIKI_PATH/SCHEMA.md` pour vérifier que la convention raw n'a pas été customisée par l'utilisateur (typiquement : champs frontmatter raw, sous-dossiers de `raw/`)
- Si `SCHEMA.md` n'existe pas ou que `$WIKI_PATH` n'est pas un wiki llm-wiki valide, abandonne et signale-le

### 2. Récupère l'audio depuis Drive

**Auth Drive** : les libs `google-auth` + `google-api-python-client` lisent le Service Account JSON via `GOOGLE_APPLICATION_CREDENTIALS`. Installe-les si absentes : `pip install --quiet google-auth google-api-python-client`.

```python
from googleapiclient.discovery import build
from google.oauth2 import service_account
import os

creds = service_account.Credentials.from_service_account_file(
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
    scopes=["https://www.googleapis.com/auth/drive.readonly"],
)
drive = build("drive", "v3", credentials=creds, cache_discovery=False)
folder_id = os.environ["CRAIG_DRIVE_FOLDER_ID"]
```

**Source A — trigger URL Craig** : cherche le zip `craig_<recording_id>_*.flac.zip` dans Drive. Poll toutes les 30 s, timeout 30 min (Craig upload parfois avec délai). Quand trouvé, extrait **tous** les `.flac` du zip en mémoire.

```python
q = f"'{folder_id}' in parents and name contains 'craig_{recording_id}_' and trashed = false"
# ... files.list ... wait 30s until non-empty ... 30min cap

import io, zipfile
from googleapiclient.http import MediaIoBaseDownload

zip_buf = io.BytesIO()
downloader = MediaIoBaseDownload(zip_buf, drive.files().get_media(fileId=files[0]["id"]))
done = False
while not done:
    _, done = downloader.next_chunk()
zip_buf.seek(0)

flac_tracks = {}  # name -> bytes
with zipfile.ZipFile(zip_buf) as zf:
    for zi in zf.infolist():
        if zi.filename.lower().endswith(".flac"):
            flac_tracks[zi.filename] = zf.read(zi)
```

**Source B — trigger demande utilisateur** : cherche le fichier nommé par l'utilisateur, ou le dernier audio non-zip du dossier.

```python
if filename_hint:
    q = f"'{folder_id}' in parents and name = '{filename_hint}' and trashed = false"
else:
    q = (
        f"'{folder_id}' in parents and trashed = false and "
        "(mimeType contains 'audio/' or name contains '.mp3' or "
        " name contains '.m4a' or name contains '.wav' or "
        " name contains '.flac' or name contains '.ogg')"
    )
resp = drive.files().list(
    q=q, orderBy="createdTime desc",
    fields="files(id,name,createdTime,size,mimeType)", pageSize=5,
).execute()
audio_file = resp["files"][0]
# download audio_file to audio_buf (same MediaIoBaseDownload pattern)
```

Si zéro fichier trouvé → ❌, signale « pas d'audio dans le dossier Drive ou pas partagé avec le SA » et abandonne.

### 3. Prépare un flux audio unique pour Groq

**Source B** : un seul fichier, passe-le tel quel.

**Source A** : le zip Craig contient 1 à N `.flac` (un par speaker, `1-<nom>.flac`, `2-<nom>.flac`, …).

- **1 track** : envoie-la telle quelle.
- **N tracks** : mixe-les avec `ffmpeg` (installé dans l'image Hermes, sinon `apt install -y ffmpeg` ou `pip install pydub` + fallback).

```python
import subprocess, tempfile, pathlib

tmpdir = pathlib.Path(tempfile.mkdtemp())
input_paths = []
for name, data in flac_tracks.items():
    p = tmpdir / pathlib.Path(name).name
    p.write_bytes(data)
    input_paths.append(p)

if len(input_paths) == 1:
    mixed_path = input_paths[0]
else:
    mixed_path = tmpdir / "mix.flac"
    cmd = ["ffmpeg", "-y"]
    for p in input_paths:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex", f"amix=inputs={len(input_paths)}:normalize=0",
        "-c:a", "flac", str(mixed_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)

audio_bytes = mixed_path.read_bytes()
speakers = [pathlib.Path(p.name).stem.split("-", 1)[-1] for p in input_paths]  # ["jeromebu", "greg"]
```

Garde la liste `speakers` pour l'annoter dans les metadata du transcript (même si Groq ne sait pas qui parle, on sait _qui est dans la pièce_).

### 4. Transcris via Groq

**Endpoint** (compat OpenAI Whisper) : `POST https://api.groq.com/openai/v1/audio/transcriptions`.

**Limite** : 25 MB par requête (Groq sync). Pour `.flac` haute qualité, compte ~1h d'audio ≈ 20-25 MB. Au-delà : abandonne proprement (pas de chunking auto pour l'instant), signale-le à l'utilisateur.

```python
import requests, os

if len(audio_bytes) > 25 * 1024 * 1024:
    raise RuntimeError(f"Audio {len(audio_bytes)/1e6:.1f} MB > 25 MB Groq sync limit")

resp = requests.post(
    "https://api.groq.com/openai/v1/audio/transcriptions",
    headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
    files={"file": (mixed_path.name, audio_bytes, "audio/flac")},
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
# transcription["text"]     -> full text
# transcription["segments"] -> [{"start": float, "end": float, "text": str}, ...]
# transcription["duration"] -> float seconds
# transcription["language"] -> detected lang
```

**Pas de diarisation** : Groq Whisper sort un seul flux de texte. La ligne de métadonnées du transcript liste la `speakers` extraite du zip Craig (ou fournie par l'utilisateur pour source B), mais chaque tour de parole dans le body est prefixé d'un placeholder `???` sauf si un seul speaker est connu — auquel cas c'est son nom.

### 5. Collecte les métadonnées

- **Durée totale** : `transcription["duration"]` (secondes → formate en `Xm Ys`)
- **Langue détectée** : `transcription["language"]`
- **Speakers dans la pièce** (Source A uniquement) : liste des noms extraits des filenames `.flac` (`1-jeromebu.flac` → `jeromebu`)
- **Speaker unique** (Source B) : utilise le nom du fichier comme fallback, ou demande à l'utilisateur si ambigu
- **Date d'enregistrement** :
  - Source A : parse depuis le nom du zip (pattern `craig_<id>_<YYYY-M-D>_<HH-MM-SS>.flac.zip`)
  - Source B : `createdTime` du fichier Drive (converti en local time)
- **Nom pour le slug** :
  - Source A : essaye `info.txt` dans le zip si présent (cherche une ligne avec le nom du salon), sinon fallback
  - Source B : basé sur le nom du fichier (stripping de l'extension)

### 6. Compose le contenu Markdown

Le contenu (qui sera sauvegardé dans `raw/transcripts/`) :

```markdown
---
source_url: <voir tableau ci-dessous>
ingested: <YYYY-MM-DD>
sha256: <hex digest du body ci-dessous, sans le frontmatter>
---

# Transcription <titre humain>

- **Source audio** : `<zip craig ou nom du fichier Drive>`
- **Date** : <YYYY-MM-DD HH:MM>
- **Durée** : <Xm Ys>
- **Participants** : <liste extraite du zip / nom fichier / défaut ???>
- **Transcription** : Groq Whisper Large v3 Turbo (langue détectée : `<lang>`)

[00:03] **???**: premier segment de parole...

[00:18] **???**: segment suivant...
```

**Règles par source** :

| Champ | Source A (Craig) | Source B (audio direct) |
|---|---|---|
| `source_url` (frontmatter) | `https://craig.horse/rec/<id>` | `drive://<folder_id>/<filename>` |
| **Source audio** (body) | `craig_<id>_<date>.flac.zip` | `<filename>` |
| **Participants** (body) | extraits des filenames `.flac` du zip (ex. `jeromebu, greg`) | nom dérivé du fichier ou demandé à l'utilisateur |
| Préfixe speaker body | `???` (si >1 speaker) ou le nom (si 1 seul) | nom utilisateur si connu, sinon `???` |

**Pattern de code pour le body** :

```python
lines = []
single_speaker = speakers[0] if len(speakers) == 1 else None
for seg in transcription["segments"]:
    ts = _seconds_to_mmss(seg["start"])  # "00:03" ou "01:02:03"
    speaker = single_speaker or "???"
    text = seg["text"].strip()
    lines.append(f"[{ts}] **{speaker}**: {text}\n")
body = "\n".join(lines)
```

**Conventions importantes** (cf. SKILL.md llm-wiki) :
- Le frontmatter raw est **minimaliste** : juste `source_url`, `ingested`, `sha256`. Pas de `title`, `tags`, `type` — ces champs sont pour les pages wiki dans `entities/`, `concepts/`, etc.
- Le `sha256` est calculé sur le body (tout ce qui est après le `---` de fermeture du frontmatter), pas sur le frontmatter lui-même
- Une ligne vide entre chaque tour de parole
- Pas de `[[wikilinks]]` à ce stade — `raw/` est immuable, l'enrichissement vit dans des pages wiki séparées que créera l'opération `ingest`

### 7. Dédup et chemin final

**Chemin** : `$WIKI_PATH/raw/transcripts/<YYYY-MM-DD>-<slug>.md`

- `<YYYY-MM-DD>` = date du recording (voir section 5 pour la dérivation par source)
- `<slug>` (par ordre de préférence) :
  - Source A : `info.txt` du zip contient un nom de salon Discord lisible → slugifier (minuscules, accents retirés, espaces → tirets)
  - Source B : nom du fichier Drive sans extension, slugifié
  - sinon demande à l'utilisateur un nom court avant d'écrire
  - dernier fallback : `craig-<recording_id>` (Source A) ou `audio-<drive_file_id_8>` (Source B)

**Avant d'écrire** :

1. `git -C "$WIKI_PATH" pull --rebase`
2. Si le fichier existe déjà :
   - Compute le sha256 du body actuel
   - Compare avec le sha256 du frontmatter du fichier existant
   - **Identique** → skip toute écriture, signale "transcript déjà présent et inchangé", ne re-déclenche RIEN
   - **Différent** (drift) → demande à l'utilisateur : écraser ? créer une nouvelle entrée avec slug suffixé (`-2`, `-bis`) ? abandonner ?
3. Si le fichier n'existe pas → écris-le

### 8. Commit + push

```bash
cd "$WIKI_PATH"
git add "raw/transcripts/<filename>"
git commit -m "raw: capture transcript <date> <slug>"
git push
```

Le `$GITHUB_TOKEN` est rafraîchi par le sidecar GitHub App. Si le push échoue avec une erreur d'auth, attends 60s et retry une fois.

### 9. Confirme la phase A

Réagis 📥 au message d'origine pour signaler la fin de la capture (mais pas encore de l'ingestion). Pas besoin de poster un message texte à ce stade — la confirmation finale arrivera après la phase B.

### 10. Enchaîne la phase B — opération `ingest` du skill `llm-wiki`

Active le skill `llm-wiki` (via `skill_view("llm-wiki")` si tu ne l'as pas en contexte) et applique son opération **`ingest`** sur le fichier que tu viens d'écrire :

```
$WIKI_PATH/raw/transcripts/<filename>
```

Suis intégralement la procédure d'ingest définie dans le SKILL.md llm-wiki (section `## Core Operations → 1. Ingest`). En résumé, elle va :

- Lire le raw
- Identifier entités et concepts mentionnés (personnes, projets, décisions, sujets techniques)
- Vérifier ce qui existe déjà dans `index.md` (orientation préalable obligatoire : `SCHEMA.md` → `index.md` → derniers `log.md`)
- Créer ou updater les pages dans `entities/`, `concepts/`, `comparisons/` selon les **Page Thresholds** (≥ 2 mentions OR central à 1 source)
- Ajouter les nouvelles pages à `index.md`
- Cross-référencer (≥ 2 wikilinks par page)
- Appender à `log.md` : `## [YYYY-MM-DD] ingest | <titre>` + liste des fichiers touchés
- Commit + push (un commit séparé pour la phase B, distinct du commit raw)

**Mode automated** : tu enchaînes sans demander de validation à l'utilisateur (le skill llm-wiki prévoit explicitement ce mode au step ② de l'ingest : "Skip this in automated/cron contexts — proceed directly").

**Si l'ingest échoue** (erreur, conflit git, page existante en contradiction, etc.) : ne fail pas tout. Le raw a déjà été commité (phase A réussie). Réagis ⚠️ et signale clairement à l'utilisateur ce qui a bloqué — il peut relancer manuellement l'ingest plus tard avec les bonnes corrections.

### 11. Confirme l'enchaînement complet

Une fois les deux phases terminées, réagis ✅ au message d'origine et poste dans le canal :

```
✅ Transcript intégré
   📥 Capture : `raw/transcripts/<filename>` (sha256: <8 premiers>)
   🧠 Ingest : <N> page(s) wiki créée(s)/updatée(s)
        - [[page-a]], [[page-b]], [[page-c]]
   📝 Log : `## [YYYY-MM-DD] ingest | <titre>`
```

Si la phase B a été skipped (raw déjà existant et inchangé) :
```
ℹ️ Transcript déjà présent et inchangé (sha256 match) — rien à faire.
```

## Pitfalls

- **Ne touche jamais à `raw/` après écriture** — sources immuables (cf. SKILL.md llm-wiki, ligne 478). Les corrections vivent dans des pages wiki.
- **Ne touche pas à `index.md` ni à `log.md`** — c'est le job de l'opération `ingest` du skill `llm-wiki`. Si tu les modifies depuis ce skill, tu duplique le travail et risques des conflits.
- **Frontmatter raw ≠ frontmatter wiki** — n'utilise PAS `title`, `tags`, `type`, `sources` ici. Ces champs sont réservés aux pages dans `entities/`, `concepts/`, etc. Le frontmatter raw est volontairement minimal.
- **Le `sha256` se calcule sur le body uniquement** (après le `---` de fermeture du frontmatter). Sinon le hash change à chaque ingestion à cause du `ingested:` qui bouge, et la dédup ne marche jamais.
- **Idempotence forte** : si le sha256 matche l'existant, **rien n'est écrit, rien n'est commité, rien n'est pushé**. Garde un historique git propre.
- **Encodage UTF-8 strict** : les accents français doivent passer (parsing + écriture).
- **Pas de diarisation** : Groq Whisper ne sépare pas les speakers. Tout le body est préfixé `???` sauf si un seul speaker est connu (fichier solo / filename explicite). Ne bluffe pas l'attribution.
- **Speakers de la pièce vs speakers diarisés** : la ligne `Participants` du body liste qui était dans la pièce (issu des filenames `.flac` du zip Craig, ou fourni par l'utilisateur), mais ce n'est PAS une attribution des tours de parole — juste un contexte.
- **Audio sans paroles** (silence, noise-only) : Groq retourne `segments: []`. Préviens l'utilisateur, n'écris rien dans `raw/`.
- **Audio > 25 MB** : abandonne avec erreur claire. Suggère à l'utilisateur de re-encoder en qualité plus basse (`ffmpeg -i in.flac -ab 96k -ar 16000 out.mp3`) ou de découper.
- **Zip Craig jamais arrivé** : après 30 min de poll Drive, abandonne, ❌, conserve l'URL. Craig a peut-être échoué à uploader ou l'utilisateur a arrêté le recording sans déclencher l'upload côté Discord.
- **SA sans accès au dossier** : `files.list` renverra une liste vide (pas une erreur de permissions). Si tu ne trouves jamais aucun fichier, vérifie d'abord que le dossier `CRAIG_DRIVE_FOLDER_ID` est bien partagé avec l'email du SA avant de boucler 30 min.
- **Rate limit Groq** : 429 → back off 30s, retry 3 fois max, puis abandonne avec le message d'erreur.
- **Réseau qui drop** (Drive ou Groq) : retry 3 fois avec backoff exponentiel (30s → 60s → 120s).

## Verification

Après exécution complète (phases A + B), vérifie :

**Phase A** :
1. `$WIKI_PATH/raw/transcripts/<filename>.md` existe avec le bon frontmatter (3 champs : `source_url`, `ingested`, `sha256`)
2. Le sha256 dans le frontmatter correspond bien au hash du body
3. `git log` dans `$WIKI_PATH` montre le commit "raw: capture transcript …"

**Phase B** :
4. Au moins une page dans `entities/` ou `concepts/` mentionne le fichier raw dans son frontmatter `sources:` (sauf si l'ingest n'a rien jugé promotable selon les seuils)
5. `log.md` contient une entrée `## [YYYY-MM-DD] ingest | <titre>` qui liste les pages touchées
6. `git log` montre un commit séparé pour l'ingest (ex: "wiki: ingest transcript …")
7. `git push` a réussi pour les deux commits

**Final** :
8. Tu as réagi ✅ au message d'origine
9. Le message de confirmation final est posté avec la liste des pages wiki créées/updatées

Si un point échoue, signale-le clairement plutôt que de prétendre que ça a marché.

## Notes

- **Auth Drive** : le Service Account est authentifié via le JSON pointé par `GOOGLE_APPLICATION_CREDENTIALS`. L'accès est scoppé au(x) dossier(s) partagé(s) explicitement avec l'email du SA — pas d'accès aux autres dossiers Drive de l'utilisateur.
- **Pourquoi Groq et pas la transcription Craig interne** : Craig utilise Faster Whisper sur Runpod avec une qualité observée peu fiable (confusions de speakers, erreurs lexicales). Groq Whisper Large v3 Turbo est plus précis, ~10× plus rapide que realtime, et ~$0.04/h audio (abo Groq free tier généreux pour un usage perso).
- **Coût** :
  - Côté Craig : $8/mois Patreon Tier 3 (inclut l'enregistrement, le stockage, l'upload Drive auto — transcription interne ignorée par ce skill).
  - Côté Groq : quelques centimes par heure audio via leur API.
- **Latence de bout en bout** :
  - Source A (Craig zip) : upload Drive Craig (~1-5 min post-recording) + Groq (~0.1× realtime) = ~1-6 min pour 1h d'audio. BIEN plus rapide que la transcription Craig interne qui est ~1:1.
  - Source B (audio direct) : ~0.1× realtime Groq = ~6 min pour 1h.
- **Pourquoi pas d'auto-ingest** : le skill llm-wiki impose des seuils stricts (≥ 2 mentions ou central à 1 source pour créer une page). Cette décision relève de l'agent au moment de l'ingest, avec contexte sur l'index existant — pas du skill de capture qui n'a pas cette vue d'ensemble. Garder les deux séparés laisse aussi à l'utilisateur le choix de promouvoir ou non un transcript donné.

## Cron (non requis)

L'enchaînement A→B est synchrone dans ce skill — pas besoin de cron de retry pour l'ingestion routinière. Un cron pourrait servir uniquement comme **filet de sécurité** : scanner périodiquement `raw/transcripts/` pour détecter les fichiers non référencés dans aucun `sources:` de page wiki ni dans `log.md`, et relancer l'ingest sur eux. Utile seulement si la phase B échoue souvent et qu'on doit rattraper les échecs en différé. À considérer si ça arrive.

## Comment savoir si un raw est ingéré

Le frontmatter raw ne contient **pas** de marqueur d'ingestion (la convention llm-wiki garde `raw/` strictement immuable). Pour vérifier l'état d'un raw donné :

```bash
# Cherche la mention dans les pages wiki (frontmatter sources:)
grep -rl "raw/transcripts/<filename>" "$WIKI_PATH/entities" "$WIKI_PATH/concepts" "$WIKI_PATH/comparisons" "$WIKI_PATH/queries"

# OU cherche dans log.md
grep "<filename>" "$WIKI_PATH/log.md"
```

Si l'un des deux remonte du résultat, le raw est ingéré.

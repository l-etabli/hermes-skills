---
name: craig-listener
description: "Active-toi sur tout message du bot Craig dans #craig-events de cette instance. Extrait le Recording ID, écrit un pending state si le recording est en cours (craig-watch prend le relais en cron), ou déclenche directement la transcription si le recording est déjà terminé. Pas de filtrage par channel : Discord garantit l'isolation entre instances."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, craig, discord, event-driven]
    category: voice
    related_skills: [craig-transcript-record, craig-watch, llm-wiki]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki. Utilisé pour stocker l'état pending dans .craig-pending/ et hérité par craig-transcript-record."
    required_for: full functionality
  - name: DISCORD_BOT_TOKEN
    prompt: "Token du bot Discord de cette instance Hermes. Utilisé pour la self-discovery du message_id via l'API REST (le LLM ne sait pas extraire les IDs de son contexte de façon fiable)."
    required_for: full functionality
  - name: CRAIG_EVENTS_CHANNEL_ID
    prompt: "ID du canal Discord #craig-events de cette instance. Utilisé par la self-discovery pour trouver le message Craig correspondant à un Recording ID donné."
    required_for: full functionality
  - name: GITHUB_TOKEN
    prompt: "Token GitHub pour le push (refresh via la GitHub App). Hérité par craig-transcript-record."
    required_for: full functionality
  - name: GOOGLE_APPLICATION_CREDENTIALS
    prompt: "Chemin du JSON Service Account Drive. Hérité par craig-transcript-record."
    required_for: full functionality
  - name: AUDIO_DRIVE_FOLDER_ID
    prompt: "ID du dossier Drive d'auto-upload Craig. Hérité par craig-transcript-record."
    required_for: full functionality
  - name: GROQ_API_KEY
    prompt: "Clé API Groq. Hérité par craig-transcript-record."
    required_for: full functionality
---

# Craig Listener

Pont **event-driven** entre Craig (bot Discord d'enregistrement vocal) et le pipeline de transcription. Trigger sur le message initial de Craig, écrit un état pending pour que `craig-watch` prenne le relais en cron.

## Architecture Discord (rappel)

```
[Catégorie Perso]      role @perso       → Hermes-perso
  └─ #craig-events     ← Craig autorecord post depuis voice-channels Perso
[Catégorie Piloti]     role @piloti      → Hermes-piloti
  └─ #craig-events     ← Craig autorecord post depuis voice-channels Piloti
[Catégorie Telluris]   role @telluris    → Hermes-telluris
  └─ #craig-events     ← Craig autorecord post depuis voice-channels Telluris
```

Chaque Hermes a uniquement le rôle de SA catégorie → ne voit que SON `#craig-events`. **Tout le routing est fait par Discord.** Ce skill ne prend AUCUNE décision de filtrage.

## Cycle de vie d'un message Craig

Craig pose **un seul** message dans `#craig-events`, qu'il **édite** (même `message_id`) au cours du recording :

1. **Initial** (au start) :
   ```
   🔴 Recording...
   Recording ID: GDy7gHgSqJC5
   Channel: ⁠salon de test
   Started: 15:06:15 (il y a 4 secondes)
   Activity
   00:00:00: @Jérôme joined the recording.
   ```

2. **Final** (à la fin, MÊME message édité) :
   ```
   Recording ended.
   Recording ID: GDy7gHgSqJC5
   Channel: ⁠salon de test
   Started: 15:06:15 (il y a une minute)
   Activity
   00:00:00: @Jérôme joined the recording.
   00:00:10: Autorecord stopped due to lack of users.
   ```

Hermes upstream **ne supporte pas `on_message_edit`** — donc le listener ne voit que la version initiale. Le bridge vers la version finale est `craig-watch` (cron 5 min) qui refetch le message via Discord API.

## When to Use

Active-toi **immédiatement** dès que tu reçois un message du bot Craig dans `#craig-events` qui contient `Recording ID:`. Cas typique : message initial avec `🔴 Recording...`.

**Ne pas activer** si :
- Le message ne contient pas `Recording ID:` (autre msg Craig, msg humain).
- Tu es déjà en train de traiter un autre Recording ID dans ce run (séquentiel).

## Procedure

### 1. Acknowledge

Réagis ⏳ au message Craig pour signaler que tu as bien capté.

### 2. Lance `listener.py`

Le script fait l'extraction regex + l'écriture pending. **Ne fais PAS l'extraction toi-même** — la logique critique vit dans le script (le LLM ignore parfois les instructions explicites du SKILL.md).

Tu n'as **PAS** besoin de connaître ni de passer le `channel_id` ou le `message_id` — le script les découvre lui-même via l'API Discord (`GET /channels/$CRAIG_EVENTS_CHANNEL_ID/messages?limit=25` puis match sur `Recording ID: <id>` dans le body). Passe juste le body Craig via `--message-file`.

```python
# 1) Écris le body Craig dans un tmpfile (PAS via shell — Craig embed des
#    zero-width chars U+200B que le scanner sécurité Hermes flag si on les
#    met sur la cmdline `sh -c` ou `printf %q`).
from pathlib import Path
Path("/tmp/craig-msg.txt").write_text(craig_message_body, encoding="utf-8")

# 2) Invoke listener.py avec --message-file uniquement.
import subprocess
r = subprocess.run([
    "uv", "run",
    "--with", "google-auth",
    "--with", "google-api-python-client",
    "--with", "requests",
    "/opt/data/skills-shared/craig-listener/listener.py",
    "--message-file", "/tmp/craig-msg.txt",
], capture_output=True, text=True)
print(r.stdout)
```

**Ne PAS utiliser `printf '%s' "<body>" | ...` ni `sh -c`** : le scanner sécurité bloque (zero-width chars dans le body Craig).

`--channel-id` et `--message-id` existent comme escape hatch pour des runs manuels (debug, replay), mais en mode normal **ne les passe pas** — le LLM tend à les inventer, et le script discovery est plus fiable.

Le script :
1. Extrait `Recording ID: <id>` via regex.
2. Si `"Recording ended."` est déjà dans le body (rare : Hermes a vu le panel après-coup) → invoque `craig-transcript-record/scan.py` direct.
3. Sinon → écrit `$WIKI_PATH/.craig-pending/<craig_id>.json` avec `{craig_id, channel_id, message_id, first_seen_at}`. Le cron `craig-watch` prendra le relais.

### 3. Lis le JSON et réagis

```json
{"status": "pending",        "craig_id": "...", "state_path": ".craig-pending/<id>.json"}
{"status": "already-pending", "craig_id": "...", "state_path": ".craig-pending/<id>.json"}
{"status": "processed",      "craig_id": "...", "path": "raw/transcripts/...", "drive_id": "...", "name": "..."}
{"status": "skipped",        "craig_id": "...", "reason": "already-transcribed", "as": "..."}
{"status": "error",          "craig_id": "...", "reason": "...", "detail": "..."}
{"status": "error",          "reason": "no-recording-id|format-mismatch|empty-message|missing-discord-ids|bad-discord-ids", "detail": "..."}
```

- **`pending`** → silence côté Discord (la réaction ⏳ posée au step 1 reste). craig-watch s'occupera de la suite.
- **`already-pending`** → l'event Discord a été reçu deux fois (rare, mais possible : reconnect bot, replay). Ne pose PAS une nouvelle réaction ⏳, ne renotifie pas. Le pending d'origine est toujours en vol.
- **`processed`** → la sortie contient un champ `progress[]` avec les phases qu'a franchies `scan.py`. Suis la **UX live** ci-dessous (path direct : pas de pending, donc l'utilisateur découvre la chaîne en un seul passage). Puis suis la procédure `craig-transcript-record` à partir du step 3 (commit + push) puis step 4 (ingest llm-wiki). Notification ✅.
- **`skipped` / `already-transcribed`** → réagis ✅ pour confirmer qu'on est au courant.
- **`error` / `no-recording-id`** → ce n'était pas un message Craig de recording. Retire la réaction ⏳, ignore silencieusement.
- **`error` / `format-mismatch`** → ⚠️ **bruyant** : le message ressemble à un panel Craig (présence de `🔴 Recording`, `Voice Region:`, etc.) mais la regex `Recording ID:` a échoué. Très probablement le format Craig a changé. Notifie l'utilisateur avec le snippet, pour qu'on puisse mettre à jour la regex. Ne plus jamais ignorer silencieusement ce cas — sinon on arrête de transcrire sans s'en rendre compte.
- **`error` / `missing-discord-ids` / `bad-discord-ids`** → escape-hatch CLI mal utilisé. En mode normal n'utilise pas ces args, le script self-discovere.
- **`error` / `discord-discovery-failed`** → l'API Discord n'a pas pu retrouver le msg Craig. Cause classique : permission `Read Message History` manquante sur `#craig-events` (status 403 dans `detail`). Notifie ⚠️ avec le `detail` brut. **NE PAS** essayer de contourner en écrivant un pending toi-même, **NE PAS** inventer un craig_id, **NE PAS** bypasser le script — le seul vrai fix est côté Discord (config perm).
- **`error` / autres** → notifie ⚠️ avec `reason` + `detail`. Idem : aucune réécriture manuelle de pending JSON, aucune invention d'ID. Si le script erreur, l'état n'est PAS écrit, point.

## ⛔ Anti-pattern : ne JAMAIS bypass le script

Si le script retourne `status: "error"` :
- ❌ NE PAS écrire un fichier `.craig-pending/<id>.json` toi-même (write_file, Path.write_text...).
- ❌ NE PAS prétendre dans Discord que « j'ai forcé l'enregistrement de l'état » ou « j'ai mis en file d'attente » — c'est mensonger.
- ❌ NE PAS inventer un craig_id si l'extraction a échoué.
- ✅ Surface le `reason` + `detail` exact dans Discord avec ⚠️, point.

L'utilisateur préfère savoir que rien n'est fait que croire qu'une chose est en cours alors qu'elle ne l'est pas. Un état corrompu écrit par toi est PIRE qu'une erreur affichée — il bloque silencieusement la chaîne pendant des heures.

## UX live (path `processed` direct, recording déjà terminé)

Quand l'event arrive APRÈS la fin du recording (Hermes a vu le panel en mode `Recording ended.` d'emblée), le listener invoque `scan.py` directement et la chaîne complète (Drive poll → Groq → write) tient en un seul run LLM. La sortie agrège un champ `progress[]` :

```json
[
  {"status": "progress", "phase": "drive-poll-start"},
  {"status": "progress", "phase": "zip-found", "name": "...", "size_bytes": 12345678},
  {"status": "progress", "phase": "groq-start"},
  {"status": "progress", "phase": "writing-raw"}
]
```

Comme `scan.py` a tourné synchronement, ces phases sont déjà toutes passées au moment où tu lis le résultat. Tu n'as donc **pas** à éditer un message en live (le recording étant déjà terminé, le user n'attend pas en temps réel). **Une seule notif finale** :

- `processed` → poste dans le canal d'origine du panel Craig (ou `DISCORD_HOME_CHANNEL`) :
  ```
  ✅ Recording <craig_id> transcrit (chaîne directe, recording déjà terminé) :
  📄 <filename> — N pages wiki updatées : [[a]], [[b]]
  ```
  Le `progress[]` n'est utile ici que pour le debug — pas besoin de l'afficher au user, mais inclus-le si tu détectes un timing anormal (`zip-found` avec une grosse taille = recording long).

- `error` avec `progress[]` partiel → mentionne la dernière phase atteinte dans le ⚠️ (ex. « bloqué après `zip-found`, Groq a échoué : … »). C'est diagnostiqué d'un coup d'œil.

## Pitfalls

- **Ne pas pré-extraire le Recording ID toi-même** avant d'appeler le script. La regex vit dans `listener.py` — c'est l'ADN de ce skill.
- **Ne pas filtrer par channel ID dans la prose ou en code.** Le routing Discord (permissions de catégorie) garantit déjà l'isolation.
- **Ne PAS poll Drive depuis le LLM** en attendant la fin du recording. Le rôle du listener est de capter l'événement et déléguer à craig-watch via le pending state. Bloquer le LLM pendant des heures = anti-pattern.
- **Pas de parallélisme** sur plusieurs Recording ID dans le même run.
- **Pas de `git remote set-url`, pas de `export GITHUB_TOKEN`** — credential helper côté infra.

## Verification

1. Si `pending` : `$WIKI_PATH/.craig-pending/<craig_id>.json` existe avec les 4 champs.
2. Si `processed` : voir checklist du skill `craig-transcript-record`.
3. Le message Craig original a bien une réaction (⏳ tant qu'en attente, ✅ ou ⚠️ après).

## Notes

- **Pourquoi un script et pas du LLM** : extraction regex + écriture state = trivial, le coût LLM est inutile, et le LLM rate parfois des choses « évidentes ». Logique critique = code, pas prose.
- **Cohabitation Hermes / Craig** : Hermes a un filtre `DISCORD_ALLOW_BOTS` qui par défaut ignore les messages de bots. Pour que ce listener s'active, il faut soit lever le filtre globalement (les permissions Discord garantissent l'isolation) soit whitelister `#craig-events`. Voir hermes-infra côté config.
- **`.craig-pending/`** doit être gitignoré dans le wiki (state runtime, pas knowledge).

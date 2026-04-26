---
name: craig-watch
description: "Cron 5 min : refetch les messages Craig pending (écrits par craig-listener), détecte 'Recording ended.' via l'API Discord, déclenche craig-transcript-record/scan.py pour télécharger + transcrire le zip Drive, puis enchaîne llm-wiki ingest. Expire les pending de plus de 24h. Pas d'output → silence côté Discord."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, craig, cron, discord]
    category: voice
    related_skills: [craig-listener, craig-transcript-record, llm-wiki]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki. Lit .craig-pending/ et hérité par craig-transcript-record."
    required_for: full functionality
  - name: DISCORD_BOT_TOKEN
    prompt: "Token du bot Discord de cette instance Hermes. Utilisé pour refetch les messages Craig via l'API REST (GET /channels/.../messages/...)."
    required_for: full functionality
  - name: GITHUB_TOKEN
    prompt: "Token GitHub pour le push (refresh via la GitHub App). Hérité par craig-transcript-record."
    required_for: full functionality
  - name: GOOGLE_APPLICATION_CREDENTIALS
    prompt: "Chemin du JSON Service Account Drive. Hérité par craig-transcript-record."
    required_for: full functionality
  - name: AUDIO_DRIVE_FOLDER_ID
    prompt: "ID du dossier Drive Craig. Hérité par craig-transcript-record."
    required_for: full functionality
  - name: GROQ_API_KEY
    prompt: "Clé API Groq. Hérité par craig-transcript-record."
    required_for: full functionality
  - name: CRAIG_WATCH_MAX_AGE_HOURS
    prompt: "Âge max d'un pending avant expiration (défaut 24). Au-delà, on considère que Craig a crashé ou que le recording a été abandonné, et on drop l'entrée."
    required_for: optional
  - name: DISCORD_HOME_CHANNEL
    prompt: "ID du canal Discord humain où meeting-debrief poste le recap (PAS #craig-events). Hérité par meeting-debrief/debrief.py au step 5."
    required_for: full functionality
  - name: MEETING_DEBRIEF_MIN_DURATION_S
    prompt: "Durée min en secondes pour qu'un recording soit débriefé (défaut 180 = 3 min). Utilisé au step 5 pour skip cheap. Hérité par meeting-debrief/debrief.py."
    required_for: optional
---

# Craig Watch

> ⚠️ **User-driven only / fallback @mention.** Depuis Phase 1 du pipeline déterministe (avril 2026), le cron `craig-watch-followup` invoque `craig-pipeline` (script Python orchestrateur), PAS ce skill. Ce skill `craig-watch` reste utile uniquement pour : (a) une commande user manuelle (« où en est le recording de tout à l'heure ? »), (b) un fallback de debug si `pipeline.py` est cassé et qu'on veut réinvoker `watch.py` à la main. Pour le flow normal cron, voir `craig-pipeline/SKILL.md`.

Cron qui fait le pont entre la phase « recording en cours » (capturée par `craig-listener`) et la phase « transcription » (gérée par `craig-transcript-record`). Comble le trou laissé par l'absence de `on_message_edit` dans Hermes.

## Pourquoi ce skill existe

Craig édite **le même** message Discord pour passer de `🔴 Recording...` à `Recording ended.` (même `message_id`). Hermes upstream ne supporte pas `on_message_edit` → on ne peut pas écouter la transition en event-driven. Ce skill polle la transition via l'API Discord REST.

## When to Use

**Mode normal : NON, n'invoque pas ce skill.** Le cron `craig-watch-followup` invoque `craig-pipeline` (orchestrateur déterministe Python). Si tu vois ce skill mentionné dans une discussion utilisateur :

- Demande de status (« où en est la transcription du recording de tout à l'heure ? ») : `ls $WIKI_PATH/.craig-pending/` pour voir ce qui est en vol. Pas besoin d'invoquer le script.
- Fallback debug si `craig-pipeline/pipeline.py` est cassé et que l'utilisateur demande explicitement de relancer `watch.py` à la main : suis la procédure ci-dessous.

## Procedure (pour le run cron)

### 1. Lance `watch.py`

```bash
uv run --with google-auth --with google-api-python-client --with requests \
    /opt/data/skills-shared/craig-watch/watch.py
```

Le script :
1. Liste `$WIKI_PATH/.craig-pending/*.json`.
2. Pour chaque entrée :
   - Si âge > 24h (ou `CRAIG_WATCH_MAX_AGE_HOURS`) → drop → entry dans `expired[]`.
   - Sinon, `GET /channels/{channel_id}/messages/{message_id}` via bot token.
   - Si le body contient `"Recording ended."` → invoque `craig-transcript-record/scan.py --craig-id <id>` :
     - `processed` → ajoute à `processed[]`, supprime le pending.
     - `skipped` → ajoute à `skipped[]`, supprime le pending.
     - `error` → ajoute à `errors[]`, **garde** le pending (retry au prochain tick).
   - Sinon → entry dans `still_pending[]`, on garde l'entrée.

### 2. Lis le JSON agrégé

```json
{
  "checked": N,
  "processed": [{"craig_id": "...", "path": "raw/transcripts/...", "drive_id": "...", "name": "...", "progress": [...]}, ...],
  "skipped":   [{"craig_id": "...", "reason": "...", "progress": [...]?}, ...],
  "still_pending": [{"craig_id": "...", "first_seen_at": "..."}, ...],
  "expired":   [{"craig_id": "...", "first_seen_at": "...", "age_hours": N}, ...],
  "errors":    [{"craig_id": "...", "stage": "discord-fetch|scan-py|...", "detail": "...", "progress": [...]?}, ...]
}
```

Le champ `progress` (présent quand `scan.py` a tourné) est une liste ordonnée des phases franchies :

```json
[
  {"status": "progress", "phase": "drive-poll-start"},
  {"status": "progress", "phase": "zip-found", "name": "...", "size_bytes": 12345678},
  {"status": "progress", "phase": "groq-start"},
  {"status": "progress", "phase": "writing-raw"}
]
```

Tu t'en sers pour rédiger une UX live côté Discord (cf. step 2.5 ci-dessous).

### 2.5. UX Discord : status message édité, pas spam

Entre le moment où Craig pose `Recording ended.` et la notif finale ✅, il s'écoule 1-15 min (Drive cook + upload + Groq). L'utilisateur DOIT savoir qu'on est en train de bosser, sans recevoir N messages distincts.

**Règle absolue** : **un seul** message Discord par batch de recordings, **édité** une fois à la fin. Jamais plusieurs nouveaux messages.

⚠️ **Limite architecturale connue** : `watch.py` est synchrone. Quand il revient avec son JSON, scan.py a déjà fini toutes ses phases (Drive poll + Groq + write). Le champ `progress[]` arrive en post-hoc, pas en live. Donc on ne peut PAS poster un message phase par phase pendant que ça tourne. La seule option est un pattern **2 temps** :

| Étape | Action LLM Discord |
|---|---|
| **AVANT** de lancer `watch.py` (s'il y a des entrées `still_pending` à traiter) | Poste un message dans `DISCORD_HOME_CHANNEL` : « ⏳ Je traite N recording(s) Craig en attente : `<craig_id_1>`, `<craig_id_2>`… (Drive cook + Groq, 1-15 min). Je reviens avec le résultat. ». **Mémorise son `message_id`** pour l'édition finale. |
| **APRÈS** le retour de `watch.py`, pour chaque entrée `processed[i]` une fois commit/push/ingest fait | **Édite** ce même message en y ajoutant : « ✅ `<craig_id>` → `<filename>` — N pages wiki updatées : [[a]], [[b]] ». Si plusieurs entrées processed, liste-les sous le même msg. |
| Entrée `error` / `expired` / `drive-poll-timeout` dans le résultat | **Édite** le même message avec ⚠️ + `craig_id` + `reason` + `detail`. |

**Quand NE PAS poster de message du tout** :
- `result["still_pending"]` vide (rien à traiter ce tick) → silence total, ne lance même pas watch.py si tu peux le savoir d'avance via `ls .craig-pending/`.
- `result["processed"]` + `result["errors"]` + `result["expired"]` tous vides à la fin (rien à signaler) → si tu avais posté le ⏳ initial, **édite-le en `🤷 rien à signaler ce tick (tout encore en pending)`** plutôt que de le laisser bloqué sur ⏳.
- `skipped` avec `reason: already-transcribed` exclusif → silence, supprime le ⏳ initial si posté.

**Note sur le `progress[]`** : tu peux l'inclure dans le message final pour le diagnostic (« Drive zip 42 MB, Groq OK, 2m13s de transcription ») — utile pour repérer un slowness anormal. Pas obligatoire en mode normal.

**Future amélioration (Bloc B+)** : pour avoir un vrai live phase-par-phase, faudrait découper scan.py en stages invoqués sur ticks craig-watch séparés (à 30s ou 1 min), chaque tick éditant le message. Architecture lourde, hors scope actuel.

### 3. Pour chaque entrée de `processed[]` : commit + push + ingest

```bash
cd "$WIKI_PATH"
git pull --rebase
git add "<path>"
git commit -m "raw: capture transcript $(basename '<path>' .md)"
git push
```

Puis active le skill `llm-wiki` et applique son opération `ingest` sur le fichier (en automated mode, sans validation utilisateur).

### 4. Notification finale

- Si `processed` non vide : la notif principale est l'**édition** du message de status (cf. step 2.5) avec le ✅ final, contenant filename + pages wiki updatées. Pas de récap séparé dans `DISCORD_HOME_CHANNEL` — le user lit le status message édité et c'est tout.
- Si `still_pending` ou `skipped` (sans status message déjà posé) : silence (cron, pas de bruit inutile).
- Si `expired` non vide : notifie ⚠️ avec les IDs expirés (utilisateur peut décider de relancer manuellement via `craig-scan` si le zip est quand même arrivé sur Drive).
- Si `errors` non vide : applique la **politique de dedup ci-dessous** avant de notifier — sinon une erreur persistante (Drive sharing cassé, Groq down) générera 288 notifications sur 24h.

### 5. Pour chaque entrée de `processed[]` : invoque `meeting-debrief`

Une fois le commit + push + ingest faits (step 3) et le status message édité ✅ (step 4), enchaîne avec un debrief humain destiné au user — le status message édité reste un log technique court ; le recap structuré (TLDR + décisions + actions à valider) part dans `DISCORD_HOME_CHANNEL` comme un message neuf, sous lequel un thread est ouvert pour valider les actions.

a. **Skip cheap** : si `processed[i].duration_s` est connu et < `MEETING_DEBRIEF_MIN_DURATION_S` (défaut 180), skip — pas besoin de générer le JSON.

b. **Lis** `raw/transcripts/<path>.md` (le `path` est `processed[i].path`, wiki-relatif).

c. **Génère** le JSON debrief conforme à `meeting-debrief/schema.json` (cf. `meeting-debrief/SKILL.md` § Schéma).

d. **Écris-le** dans `$WIKI_PATH/raw/debriefs/<basename>.json` où `<basename>` = nom du transcript sans `.md` (mirror strict pour la corrélation, ex. `2026-04-26-1240-craig.md` → `2026-04-26-1240-craig.json`).

e. **Invoke** :

   ```bash
   uv run --with requests --with jsonschema --with pyyaml \
       /opt/data/skills-shared/meeting-debrief/debrief.py \
       --debrief-path raw/debriefs/<basename>.json
   ```

f. **Lis le JSON de sortie** :
   - `posted` → silence (le recap est sur `DISCORD_HOME_CHANNEL`, séparé du status message).
   - `skipped` (`too-short` / `already-debriefed` / `no-actions`) → silence.
   - `error` → notifie ⚠️ avec `reason` + `detail` (ce sont des erreurs locales, pas des notifs cron-spam).

Ne commit PAS `raw/debriefs/<…>.json` ici (le push est géré par le pipeline `llm-wiki ingest` au prochain tick si on veut versionner le JSON, ou ignoré sinon — au choix de hermes-infra).

### Politique de dedup d'erreurs (à appliquer côté LLM, pas côté script)

Le script garde les pending entries en cas d'erreur transitoire — le tick suivant retry. Conséquence : si une erreur est persistante, le même `craig_id` reviendra dans `errors[]` à CHAQUE tick. Pour éviter le spam :

1. **Avant de notifier** une entrée `errors[i]`, lis `$WIKI_PATH/.craig-pending/<craig_id>.json` :
   - Si le fichier contient un champ `notified_errors` listant une erreur identique (même `stage` + même `reason`/`detail`), **ne notifie PAS** — silence.
   - Sinon, notifie ⚠️ une seule fois avec `craig_id`, `stage`, `detail`.

2. **Après avoir notifié**, mets à jour le pending JSON pour ajouter l'erreur signalée :

   ```bash
   uv run --with - python3 - "$WIKI_PATH/.craig-pending/<craig_id>.json" "<stage>" "<reason_or_detail>" <<'PY'
   import json, sys, datetime
   p, stage, detail = sys.argv[1], sys.argv[2], sys.argv[3]
   with open(p) as f: d = json.load(f)
   d.setdefault("notified_errors", []).append({
       "stage": stage, "detail": detail,
       "at": datetime.datetime.now(datetime.timezone.utc).isoformat()
   })
   with open(p, "w") as f: json.dump(d, f, indent=2, ensure_ascii=False)
   PY
   ```

3. **Cas particuliers à toujours notifier** (même si déjà signalés) :
   - `expired[i]` (le pending vient d'être droppé après 24h) — événement final, pas erreur transitoire.
   - `errors[i]` avec `stage == "scan-py"` ET `reason == "format-mismatch"` provenant en amont de craig-listener — code regex cassé, urgent.

4. Quand le pending est résolu (`processed` ou `skipped`), le script supprime le fichier — donc `notified_errors` disparaît avec, ce qui est le comportement voulu (la prochaine fois qu'on revoit ce craig_id, on repart à zéro).

## Cron config (côté Hermes)

**Pas de cron permanent à provisionner.** Le cron `craig-watch-followup` est créé **dynamiquement** par `craig-listener/listener.py` (subprocess `hermes cron create`) à chaque nouveau recording, et **s'auto-supprime** quand `.craig-pending/` se vide. Depuis Phase 1 (avril 2026), le cron est attaché au skill `craig-pipeline` (orchestrateur déterministe), pas à `craig-watch` — voir `craig-pipeline/SKILL.md` pour le flow complet.

Cadence 5 min : la latence end-to-end est dominée par le cook+upload Craig (1-15 min), donc tick plus fréquent ne change rien.

## Pitfalls

- **Ne jamais supprimer un pending avant `processed` ou `skipped`.** Un crash mid-transcription ne doit pas perdre l'entrée — le tick suivant retry.
- **Ne pas batcher les commits/push** sur plusieurs `processed`. Un commit par recording, sinon le log devient illisible.
- **Pas de parallélisme** sur scan.py (chaque invoke prend potentiellement 1-15 min de poll Drive). Le script traite séquentiellement, ne change pas ça.
- **`scan-py` `error` → on GARDE le pending** : c'est une erreur transitoire (Drive timeout, Groq 5xx, etc.). Le tick suivant retry. Si l'erreur persiste 24h → expire.
- **Discord rate-limit** : 1 GET par pending par tick, 5 min entre ticks → on est très loin des limites (50 req/s par bot).

## Verification

Après un run réussi :

1. Pour chaque `processed[i]` : `$WIKI_PATH/raw/transcripts/<filename>.md` existe, frontmatter complet (cf. `craig-transcript-record` SKILL.md).
2. `$WIKI_PATH/.craig-pending/<craig_id>.json` n'existe plus pour les processed/skipped/expired.
3. `git log` montre les commits raw + ingest.
4. Notification Discord postée si `processed` non vide.

## Notes

- **Pourquoi pas de daemon `setTimeout` récursif** : Hermes n'a pas de modèle daemon long-lived. Chaque skill = 1 run LLM qui termine. Cron est le pattern natif, et l'état dans `.craig-pending/` survit aux restarts container.
- **`.craig-pending/`** est gitignoré (state runtime, pas knowledge).
- **Pourquoi pas d'API Craig** : Craig n'expose pas d'endpoint pour query l'état d'un recording par ID. La seule signal disponible est l'édition du panel Discord.

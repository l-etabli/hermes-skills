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
---

# Craig Watch

Cron qui fait le pont entre la phase « recording en cours » (capturée par `craig-listener`) et la phase « transcription » (gérée par `craig-transcript-record`). Comble le trou laissé par l'absence de `on_message_edit` dans Hermes.

## Pourquoi ce skill existe

Craig édite **le même** message Discord pour passer de `🔴 Recording...` à `Recording ended.` (même `message_id`). Hermes upstream ne supporte pas `on_message_edit` → on ne peut pas écouter la transition en event-driven. Ce skill polle la transition via l'API Discord REST, à intervalle régulier (5 min).

## When to Use

**Activé uniquement par cron** (config Hermes), jamais par un message utilisateur. Le cron passe un prompt générique du genre « active craig-watch et fais ce qu'il dit ».

Si tu vois ce skill mentionné dans une discussion utilisateur : c'est probablement une demande de status (« où en est la transcription du recording de tout à l'heure ? »). Dans ce cas :
- `ls $WIKI_PATH/.craig-pending/` pour voir ce qui est en vol.
- Pas besoin d'invoquer le script juste pour ça.

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

### 2.5. UX Discord : ÉDITER un message de status, pas en spammer N

Entre le moment où Craig pose `Recording ended.` et la notif finale ✅, il s'écoule 1-15 min (Drive cook + upload + Groq). L'utilisateur DOIT savoir où on en est sans recevoir 4 messages distincts.

**Règle absolue** : **un seul** message Discord par recording, **édité** à chaque transition. Jamais N nouveaux messages.

| Phase | Action LLM Discord |
|---|---|
| craig-watch détecte `Recording ended.` (avant `invoke_scan`) — vu via le passage de `still_pending[]` à `processed[]` entre deux ticks | Poste **un nouveau message** dans le canal d'origine du panel Craig (pas `#craig-events` — le canal dans lequel le user a parlé du recording, ou `DISCORD_HOME_CHANNEL` à défaut) : « 🎙️ Recording terminé (`craig_id`), je récupère le zip Drive… ». **Mémorise son `message_id`** dans le contexte de ce run. |
| `progress[i].phase == "zip-found"` | **Édite** ce message : « 📥 Zip trouvé (`<size_bytes>` → format MB), transcription Groq en cours… ». |
| `progress[i].phase == "groq-start"` | (optionnel — peut sauter, la phase précédente est encore lisible.) |
| `progress[i].phase == "writing-raw"` | (optionnel.) |
| Résultat `processed` | **Édite** : « ✅ Transcript intégré : `<filename>` — N pages wiki updatées : [[a]], [[b]] ». |
| Résultat `error` / `expired` / `drive-poll-timeout` | **Édite** avec ⚠️ + `reason` + `detail`. |

**Persistance du `message_id` entre ticks cron** : approche v1 = mémoire LLM dans le contexte du run en cours. C'est suffisant pour le cas standard où la chaîne complète (zip-found → processed) tient en un ou deux ticks de 5 min. **Limite connue** : si la chaîne s'étale sur plusieurs ticks (Drive lent + Groq lent), chaque tick redémarre avec un contexte vide → tu perds l'ID du message à éditer. v2 (à implémenter si on rencontre le cas) : stocker `discord_status_message_id` dans le pending JSON au moment du premier post.

**Ne pose PAS de status message si** :
- `result["processed"]`, `result["skipped"]`, `result["errors"]` sont tous vides ou ne concernent que des entrées du tick courant pour lesquelles tu n'as encore rien posté → silence (le user n'attend rien, ou alors on est dans un tick "rien à signaler").
- Le résultat est `skipped` avec `reason: already-transcribed` ET tu n'as pas encore posté de status pour ce craig_id → silence (déjà transcrit avant, pas de bruit).

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

À ajouter dans `instances/<name>/config.yaml` (ou équivalent selon version Hermes) :

```yaml
crons:
  - name: craig-watch
    cron: "*/5 * * * *"
    prompt: "Active le skill craig-watch et exécute sa procédure complète. Si rien à signaler (no processed, no expired, no errors), reste silencieux."
```

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

---
name: craig-scan
description: "Rattrapage manuel du backlog Craig. Liste les recordings finis présents dans l'historique de #craig-events qui n'ont jamais été transcrits ni en train d'être watchés, présente la liste à l'utilisateur, attend sa sélection (« tous », « les 3 derniers », IDs précis), puis déclenche craig-transcript-record/scan.py pour chaque ID validé."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, craig, discord, backlog, manual]
    category: voice
    related_skills: [craig-listener, craig-watch, craig-transcript-record, llm-wiki]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki. Utilisé pour la dédup contre raw/transcripts/ et .craig-pending/."
    required_for: full functionality
  - name: CRAIG_DISCORD_BOT_TOKEN
    prompt: "Token du bot Discord dédié aux skills Craig. Utilisé pour lister l'historique de #craig-events via l'API REST."
    required_for: full functionality
  - name: CRAIG_EVENTS_CHANNEL_ID
    prompt: "ID Discord du #craig-events de cette instance. Clic droit sur le salon > Copier l'ID (Developer Mode ON)."
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
---

# Craig Scan

Outil de **rattrapage manuel** du backlog Craig. À déclencher quand des recordings ont été ratés (Hermes down au moment de la fin du recording, oubli de configuration, redéploiement, etc.).

## When to Use

L'utilisateur demande explicitement, via Discord :
- « scanne les recordings »
- « rattrape les enregistrements qu'on a ratés »
- « il y a des trucs Craig pas encore transcrits »
- « check le backlog craig »

**Ne pas activer** :
- En cron — c'est `craig-watch` qui gère le steady-state. `craig-scan` est explicitement manuel.
- Sur un Recording ID que l'utilisateur fournit lui-même — utilise plutôt `craig-transcript-record` directement (pas besoin de scanner l'historique).

## Procedure

### 1. Acknowledge

Réagis ⏳ au message utilisateur.

### 2. Lance `discover.py`

```bash
uv run --with requests \
    /opt/data/skills-shared/craig-scan/discover.py
```

Le script :
1. Fetch les 100 derniers messages de `$CRAIG_EVENTS_CHANNEL_ID` via l'API Discord.
2. Garde ceux qui contiennent `Recording ID:` ET `Recording ended.` (recordings finis uniquement).
3. Dédupe contre :
   - `$WIKI_PATH/raw/transcripts/*.md` (filename match `*-craig-<id>-*.md`)
   - `$WIKI_PATH/.craig-pending/<id>.json` (déjà watchés par craig-watch)
4. Émet un JSON.

### 3. Parse le JSON

```json
{
  "channel_id": "...",
  "fetched": N,
  "candidates": [
    {
      "craig_id": "GDy7gHgSqJC5",
      "message_id": "...",
      "message_url": "https://discord.com/channels/.../.../...",
      "started_at": "15:06:15",
      "channel_name": "salon de test",
      "posted_at": "2026-04-25T13:06:15+00:00"
    }, ...
  ],
  "already_transcribed": [{"craig_id": "...", "as": "..."}, ...],
  "already_pending":     [{"craig_id": "..."}, ...],
  "unparseable_craig":   [{"message_id": "...", "posted_at": "...", "snippet": "..."}, ...]
}
```

**Important** : si `unparseable_craig` n'est PAS vide, ça veut dire qu'on a vu des messages qui ressemblent à du Craig (présence de `🔴 Recording`, `Voice Region:`, `Autorecord`, etc.) mais où la regex `Recording ID:` a échoué. C'est le signal que le format Craig a probablement changé. **Notifie l'utilisateur ⚠️** avec le compte + un snippet, pour qu'il puisse vérifier et qu'on mette à jour la regex. Ne fail pas tout le run : continue avec les `candidates` valides.

### 4. **PAUSE — présente la liste à l'utilisateur**

C'est le step critique de ce skill : **ne pas transcrire automatiquement**. Présente le résultat dans Discord avec un format compact :

```
🔍 Backlog Craig — <N> recording(s) à rattraper :

  1. `GDy7gHgSqJC5` — 25/04 15:06 — #salon de test  → <message_url>
  2. `AbCdEf12345` — 24/04 09:30 — #réunion-piloti → <message_url>
  3. ...

Déjà transcrits : <K>   |   Déjà watchés : <P>

Lesquels veux-tu transcrire ? (« tous », « les 3 premiers », « 1 et 3 », ou colle des IDs)
```

Si `candidates` est vide, message court : « Rien à rattraper, le backlog est clean. » (mentionne `K` déjà transcrits + `P` déjà watchés si l'un est non-nul, sinon stop).

### 5. Attends la sélection utilisateur, puis transcris séquentiellement

Une fois l'utilisateur a répondu, parse sa sélection en liste de `craig_id`. Pour chaque ID, séquentiellement :

```bash
uv run --with google-auth --with google-api-python-client --with requests \
    /opt/data/skills-shared/craig-transcript-record/scan.py --craig-id <ID>
```

Lis le JSON renvoyé par `scan.py`. Pour chaque `processed` :
1. `cd "$WIKI_PATH" && git pull --rebase && git add <path> && git commit -m "raw: capture transcript ..." && git push`
2. Active `llm-wiki` et applique `ingest` sur le fichier.

Pour chaque `error`, log et continue avec l'ID suivant. Pour chaque `skipped`, silence.

### 6. Notification finale

```
✅ Backlog Craig rattrapé : <N>/<M> transcrits.
   - [[2026-04-25-craig-xxx]] (3 pages wiki)
   - [[2026-04-24-craig-yyy]] (1 page wiki)
   ⚠️ <K> erreurs : <liste IDs + raisons>
```

## Pitfalls

- **NE TRANSCRIS PAS automatiquement la liste entière.** L'utilisateur doit valider — c'est le contrat de ce skill. S'il dit « tous », OK, mais pose la question.
- **Séquentiel uniquement** : pas de parallélisme sur scan.py (chaque invoke peut prendre 1-15 min de poll Drive et ils se marchent dessus sur git).
- **`discover.py` ne touche pas Drive** : il ne fait que lire l'historique Discord. Si un recording est dans la liste, ça ne garantit pas que le zip soit déjà sur Drive (cook+upload Craig peut être en retard). C'est `scan.py` qui poll Drive (timeout 20 min).
- **Limite 100 messages** : c'est la limite single-page Discord. Si le backlog est plus profond, paginer (pas implémenté pour l'instant — prévenir l'utilisateur si on suspecte un backlog > 100).
- **Pas de filtrage par channel/instance** : on lit `$CRAIG_EVENTS_CHANNEL_ID` qui est l'unique channel auquel cette instance a accès dans cette catégorie. Discord garantit l'isolation.
- **Pas de `git remote set-url`, pas de `export GITHUB_TOKEN`** — credential helper côté infra.

## Verification

1. Pour chaque recording transcrit : voir checklist `craig-transcript-record` SKILL.md.
2. Le message utilisateur original a une réaction ✅ (ou ⚠️ si erreurs).
3. Aucune entrée nouvellement créée dans `.craig-pending/` (ce skill ne passe pas par le watcher).

## Notes

- **Pourquoi pas un cron de scan** : le steady-state est couvert par `craig-listener` + `craig-watch`. Un cron `craig-scan` ferait double emploi et risquerait de bombarder l'utilisateur de demandes de validation. Si on veut automatiser le rattrapage à terme, on enrobera ce skill dans un cron qui transcrit `tous` automatiquement (à discuter).
- **`channel_name`** dans la sortie de `discover.py` est extrait par regex du body Craig (`Channel: <name>`) — peut être `None` si le format change.

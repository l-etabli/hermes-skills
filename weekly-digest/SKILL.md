---
name: weekly-digest
description: "Digest hebdomadaire des messages Discord visibles à l'instance, posté dans `$CRAIG_HOME_CHANNEL` + appendé au `log.md` du KB. Mode passé en argument du cron : `--recap` (vendredi 17h, 3 bullets décidé/livré/noté) ou `--briefing` (lundi 9h, objectifs/actions/blocages/échéances). UN SEUL appel LLM via `hermes -z` et le provider GPT/OAuth configuré. Invoqué uniquement par cron."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [digest, weekly, discord, kb, cron]
    category: kb
    related_skills: [llm-wiki]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki. Le digest est appendé à log.md."
    required_for: full functionality
  - name: CRAIG_DISCORD_BOT_TOKEN
    prompt: "Token bot Discord pour lire l'historique 7j et POST dans le home channel."
    required_for: full functionality
  - name: CRAIG_HOME_CHANNEL
    prompt: "ID du canal où poster le digest (récap vendredi + briefing lundi : même canal pour continuité visuelle)."
    required_for: full functionality
  - name: GITHUB_TOKEN
    prompt: "Installation token GitHub App pour push log.md (refresh sidecar 45m)."
    required_for: full functionality
---

# weekly-digest (récap vendredi + briefing lundi)

## When to Use

⚠️ **Jamais en @mention.** Invoqué uniquement par 2 cron par instance :

- `weekly-recap` — vendredi 17h, `--recap` (3 bullets : décidé / livré / noté)
- `weekly-briefing` — lundi 9h, `--briefing` (objectifs / actions reprises / blocages / échéances)

Posté dans le **même canal** (`$DISCORD_HOME_CHANNEL`) — le briefing lundi affiche le récap vendredi juste au-dessus, continuité visuelle voulue.

## Comment l'invoquer

```bash
uv run --with requests --with pyyaml \
    /opt/data/skills-shared/weekly-digest/digest.py \
    --recap          # ou --briefing
    [--days 7]       # fenêtre, défaut 7j
    [--dry-run]      # n'écrit rien, retourne juste le digest
```

Le mode est exclusif (`--recap` XOR `--briefing`), passé par le cron, le skill ne devine pas.

Sortie JSON :

```json
{
  "status": "ok",
  "mode": "recap",
  "channels_scanned": 4,
  "messages_collected": 87,
  "discord_message_id": "1234567890",
  "log_sha": "a1b2c3d4"
}
```

## Workflow LLM (1 tour)

Le tick LLM qui exécute le cron fait UNE chose :

```bash
uv run --with requests --with pyyaml \
    /opt/data/skills-shared/weekly-digest/digest.py --recap
```

Et reporte la sortie JSON. **Pas d'orchestration LLM** : le script gère tout (Discord fetch, `hermes -z`, post, log append, commit/push).

## Architecture

```
Cron tick (vendredi 17h ou lundi 9h)
  │
  ▼  digest.py --recap | --briefing
  │
  ├─ 1. GET /users/@me/guilds → liste les guildes visibles au bot
  ├─ 2. Pour chaque guilde : GET /guilds/{id}/channels → text channels
  │       (le bot ne voit que les channels où il a Read Message History,
  │        donc le filtrage par catégorie/rôle est déjà fait par Discord)
  ├─ 3. Pour chaque channel : GET messages avant 7j (via snowflake `after`)
  │       agrège (channel, author, timestamp, content)
  ├─ 4. 1 appel `hermes -z` avec le provider GPT/OAuth configuré
  ├─ 5. POST le digest dans DISCORD_HOME_CHANNEL
  ├─ 6. Append au $WIKI_PATH/log.md (entrée datée + mode + digest brut)
  └─ 7. git add log.md + commit + push
```

## Prompts par mode

- **`--recap`** : « Tu es l'assistant <instance>. Voici les messages des 7 derniers jours sur les channels où tu es présent. Produis un récap court vendredi soir, **3 bullets max** : ce qui a été DÉCIDÉ, ce qui a été LIVRÉ, ce qui est À NOTER. Ton sec, factuel, pas de remplissage. Cite des @auteurs si pertinent. »
- **`--briefing`** : « Tu es l'assistant <instance>. Voici les messages des 7 derniers jours. Produis un briefing lundi matin : **OBJECTIFS** de la semaine, **ACTIONS REPRISES** du vendredi, **BLOCAGES** ouverts, **ÉCHÉANCES** proches. Prospectif et actionnable, pas un copier-coller du récap. »

## Idempotence

Aucune. Si le cron ratisse 2 fois (manuel + horaire), tu auras 2 posts. Acceptable en V1 (la fenêtre 7j change peu). Pour rejeter les doublons, regarder le dernier message du bot dans le canal — pas implémenté.

## Pitfalls

- **Pas de `pip install`** : `uv run --with requests --with pyyaml`.
- **Pas d'appel API LLM direct** : `hermes -z` réutilise le provider GPT/OAuth configuré sur l'instance.
- **Discord rate-limits** : le scan parcourt potentiellement N channels × M messages. Implémenté avec backoff exponentiel + respect du `Retry-After`. Sur ~10 channels et ~7j de trafic, ça reste sous 50 calls.
- **Token Discord scopes** : nécessite `View Channel` + `Read Message History` sur tous les channels à digérer (déjà acquis si le bot voit le canal).
- **`DISCORD_HOME_CHANNEL`** : doit être un text channel où le bot peut POST. Pas de check préalable, l'erreur Discord remonte tel quel dans le JSON.
- **`log.md`** : append-only, git pull --rebase avant écriture pour éviter les races avec Web Clipper / Craig pipeline.

## Verification

Après un run réussi :

1. Le digest est visible dans `#hermes-<instance>-home`.
2. `git log --oneline -n 1` côté KB montre `log: weekly-<recap|briefing> YYYY-MM-DD`.
3. La sortie JSON contient `discord_message_id` et `log_sha` non null.
4. Lancer manuellement pour valider : `sudo docker exec -u hermes hermes-perso /opt/hermes/.venv/bin/hermes cron run weekly-recap` (bypass horaire).

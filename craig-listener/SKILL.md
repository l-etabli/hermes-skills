---
name: craig-listener
description: "Active-toi sur tout message du bot Craig dans #craig-events de cette instance, même présenté comme vide ou sans Recording ID. Lance listener.py sans argument : il récupère le panneau via API Discord et le met en file pour craig-pipeline, que l'enregistrement soit en cours ou terminé. Les enregistrements terminés par le pipeline ou en quarantaine ne sont pas relancés."
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, transcript, craig, discord, event-driven]
    category: voice
    related_skills: [craig-pipeline]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki. Utilisé pour stocker l'état pending dans .craig-pending/ et conserver les états terminaux."
    required_for: full functionality
  - name: CRAIG_DISCORD_BOT_TOKEN
    prompt: "Token du bot Discord dédié aux skills Craig. Utilisé pour la self-discovery du message_id via l'API REST (le LLM ne sait pas extraire les IDs de son contexte de façon fiable)."
    required_for: full functionality
  - name: CRAIG_EVENTS_CHANNEL_ID
    prompt: "ID du canal Discord #craig-events de cette instance. Utilisé par la self-discovery pour trouver le message Craig correspondant à un Recording ID donné."
    required_for: full functionality

---

# Craig Listener

Pont **event-driven** entre Craig (bot Discord d'enregistrement vocal) et le pipeline de transcription. Trigger sur le message initial de Craig, écrit un état pending pour que `craig-pipeline` prenne le relais en cron.

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

## Cycle de vie

Craig publie un panneau au démarrage puis édite le même message à la fin. Le listener enregistre l'identifiant et crée si nécessaire le cron `craig-watch-followup`. Le pipeline refetch le panneau toutes les cinq minutes et attend `Recording ended.` avant de transcrire.

**Même si le panneau est déjà terminé, utilise cette file.** Aucune transcription, ingestion ou génération de résumé ne doit être orchestrée directement par le LLM.

## When to Use

Active-toi **immédiatement** sur un message du bot Craig dans `#craig-events`, y compris dans le thread créé par Hermes pour ce message. Lance `listener.py` sans argument même si le contexte affiche `(The user sent a message with no text content)` ou ne contient pas `Recording ID:`.

Le panneau Craig utilise Discord Components V2 : son texte peut être absent du contexte transmis par Hermes alors qu'il est présent sur Discord. **Un contexte vide ne permet pas de conclure que le panneau est vide ou sans identifiant.** Le script récupère le panneau via API et valide lui-même son contenu ; ne filtre pas sur le texte visible avant de l'appeler.

**Ne pas activer** sur un message humain ou d'un autre bot, ni pour un deuxième Recording ID si tu en traites déjà un dans ce run (séquentiel).

## Procedure

### 1. Acknowledge

Réagis ⏳ au message Craig si sa référence est disponible. Si Hermes ne fournit pas de référence exploitable, passe directement au script sans inventer d’identifiant ; la réaction ne doit pas bloquer le traitement.

### 2. Lance `listener.py`

Le script fait l'extraction regex + l'écriture pending. **Ne fais PAS l'extraction toi-même** — la logique critique vit dans le script (le LLM ignore parfois les instructions explicites du SKILL.md).

**Le script ne prend AUCUN argument**. Lance-le exactement comme ça, sans rien d'autre :

```python
import subprocess
r = subprocess.run(
    ["/opt/data/skills-shared/craig-listener/listener.py"],
    capture_output=True, text=True,
)
print(r.stdout)
```

C'est tout. Le script va chercher le dernier msg Craig dans `#craig-events` via API (`CRAIG_EVENTS_CHANNEL_ID` + `CRAIG_DISCORD_BOT_TOKEN` env), parse Components V2, extrait le Recording ID, écrit le pending.

**N'essaie PAS de** :
- ❌ passer `--message` / `--message-file` / `--channel-id` / `--message-id` — ces flags n'existent plus, le script va rejeter avec argparse error.
- ❌ recopier le body Craig depuis ton contexte — la source de vérité est Discord, pas toi. Tu as halluciné le Recording ID dans tous les tests précédents.
- ❌ wrapper dans `sh -c` ou `python3 ... --message "..."` — security scanner bloque (zero-width chars Craig).

Le script :
1. Récupère le dernier panneau Craig et extrait son identifiant depuis les composants Discord.
2. Refuse de relancer un identifiant présent dans `.craig-pending/completed/` ou `.craig-failed/`.
3. Crée une entrée `.craig-pending/<id>.json` si elle n'existe pas, sans écraser un suivi existant.
4. Vérifie le cron `craig-watch-followup` via `cron list --all` et le crée si absent (`--script craig-pipeline-runner.sh --no-agent --deliver local`). Un cron pausé reste pausé.

### 3. Lis le résultat JSON

- **`pending` / `already-pending`** : le pipeline prend le relais. Vérifie `followup_cron` : `created` ou `already-running` confirme le suivi. Si `paused` ou `failed: ...`, signale le statut opérateur ; ne reprends pas le cron automatiquement. Sinon, silence côté Discord.
- **`skipped` / `already-completed`** : cet enregistrement a déjà terminé le pipeline. Ne relance rien.
- **`skipped` / `quarantined`** : une intervention opérateur est requise. Ne supprime pas la quarantaine, ne crée pas de pending, ne relance pas la transcription. Le pipeline a déjà émis l'alerte de mise en quarantaine ; ne la répète pas à chaque événement.
- **`error` / `empty-craig-panel`** : signale le `detail` du script. Le panneau récupéré via API n'a pas de texte extractible. Ce résultat est distinct du contexte vide reçu par le LLM, qui impose d'appeler le script.
- **`error` / `format-mismatch`** : signale le `detail` du script. Aucun identifiant reconnu dans le panneau récupéré ; n'en invente pas.
- **`error` / `discord-discovery-failed`** : signale le `detail` du script, notamment une éventuelle erreur de permissions Discord.
- **Autre erreur** : signale `reason` et `detail`. Ne réessaie pas en boucle dans ce tour.

**Si le script échoue, aucun succès ne doit être annoncé.** Ne crée jamais de pending à la main et ne contourne pas le script. Ne termine pas un tour par une annonce d'action future : lance effectivement le script, puis rapporte uniquement son résultat si une intervention est nécessaire.

Le cron exécute toute la chaîne : transcription → commit/push → ingestion → résumé. Chaque tentative est limitée à 30 minutes par défaut ; les échecs répétés sont mis en quarantaine. Le cron se supprime lorsque la file est vide. Les états terminaux persistent pour empêcher une relance par un événement dupliqué.

## Pitfalls

- **Ne pas pré-extraire le Recording ID toi-même** avant d'appeler le script. La regex vit dans `listener.py` — c'est l'ADN de ce skill.
- **Ne pas filtrer par channel ID dans la prose ou en code.** Le routing Discord (permissions de catégorie) garantit déjà l'isolation.
- **Ne PAS poll Drive depuis le LLM** en attendant la fin du recording. Le rôle du listener est de capter l'événement et déléguer à craig-watch via le pending state. Bloquer le LLM pendant des heures = anti-pattern.
- **Pas de parallélisme** sur plusieurs Recording ID dans le même run.
- **Pas de `git remote set-url`, `git credential fill`, ni `export GITHUB_TOKEN`** — utilise `/usr/local/bin/hermes-wiki-git pull|push` pour le réseau.

## Verification

- `pending` / `already-pending` : le chemin retourné existe et `followup_cron` indique un suivi actif.
- `skipped` : l'état terminal existe ; aucune relance ni nouveau cron.
- `error` : le diagnostic vient du script, aucune réussite inventée.

## Notes

- **Pourquoi un script et pas du LLM** : extraction regex + écriture state = trivial, le coût LLM est inutile, et le LLM rate parfois des choses « évidentes ». Logique critique = code, pas prose.
- **Cohabitation Hermes / Craig** : Hermes a un filtre `DISCORD_ALLOW_BOTS` qui par défaut ignore les messages de bots. Pour que ce listener s'active, il faut soit lever le filtre globalement (les permissions Discord garantissent l'isolation) soit whitelister `#craig-events`. Voir hermes-infra côté config.
- **`.craig-pending/`** doit être gitignoré dans le wiki (state runtime, pas knowledge).

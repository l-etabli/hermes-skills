# hermes-skills

Skills custom partagés entre les instances Hermes (`nousresearch/hermes-agent`) déployées par [hermes-infra](https://github.com/l-etabli/hermes-infra).

Suit le standard ouvert [agentskills.io](https://agentskills.io) (chaque skill = un dossier avec un `SKILL.md` + ressources optionnelles).

## Pourquoi un repo dédié

Les 3 instances Hermes (perso, piloti, telluris) tournent sur le même VPS dans des containers séparés. Ce repo est cloné sur le VPS et **bind-mounté en read-only** dans chaque container via le mécanisme `skills.external_dirs` de Hermes — ainsi un même skill sert toutes les instances sans duplication, et un `git push` ici suffit à propager partout.

## Layout

```
hermes-skills/
├── README.md
├── craig-listener/           # event-driven : msg Craig → pending state
│   ├── SKILL.md
│   └── listener.py
├── craig-watch/              # cron : détecte fin de recording, lance la transcription
│   ├── SKILL.md
│   └── watch.py
├── craig-scan/               # manuel : rattrapage backlog avec validation utilisateur
│   ├── SKILL.md
│   └── discover.py
├── craig-transcript-record/  # primitive : --craig-id → poll Drive → Groq → raw/
│   ├── SKILL.md
│   └── scan.py
└── …
```

Un dossier par skill, à la racine, contenant au minimum un `SKILL.md`. La catégorisation sémantique vit dans le frontmatter (`metadata.hermes.category`), pas dans l'arborescence — on regroupera en sous-dossiers (`voice/`, `kb/`, `github/`, …) si la liste devient longue.

## Anatomie d'un `SKILL.md`

Frontmatter YAML obligatoire + corps Markdown.

```yaml
---
name: <skill-name>                       # identifiant unique, kebab-case
description: <une phrase>                # ce qui décide quand l'invoquer
version: 0.1.0                           # semver
platforms: [linux]                       # OS supportés
metadata:
  hermes:
    tags: [<liste>]
    category: <catégorie>
    requires_toolsets: [terminal]        # ne s'affiche que si dispo
required_environment_variables:
  - name: <VAR>
    prompt: <pourquoi tu en as besoin>
    required_for: full functionality
---

# Titre humain

## When to Use
Conditions précises de déclenchement.

## Procedure
Étapes détaillées (numérotées).

## Pitfalls
Pièges connus.

## Verification
Comment vérifier que ça a marché.
```

Hermes charge ce fichier en **progressive disclosure** : seuls `name` + `description` sont en contexte par défaut, le reste se charge à la demande. Tu peux donc être généreux dans le détail de la procédure sans coût pour les autres skills.

## Comment ajouter un skill

1. `mkdir -p <category>/<skill-name>/`
2. Écrire le `SKILL.md` avec la spec ci-dessus
3. (Optionnel) Ajouter `scripts/`, `references/`, `templates/`, `assets/`
4. Tester en local si possible (`hermes skills list` doit le voir une fois mounté)
5. PR + review + merge sur `main`
6. Pull côté VPS (manuel pour l'instant, cron/CI à venir)

## Qui peut écrire ?

- **Humains** (Jérôme & co.) : accès écrit direct via PR ou push direct sur `main` selon préférence.
- **Hermes (les agents)** : par défaut **lecture seule**. Le repo leur est bind-mounté en `:ro`. Un agent peut **proposer** une amélioration de skill via le canal Discord (« je pense qu'il faudrait améliorer X parce que Y ») ou en générant lui-même un patch dans son skills local — mais le merge dans ce repo passe par un humain.
- **Évolution possible** : si un agent démontre qu'il améliore correctement les skills, on peut lui donner les droits d'écriture via une GitHub App dédiée. À considérer après quelques mois d'usage.

## Déploiement

Géré par [hermes-infra](https://github.com/l-etabli/hermes-infra) :

- Clone du repo sur le VPS dans `/opt/hermes/skills-shared/`
- Bind-mount dans chaque container : `/opt/hermes/skills-shared:/opt/data/skills-shared:ro`
- Patch de `instances/<name>/config.yaml` :
  ```yaml
  skills:
    external_dirs:
      - /opt/data/skills-shared
  ```

## Skills inclus

Famille **Craig** — capture event-driven des recordings vocaux Discord (Craig bot) vers la KB :

| Skill | Catégorie | Trigger | Rôle |
|---|---|---|---|
| [`craig-listener`](./craig-listener/) | voice | Discord event | Capte le msg initial de Craig dans `#craig-events`, extrait le Recording ID, écrit un pending state dans `$WIKI_PATH/.craig-pending/` (le watcher prend le relais). Si le msg montre déjà `Recording ended.`, déclenche directement la transcription. |
| [`craig-watch`](./craig-watch/) | voice | Cron 5 min | Refetch chaque pending via l'API Discord, détecte la transition vers `Recording ended.`, invoque `craig-transcript-record/scan.py`, supprime le pending sur succès. Expire les pending > 24h. |
| [`craig-scan`](./craig-scan/) | voice | Manuel (utilisateur) | Liste les recordings finis dans l'historique de `#craig-events` qui n'ont jamais été transcrits ni watchés, présente la liste, **attend la sélection utilisateur**, puis transcrit séquentiellement les IDs validés. |
| [`craig-transcript-record`](./craig-transcript-record/) | voice | `--craig-id <ID>` (appelé par les 3 ci-dessus) | Primitive de transcription : poll Drive (30→60→120s, timeout 20 min), download le zip Craig, mix ffmpeg multi-pistes, Groq Whisper, écrit `raw/transcripts/<date>-craig-<id>-…md`. Idempotent via `drive_id` + `craig_id`. |

**Flux typique** : Craig poste `🔴 Recording...` → `craig-listener` écrit pending → recording dure N minutes → Craig édite en `Recording ended.` → `craig-watch` détecte (cron suivant) → invoque `craig-transcript-record/scan.py` → poll Drive jusqu'au zip (cook+upload Craig 1-15 min) → transcription Groq → raw écrit → LLM commit/push + `llm-wiki ingest`.

**Routing entre instances Hermes** : 100 % Discord (catégorie + rôle). Chaque instance ne voit que SON `#craig-events` via les permissions Discord. Aucune décision de filtrage côté skills.

## Liens

- Standard : [agentskills.io](https://agentskills.io)
- Hermes docs : [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Infra : [hermes-infra](https://github.com/l-etabli/hermes-infra)

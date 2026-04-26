---
name: wiki-quick
description: "Drop conversationnel rapide vers `raw/notes/<slug>.md` du vault llm-wiki + commit/push. Pour quand l'utilisateur demande explicitement « wiki ça », « ingest cette conv », « garde ce qu'on vient de dire ». Pose le contenu avec frontmatter (source: discord, captured_by: wiki-quick, channel, participants) et retourne le path. L'agent enchaîne ensuite avec `llm-wiki ingest <path>` (mêmes seuils que le pipeline Craig)."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [wiki, ingest, discord, capture, text]
    category: research
    related_skills: [llm-wiki, craig-pipeline]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki. Le skill écrit raw/notes/<slug>.md."
    required_for: full functionality
  - name: GITHUB_TOKEN
    prompt: "Installation token GitHub App (refresh sidecar 45m). Lu en fallback si /opt/data/.env est absent."
    required_for: full functionality
---

# wiki-quick (drop Discord -> raw/notes/)

## When to Use

L'utilisateur demande explicitement à archiver un échange ou une note libre dans le wiki :

- « wiki ça »
- « ingest cette conv »
- « garde ce qu'on vient de dire »
- « note ça : … »

**Contre-indications** :
- Lien web / PDF / fichier upload → utilise `llm-wiki ingest` directement (web_extract upstream).
- Transcript voix Craig → c'est `craig-pipeline` qui s'en occupe automatiquement.
- Question pure lecture (« résume les 50 derniers messages ») → réponds en direct, n'écris rien.

## Comment l'invoquer

Une seule commande :

```bash
uv run --with pyyaml \
    /opt/data/skills-shared/wiki-quick/quick_ingest.py \
    --channel "<channel-name-or-id>" \
    --participants "<csv>" \
    [--title "<titre court>"] \
    [--source-url "<url>"]
```

Le contenu (texte conversationnel, snippet, paste) passe sur **stdin**. Pipe-le ou utilise un heredoc — pas d'argument `--content` pour éviter les soucis de quoting sur du multi-ligne.

Exemple :

```bash
cat <<'EOF' | uv run --with pyyaml /opt/data/skills-shared/wiki-quick/quick_ingest.py \
    --channel "#perso-home" \
    --participants "jerome,manu" \
    --title "stack typescript piloti -> effect"
Manu pense que la stack TypeScript pour Piloti devrait migrer vers Effect.
Ses arguments : meilleur runtime errors, schema dependency injection.
Mon contre-argument : courbe d'apprentissage pour Louise, pas urgent.
À reprendre lundi.
EOF
```

Sortie JSON (sur stdout) :

```json
{
  "status": "ok",
  "path": "raw/notes/2026-04-26-2310-stack-typescript-piloti.md",
  "abs_path": "/opt/data/wiki/raw/notes/2026-04-26-2310-stack-typescript-piloti.md",
  "sha": "a1b2c3d4",
  "url": "https://github.com/l-etabli/kb-perso/commit/a1b2c3d4"
}
```

## Workflow LLM (3 étapes)

1. **Reconstituer le contenu** à archiver depuis le contexte de la conversation Discord (messages pertinents, sans bruit). Ajoute une ligne d'intro si utile (« contexte : on parlait de X »).
2. **Invoquer le script** avec les métadonnées (channel, participants minimum).
3. **Enchaîner avec `llm-wiki ingest <path>`** en utilisant le `path` retourné. Le skill llm-wiki décidera s'il faut créer/mettre à jour des pages `entities/` / `concepts/` selon les seuils du SCHEMA.

Ne fais PAS d'étape 4 « répondre à l'utilisateur en résumant » — le commit URL et le path retournés suffisent.

## Frontmatter écrit

```yaml
---
title: <titre>
source: discord
captured_by: wiki-quick
channel: <channel>
participants: [<csv split>]
created: YYYY-MM-DDTHH:MM:SSZ
source_url: <url ou null>
---
```

Pas de `sha256` (contenu conversationnel mute, pas de re-ingest depuis URL). `created` plutôt que `ingested` parce que c'est l'heure de la conversation, pas d'un download.

## Idempotence

Le slug inclut `YYYY-MM-DD-HHMM` -> collision ultra-rare (< 1 par minute par instance). Si collision exacte, le script suffixe `-2`, `-3`, etc. Le `git pull --rebase` avant écriture évite les races avec Obsidian Web Clipper / Craig pipeline.

## Pitfalls

- **Pas de `pip install`** : tout passe par `uv run --with pyyaml` (single dep).
- **Pas de `import openai` / `anthropic`** : ce skill ne fait AUCUN appel LLM. Il pose juste un fichier et push.
- **Le contenu passe sur stdin** : ne mets pas d'argument `--content` pour éviter le hell du quoting Bash sur du multi-ligne avec des backticks / dollars.
- **Le commit/push est inconditionnel** : si rien à committer (collision rebase), le script retourne `status: noop` sans erreur — l'agent peut continuer avec `llm-wiki ingest <path>` quand même.

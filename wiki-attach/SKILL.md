---
name: wiki-attach
description: "Ingere dans le llm-wiki une piece jointe (PDF, doc, image, audio) deja uploadee par l'utilisateur dans le message Discord courant. Le bridge Hermes upstream telecharge automatiquement les attachments via le bot token authentifie et les cache sous /opt/data/cache/documents/ (ou /opt/data/cache/images/, /opt/data/cache/audio/). Ce skill explique comment retrouver ces fichiers caches et enchainer avec `llm-wiki ingest <path>`. A utiliser quand l'utilisateur dit « ingere ce PDF », « ajoute ce doc au wiki », « wiki ces fichiers », « mets ces pieces jointes dans la KB »."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [wiki, ingest, discord, attachment, pdf, document]
    category: research
    related_skills: [llm-wiki, wiki-quick]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki (ex. /opt/data/wiki)."
    required_for: full functionality
---

# wiki-attach (ingest Discord attachment -> llm-wiki)

## When to Use

L'utilisateur a uploade un ou plusieurs fichiers (PDF, docx, image, audio, etc.) dans le message Discord courant et demande de les ajouter au wiki :

- « ingere ce PDF »
- « ajoute ce doc au wiki »
- « wiki ces fichiers »
- « mets ces pieces jointes dans la KB »
- « ces docs sont a mettre dans le llm-wiki puis a ingerer »

**Contre-indications** :
- Texte conversationnel pur, snippet, paste -> `wiki-quick`.
- Lien web public -> `llm-wiki ingest <url>` directement (web_extract upstream).
- Transcript voix Craig -> `craig-pipeline` automatique.

## Le piege a eviter (pourquoi ce skill existe)

**N'invente PAS de probleme d'auth Discord.** Le bridge upstream `gateway/platforms/discord.py` a deja telecharge tes attachments via la session bot authentifiee (`DISCORD_BOT_TOKEN`) et les a caches sur disque local. Tu n'as pas besoin de :

- Naviguer sur discord.com via browser (tu seras bloque par le login wall — c'est attendu, ton browser n'est pas connecte au compte Discord).
- Demander a l'utilisateur de copier-coller le contenu du PDF.
- Demander un nouveau token / cookies / OAuth.
- Demander un lien CDN « clic droit copier le lien ».

Les fichiers sont **deja la, sur disque, accessibles a ton tool `terminal`**. Cf. upstream `cache_document_from_bytes` (`gateway/platforms/base.py`) qui ecrit dans `get_document_cache_dir()` -> `{HERMES_HOME}/cache/documents/doc_<uuid12>_<filename_original>`.

Mode d'echec observe en prod le 2026-04-28 sur Telluris (modele gemini-3-flash-preview) : le LLM a halluciné un probleme d'auth, propose 3 workarounds inutiles, et a oublie d'aller voir `/opt/data/cache/documents/`. Ne reproduis pas.

## Workflow LLM (4 etapes, sans message d'attente entre les tool calls)

### 1. Localiser les fichiers caches

Les attachments du message courant sont dans :

```
/opt/data/cache/documents/      # PDF, docx, txt, md, csv, json, xml, yaml, zip, xlsx, pptx, ...
/opt/data/cache/images/         # png, jpg, gif, webp
/opt/data/cache/audio/          # ogg, mp3, wav, webm, m4a
```

Le nom de fichier garde le filename original suffixe a un uuid : `doc_<12hex>_<original-name.ext>`.

Liste les fichiers les plus recents pour identifier ceux du message courant :

```bash
ls -lt /opt/data/cache/documents/ | head -10
```

Croise avec le nom des fichiers que l'utilisateur a uploades (visible dans le contexte Discord — ex. `2026-04-28 Hydro-Geo - Peloton Platform Budgetary Proposal.pdf`). Si plusieurs fichiers cached recemment, prends ceux dont le suffixe matche les noms uploades.

**Si le contexte de session expose `media_urls`** (paths transmis directement par le bridge), prefere ces chemins-la — ils sont garantis pointer sur les fichiers du message courant, pas un lookup heuristique sur mtime.

### 2. (Optionnel) Verifier le mime / la taille

```bash
file /opt/data/cache/documents/doc_xxx_<filename>
ls -lh /opt/data/cache/documents/doc_xxx_<filename>
```

Le bridge a deja filtre sur `SUPPORTED_DOCUMENT_TYPES` (incluant `.pdf`) et capote a 32 MB par fichier. Si le fichier est absent du cache, c'est probablement un type non supporte ou un overflow taille — surface l'erreur, n'essaie pas de re-telecharger depuis Discord.

### 3. Extraire le contenu avec le tool `web_extract`

**`llm-wiki` n'est PAS une CLI.** Il n'existe **aucun binaire `llm-wiki` dans le PATH** — c'est un **playbook** (cf. `skills/research/llm-wiki/SKILL.md` upstream) que tu dois derouler avec **tes propres tools Hermes**, pas via un appel shell.

Pour extraire le texte/contenu d'un PDF (ou d'un .docx, .pptx, ...), invoque le **tool `web_extract`** d'Hermes sur le chemin local cache :

```
web_extract(url="/opt/data/cache/documents/doc_<uuid>_<file1>.pdf")
web_extract(url="/opt/data/cache/documents/doc_<uuid>_<file2>.pdf")
```

`web_extract` accepte les paths locaux **ET** les URLs (cf. SKILL.md upstream `llm-wiki` § Core Operations / Ingest etape ① : « PDF → use `web_extract` (handles PDFs), save to `raw/papers/` »). Sous le capot c'est le modele auxiliaire `auxiliary.web_extract` (typiquement gemini-2.5-flash, multimodal, cheap, gere bien les PDFs natifs).

**Anti-patterns observes en prod le 2026-04-28 sur Telluris (haiku-4.5)** — ne fais PAS :
- `pip install pypdf` / `pip install PyMuPDF` / `pip install fitz` -> pollue le container, ne survit pas au restart, et c'est inutile (web_extract gere les PDFs).
- `pdftotext -layout ...` -> n'est meme pas installe par defaut, et meme si c'etait le cas, web_extract est un meilleur passage (preserve la structure + multimodal).
- `python3 -c "import fitz; ..."` heredoc -> idem, contournement inutile.
- `browser_navigate file:///opt/data/wiki/raw/papers/...pdf` -> le browser ne rend pas les PDFs proprement, et ca consomme un browser session pour rien.
- `convert -density 150 ...pdf ...png` puis `vision_analyze` page par page -> tres lent, perd le texte vectoriel selectable, et c'est exactement ce que web_extract fait deja en interne mais en mieux.
- Tenter d'invoquer `llm-wiki ingest <path>` au shell -> commande inexistante, retourne "command not found".

### 4. Derouler l'ingest llm-wiki inline

Une fois le texte extrait via `web_extract`, applique les **6 etapes du playbook `llm-wiki` ingest** (cf. `skill_view: llm-wiki`, section "Core Operations / 1. Ingest") :

① **Capture raw** : ecris le markdown extrait dans `$WIKI_PATH/raw/papers/<descriptive-name>.md` avec frontmatter (`source_url: discord-attachment`, `ingested: YYYY-MM-DD`, `sha256: <body-hash>`). Optionnellement copie le PDF original a cote (`<descriptive-name>.pdf`) si la ré-inspection visuelle peut etre utile plus tard.
② **(Skippable en mode auto)** Discuter takeaways avec l'utilisateur — si la demande est explicitement « ingere ces docs » sans question, passe directement a ③.
③ **Check existing** : `search_files` dans `$WIKI_PATH/index.md` + grep pour les entites/concepts mentionnes (ex: « Peloton », « HGE », « Wellview ») — eviter les doublons.
④ **Write/update** pages `entities/` et `concepts/` selon les seuils de `SCHEMA.md` (≥ 2 mentions ou central a la source). Frontmatter complet, wikilinks (≥ 2 sortants par page), tags depuis la taxonomie SCHEMA, provenance `^[raw/papers/<file>.md]` quand 3+ sources.
⑤ **Update navigation** : ajouter les nouvelles pages a `index.md` + appender a `log.md` (`## [YYYY-MM-DD] ingest | <Source Title>` avec liste des fichiers crees/modifies).
⑥ **Commit + push** depuis `$WIKI_PATH` (`git add . && git commit -m "ingest: <source>" && git push`). Le token est dans `$GITHUB_TOKEN` (refresh sidecar 45m).

Pour chaque etape, utilise tes tools natifs (`terminal` pour les ecritures fs et git, `search_files` pour la recherche) — **pas** de subagent / `delegate_task`. Le subagent rallonge le temps et casse la traçabilite Discord (l'utilisateur ne voit plus ce que tu fais).

### 5. Confirmer en Discord

**Une seule fois, a la fin**, apres que tous les commits ont push : poste un court message listant les pages wiki creees/mises a jour et l'URL du commit GitHub.

**Ne termine PAS un tour sur un message d'annonce** (« Je vais maintenant ingerer... ») sans avoir lance le tool call qui suit. Cf. `AGENTS.md` § Anti-pattern 7 (vu en prod 2026-04-27 sur craig-transcript-record, et reproduit le 2026-04-28 sur ce meme skill).

## Pitfalls (recap des modes d'echec observes)

- **Le browser est inutile** sur discord.com — page de login systematique. Le fichier est sur disque local.
- **Pas de re-download via curl** depuis l'URL CDN — le bridge l'a deja fait avec auth, les liens CDN expirent vite.
- **N'invoque pas `wiki-quick`** — ce skill ne gere que du texte. Sa contre-indication te renvoie ici.
- **Plusieurs fichiers** : extrais et traite-les sequentiellement, pas en parallele (eviter race conditions sur git commit / `log.md`).
- **Si le fichier n'apparait pas dans le cache** : surface explicitement « le fichier `<name>` n'est pas dans `/opt/data/cache/documents/`, le bridge l'a probablement filtre (type non supporte > SUPPORTED_DOCUMENT_TYPES, ou taille > 32 MB) ». N'invente pas le contenu, ne demande pas le re-upload sans cette analyse prealable.
- **Pas de subagent** : `delegate_task` rend le LLM aveugle a son propre travail (vu en prod le 2026-04-28 : subagent est parti en boucle 4+ minutes sans rendre la main). Ingest est sequentiel et debuggable, fais-le inline.
- **Pas de skill auto-installe** : ne cree pas un skill custom `<inst>-wiki-ingest` via `skill_install` (vu sur Telluris, supprime le 2026-04-28). Si la procedure ici est insuffisante, edite ce fichier directement dans `l-etabli/hermes-skills` et push.

## Pourquoi pas un script `wiki_attach.py` dedie

L'ingest est un **playbook LLM** par construction : la valeur ajoutee est dans le jugement (quelles entites creer, quels wikilinks tracer, quelle taxonomie de tags appliquer) — pas dans le pipe shell deterministe. `web_extract` fait l'extraction, le LLM fait la synthese. Pas de code Python a maintenir.

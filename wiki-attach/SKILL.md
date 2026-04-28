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

### 3. Activer le skill `llm-wiki` et invoquer son operation `ingest`

**NE PAS** reimplementer l'ingest en Python heredoc. **NE PAS** copier le PDF dans `raw/papers/` a la main. Active le skill `llm-wiki` (`skill_view: llm-wiki`) et applique son operation `ingest` sur chaque chemin cache, dans l'ordre :

```
llm-wiki ingest /opt/data/cache/documents/doc_<uuid>_<file1>.pdf
llm-wiki ingest /opt/data/cache/documents/doc_<uuid>_<file2>.pdf
```

Le skill `llm-wiki` se charge de :
- Copier dans `raw/papers/` (ou `raw/articles/` selon le type) avec frontmatter sha256 + ingested timestamp,
- Decider de la creation/mise a jour de pages `entities/` / `concepts/` selon les seuils de `SCHEMA.md`,
- Appender a `log.md`,
- Commit + push sur le repo `kb-<instance>`.

### 4. Confirmer en Discord

**Une seule fois, a la fin**, apres que TOUS les ingest ont reussi : poste un court message qui liste les pages wiki creees ou mises a jour, et le commit URL renvoye par `llm-wiki ingest`.

**Ne termine PAS un tour sur un message d'annonce** (« Je vais maintenant ingerer... ») sans avoir lance le tool call qui suit. Cf. `AGENTS.md` § Anti-pattern 7 (vu en prod 2026-04-27 sur craig-transcript-record).

## Pitfalls

- **Le browser est inutile** : pas de `browser_navigate` sur discord.com, ca tombe sur la page de login systematiquement. Le fichier est deja sur disque.
- **Pas de re-download via curl** : meme si tu reussissais a extraire l'URL CDN du message, le bridge l'a deja fait pour toi avec auth. Re-fetcher serait redondant et echouerait sur les attachments expires.
- **N'invoque pas `wiki-quick`** : ce skill ne gere que du texte. Sa propre contre-indication te renvoie ici.
- **Plusieurs fichiers** : ingere-les **un par un** avec un tool call separe par fichier — `llm-wiki ingest` ne prend qu'un path a la fois.
- **Si le fichier n'apparait pas dans le cache** : surface explicitement « le fichier `<name>` n'est pas dans `/opt/data/cache/documents/`, le bridge l'a probablement filtre (type non supporte, > 32 MB) ». N'invente pas le contenu, ne demande pas a l'utilisateur de le re-uploader.

## Pourquoi pas un script `wiki_attach.py` dedie

Tout le travail utile est deja fait par le skill `llm-wiki` upstream (qui inclut PDF text extraction, OCR si besoin, dedup via sha256, creation de pages). Ce skill `wiki-attach` est purement un **playbook** pour le LLM : il decrit ou trouver les fichiers et dans quel ordre invoquer `llm-wiki`. Pas de code Python a maintenir.

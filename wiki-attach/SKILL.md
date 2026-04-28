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

### 3. Extraire le contenu du PDF (ou doc) avec `pdftotext`

**`llm-wiki` n'est PAS une CLI.** Il n'existe aucun binaire `llm-wiki` dans le PATH — c'est un **playbook** (cf. `skills/research/llm-wiki/SKILL.md` upstream) que tu derouleras toi-meme inline a l'etape 4. **`web_extract` n'est PAS un tool agent non plus** — c'est un modele auxiliaire interne (cf. `config.yaml: auxiliary.web_extract`), pas dans ta toolbox. Idem : `vision_analyze` ne lit que les **vraies images** (jpg/png), pas les PDFs.

**Le bon chemin dans cet environnement est `pdftotext`** (binaire poppler-utils). Il est present dans l'image extras hermes-agent-extras (cf. `hermes-infra/AGENTS.md` § "Image extras"). Invoque-le via `terminal` :

```bash
pdftotext -layout /opt/data/cache/documents/doc_<uuid>_<file>.pdf /tmp/<descriptive-name>.txt
cat /tmp/<descriptive-name>.txt | head -100   # verifier
```

Le flag `-layout` preserve les colonnes/tableaux (essentiel pour les devis et propositions structures). Pour des PDFs purement textuels la sortie est exploitable telle quelle.

**Si le texte vectoriel est insuffisant** (PDF scanne, schema, image de tableau) : passe par `pdftoppm` pour rasteriser, puis appelle `vision_analyze` sur chaque page :

```bash
mkdir -p /tmp/pages-<descriptive-name>
pdftoppm -png -r 150 /opt/data/cache/documents/doc_<uuid>_<file>.pdf /tmp/pages-<descriptive-name>/p
ls /tmp/pages-<descriptive-name>/
```

Puis invoque le **tool `vision_analyze`** (vrai tool dans la toolbox Hermes) sur chaque PNG sequentiellement.

**Si tu prefe Python** : `pypdf` et `pdfplumber` sont installes dans l'image extras :

```bash
python3 -c "
from pypdf import PdfReader
r = PdfReader('/opt/data/cache/documents/doc_<uuid>_<file>.pdf')
for i, page in enumerate(r.pages):
    print(f'=== page {i+1} ==='); print(page.extract_text())
"
```

**Anti-patterns formellement interdits** (chacun a fait perdre 30+ min en prod le 2026-04-28 sur Telluris haiku-4.5 avant ce patch) :

- ❌ `pip install pypdf` / `pip install PyMuPDF` / `pip install fitz` -> **inutile maintenant** : pypdf et pdfplumber sont preinstalles dans l'image extras. Si l'import echoue, surface l'erreur — ne re-installe rien.
- ❌ `python3 -c "import fitz; ..."` -> fitz/PyMuPDF n'est pas pre-install, et le besoin est couvert par pypdf. Ne te bats pas avec.
- ❌ `python3 -c "import zlib; ... pdf stream extraction"` -> reverse engineering du format PDF a la main, completement absurde.
- ❌ `browser_navigate file:///opt/data/cache/documents/...pdf` -> le browser ne rend pas les PDFs proprement et c'est lent.
- ❌ `python3 -c "from hermes_tools import web_extract"` -> `web_extract` n'est pas une fonction Python importable, c'est un modele aux interne (et meme pas un tool exposé a l'agent).
- ❌ `delegate_task` / subagent -> proscrit pour cet ingest, cf. § Pitfalls.
- ❌ Tenter `llm-wiki ingest <path>` au shell -> commande inexistante.

### 4. Derouler l'ingest llm-wiki inline

Une fois le texte extrait, applique les **6 etapes du playbook `llm-wiki` ingest** (cf. `skill_view: llm-wiki`, section "Core Operations / 1. Ingest") :

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

L'ingest est un **playbook LLM** par construction : la valeur ajoutee est dans le jugement (quelles entites creer, quels wikilinks tracer, quelle taxonomie de tags appliquer) — pas dans le pipe shell deterministe. `pdftotext` fait l'extraction (deterministe), le LLM fait la synthese (jugement). Pas de code Python a maintenir.

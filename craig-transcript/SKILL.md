---
name: craig-transcript
description: "Récupère la transcription WebVTT d'un enregistrement Craig (bot Discord) depuis une URL craig.horse/rec/<id>?key=<key>, la dépose dans raw/transcripts/ d'un wiki llm-wiki, puis enchaîne immédiatement avec l'opération ingest du skill llm-wiki pour la promouvoir en pages wiki."
version: 0.3.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, discord, transcript, raw-capture, llm-wiki]
    category: voice
    related_skills: [llm-wiki]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki (ex. /opt/data/wiki). Le skill llm-wiki utilise la même variable."
    required_for: full functionality
  - name: GITHUB_TOKEN
    prompt: "Token GitHub avec droit d'écriture sur le repo KB (refresh auto via la GitHub App de l'instance). Utilisé pour le push final."
    required_for: full functionality
---

# Craig Transcript

Capture la transcription d'un enregistrement Craig dans la couche **raw/** d'un wiki [llm-wiki](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/llm-wiki/SKILL.md), puis chaîne immédiatement l'opération `ingest` du skill `llm-wiki` pour promouvoir le transcript en pages wiki (entités, concepts, cross-références).

Rationale : un transcript capturé sans être ingéré, c'est de la connaissance dormante. La capture brute n'a de valeur que si elle alimente le réseau de pages wiki.

## When to Use

Active-toi quand un message dans un canal `*-scribe-inbox` (ou un message direct de l'utilisateur) contient une URL au format :

```
https://craig.horse/rec/<recording_id>?key=<key>
```

Régex : `https://craig\.horse/rec/([A-Za-z0-9]+)\?key=([A-Za-z0-9_-]+)`

Exemple : `https://craig.horse/rec/nhWUc76htgaR?key=mohN17`

Ignore les paramètres supplémentaires (`&delete=...`). Si plusieurs URLs sont collées, traite-les séquentiellement.

## Scope (deux phases distinctes, exécutées en chaîne)

**Phase A — Capture (responsabilité directe de ce skill)** :
- Pull le `.vtt` depuis l'API Craig (auth `?key=`)
- **Note technique** : Utiliser un `User-Agent` de navigateur (ex: Mozilla/5.0...) pour toutes les requêtes afin d'éviter les erreurs 403 Forbidden.
- Parse + reformate en Markdown lisible avec speakers
- Dépose dans `$WIKI_PATH/raw/transcripts/<date>-<slug>.md` avec **frontmatter raw** (sha256, source_url, ingested)
- Dédup via sha256 (skip toute la suite si déjà présent à l'identique)
- `git pull --rebase` + `commit` + `push` du raw

**Phase B — Promotion en pages wiki (déléguée au skill `llm-wiki`)** :
- Activé immédiatement après la phase A si un nouveau raw a été écrit
- Ce skill **n'implémente pas** l'extraction d'entités/concepts ni les writes dans `entities/` `concepts/` `index.md` `log.md` — c'est tout le boulot du skill `llm-wiki` opération `ingest`
- Ce skill se contente de l'**activer** sur le fichier raw fraîchement déposé

→ L'utilisateur n'a qu'une seule action à faire (coller l'URL). Les deux phases s'enchaînent.

## Procedure

### 1. Acknowledge et oriente-toi

- Réagis ⏳ au message d'origine
- Lis `$WIKI_PATH/SCHEMA.md` pour vérifier que la convention raw n'a pas été customisée par l'utilisateur (typiquement : champs frontmatter raw, sous-dossiers de `raw/`)
- Si `SCHEMA.md` n'existe pas ou que `$WIKI_PATH` n'est pas un wiki llm-wiki valide, abandonne et signale-le

### 2. Déclenche le job de transcription côté Craig

```
POST https://craig.horse/api/v1/recordings/<id>/job?key=<key>

Headers:
  Content-Type: application/json
  Origin: https://craig.horse
  Referer: https://craig.horse/rec/<id>?key=<key>

Body:
  {"type":"transcription","options":{"format":"vtt"}}
```

Le job tourne sur Runpod (Faster Whisper). Latence typique ~1:1 audio:transcription.

### 3. Polle le statut

Toutes les 30 secondes :

```
GET https://craig.horse/api/v1/recordings/<id>/job?key=<key>
```

Continue jusqu'à `status=finished` (ou équivalent observé). **Timeout 30 min** : au-delà, abandonne, ❌, post une erreur, conserve l'URL pour retry manuel.

### 4. Télécharge le VTT

Quand `status=finished`, le payload `GET job` contient l'URL ou un token de download. Récupère le `.vtt` (toujours avec `?key=<key>` si l'endpoint l'exige).

L'endpoint exact n'est pas documenté publiquement — découvre-le en inspectant la réponse au premier run et mémorise le pattern.

### 5. Parse le VTT

Format observé :

```
WEBVTT

NOTE Created with craig.chat

1
00:00:03.820 --> 00:00:20.140
<v Jérôme> <00:00:03.820><c>Salut</c> <00:00:04.460><c>Greg,</c> ...</v>
```

Pour chaque cue :
- **Speaker** = contenu du tag `<v NAME>` (global name Discord, déjà diarisé)
- **Texte** = strip les balises de per-word timing avec `/<\d+:\d+:\d+\.\d+>|<\/?[cv][^>]*>/g` → '', puis trim + collapse des espaces multiples
- **Timestamp** = début du cue (`00:00:03.820`), reformaté en `MM:SS` (ou `HH:MM:SS` si > 1h)

Récupère aussi : durée totale, liste des speakers détectés, date d'enregistrement (depuis le payload Craig si dispo, sinon date du jour).

### 6. Compose le contenu Markdown

Le contenu (qui sera sauvegardé dans `raw/transcripts/`) :

```markdown
---
source_url: https://craig.horse/rec/<id>
ingested: <YYYY-MM-DD>
sha256: <hex digest du body ci-dessous, sans le frontmatter>
---

# Transcription <titre humain>

- **Recording Craig** : `<id>`
- **Date** : <YYYY-MM-DD HH:MM> (recorded_at)
- **Durée** : <Xm Ys>
- **Participants** : <liste des speakers>
- **Source** : Discord vocal — Craig WebVTT (Faster Whisper)

[00:03] **Jérôme**: Salut Greg, je suis très curieux de savoir...

[00:18] **Jérôme**: Je ne peux pas t'inviter, j'ai obligé...
```

**Conventions importantes** (cf. SKILL.md llm-wiki) :
- Le frontmatter raw est **minimaliste** : juste `source_url`, `ingested`, `sha256`. Pas de `title`, `tags`, `type` — ces champs sont pour les pages wiki dans `entities/`, `concepts/`, etc.
- Le `sha256` est calculé sur le body (tout ce qui est après le `---` de fermeture du frontmatter), pas sur le frontmatter lui-même
- Une ligne vide entre chaque tour de parole
- Pas de `[[wikilinks]]` à ce stade — `raw/` est immuable, l'enrichissement vit dans des pages wiki séparées que créera l'opération `ingest`

### 7. Dédup et chemin final

**Chemin** : `$WIKI_PATH/raw/transcripts/<YYYY-MM-DD>-<slug>.md`

- `<YYYY-MM-DD>` = date du recording
- `<slug>` :
  - extrait du nom du salon Discord si dispo dans le payload Craig (slugifié : minuscules, accents retirés, espaces → tirets)
  - sinon demande à l'utilisateur un nom court avant d'écrire
  - fallback : `craig-<recording_id>`

**Avant d'écrire** :

1. `git -C "$WIKI_PATH" pull --rebase`
2. Si le fichier existe déjà :
   - Compute le sha256 du body actuel
   - Compare avec le sha256 du frontmatter du fichier existant
   - **Identique** → skip toute écriture, signale "transcript déjà présent et inchangé", ne re-déclenche RIEN
   - **Différent** (drift) → demande à l'utilisateur : écraser ? créer une nouvelle entrée avec slug suffixé (`-2`, `-bis`) ? abandonner ?
3. Si le fichier n'existe pas → écris-le

### 8. Commit + push

```bash
cd "$WIKI_PATH"
git add "raw/transcripts/<filename>"
git commit -m "raw: capture transcript Craig <date> <slug>"
git push
```

Le `$GITHUB_TOKEN` est rafraîchi par le sidecar GitHub App. Si le push échoue avec une erreur d'auth, attends 60s et retry une fois.

### 9. Confirme la phase A

Réagis 📥 au message d'origine pour signaler la fin de la capture (mais pas encore de l'ingestion). Pas besoin de poster un message texte à ce stade — la confirmation finale arrivera après la phase B.

### 10. Enchaîne la phase B — opération `ingest` du skill `llm-wiki`

Active le skill `llm-wiki` (via `skill_view("llm-wiki")` si tu ne l'as pas en contexte) et applique son opération **`ingest`** sur le fichier que tu viens d'écrire :

```
$WIKI_PATH/raw/transcripts/<filename>
```

Suis intégralement la procédure d'ingest définie dans le SKILL.md llm-wiki (section `## Core Operations → 1. Ingest`). En résumé, elle va :

- Lire le raw
- Identifier entités et concepts mentionnés (personnes, projets, décisions, sujets techniques)
- Vérifier ce qui existe déjà dans `index.md` (orientation préalable obligatoire : `SCHEMA.md` → `index.md` → derniers `log.md`)
- Créer ou updater les pages dans `entities/`, `concepts/`, `comparisons/` selon les **Page Thresholds** (≥ 2 mentions OR central à 1 source)
- Ajouter les nouvelles pages à `index.md`
- Cross-référencer (≥ 2 wikilinks par page)
- Appender à `log.md` : `## [YYYY-MM-DD] ingest | <titre>` + liste des fichiers touchés
- Commit + push (un commit séparé pour la phase B, distinct du commit raw)

**Mode automated** : tu enchaînes sans demander de validation à l'utilisateur (le skill llm-wiki prévoit explicitement ce mode au step ② de l'ingest : "Skip this in automated/cron contexts — proceed directly").

**Si l'ingest échoue** (erreur, conflit git, page existante en contradiction, etc.) : ne fail pas tout. Le raw a déjà été commité (phase A réussie). Réagis ⚠️ et signale clairement à l'utilisateur ce qui a bloqué — il peut relancer manuellement l'ingest plus tard avec les bonnes corrections.

### 11. Confirme l'enchaînement complet

Une fois les deux phases terminées, réagis ✅ au message d'origine et poste dans le canal :

```
✅ Transcript intégré
   📥 Capture : `raw/transcripts/<filename>` (sha256: <8 premiers>)
   🧠 Ingest : <N> page(s) wiki créée(s)/updatée(s)
        - [[page-a]], [[page-b]], [[page-c]]
   📝 Log : `## [YYYY-MM-DD] ingest | <titre>`
```

Si la phase B a été skipped (raw déjà existant et inchangé) :
```
ℹ️ Transcript déjà présent et inchangé (sha256 match) — rien à faire.
```

## Pitfalls

- **Ne touche jamais à `raw/` après écriture** — sources immuables (cf. SKILL.md llm-wiki, ligne 478). Les corrections vivent dans des pages wiki.
- **Ne touche pas à `index.md` ni à `log.md`** — c'est le job de l'opération `ingest` du skill `llm-wiki`. Si tu les modifies depuis ce skill, tu duplique le travail et risques des conflits.
- **Frontmatter raw ≠ frontmatter wiki** — n'utilise PAS `title`, `tags`, `type`, `sources` ici. Ces champs sont réservés aux pages dans `entities/`, `concepts/`, etc. Le frontmatter raw est volontairement minimal.
- **Le `sha256` se calcule sur le body uniquement** (après le `---` de fermeture du frontmatter). Sinon le hash change à chaque ingestion à cause du `ingested:` qui bouge, et la dédup ne marche jamais.
- **Idempotence forte** : si le sha256 matche l'existant, **rien n'est écrit, rien n'est commité, rien n'est pushé, et aucun job Craig n'est re-déclenché**. Ça économise des minutes Runpod côté Craig et garde un historique git propre.
- **Encodage UTF-8 strict** : les accents français doivent passer (parsing + écriture).
- **Speaker name avec caractères spéciaux** : préserve tels quels (espaces, accents, emojis dans `<v NAME>`).
- **Cues sans speaker** (rare) : utilise `???` comme placeholder.
- **VTT vide** (recording sans paroles) : préviens l'utilisateur, n'écris rien dans `raw/`.
- **Job qui échoue côté Craig** : ❌, post l'erreur lisible, conserve l'URL.
- **Key invalide / recording supprimé** (Craig 403/410) : ❌, signale clairement.
- **Réseau qui drop** : retry 3 fois avec backoff exponentiel (30s → 60s → 120s).

## Verification

Après exécution complète (phases A + B), vérifie :

**Phase A** :
1. `$WIKI_PATH/raw/transcripts/<filename>.md` existe avec le bon frontmatter (3 champs : `source_url`, `ingested`, `sha256`)
2. Le sha256 dans le frontmatter correspond bien au hash du body
3. `git log` dans `$WIKI_PATH` montre le commit "raw: capture transcript Craig …"

**Phase B** :
4. Au moins une page dans `entities/` ou `concepts/` mentionne le fichier raw dans son frontmatter `sources:` (sauf si l'ingest n'a rien jugé promotable selon les seuils)
5. `log.md` contient une entrée `## [YYYY-MM-DD] ingest | <titre>` qui liste les pages touchées
6. `git log` montre un commit séparé pour l'ingest (ex: "wiki: ingest transcript …")
7. `git push` a réussi pour les deux commits

**Final** :
8. Tu as réagi ✅ au message d'origine
9. Le message de confirmation final est posté avec la liste des pages wiki créées/updatées

Si un point échoue, signale-le clairement plutôt que de prétendre que ça a marché.

## Notes

- **Auth Craig** : le couple `(recording_id, key)` est la seule auth requise. Pas de cookie, pas d'OAuth.
- **Coût** : 0€ par transcription (inclus dans l'abo Tier 3 Patreon de l'utilisateur, $8/mois fixe).
- **Latence Runpod** : ~1:1 audio:transcription. Pour 1h de meeting, prévois ~1h de wait. Le job continue côté Craig même si tu te déconnectes — tu peux re-poll après n'importe quel délai (rétention 14j en Tier 3).
- **Pourquoi WebVTT** plutôt que SRT/TXT : speakers structurés via tag W3C `<v NAME>`, parsing fiable, per-word timing optionnel.
- **Pourquoi pas d'auto-ingest** : le skill llm-wiki impose des seuils stricts (≥ 2 mentions ou central à 1 source pour créer une page). Cette décision relève de l'agent au moment de l'ingest, avec contexte sur l'index existant — pas du skill de capture qui n'a pas cette vue d'ensemble. Garder les deux séparés laisse aussi à l'utilisateur le choix de promouvoir ou non un transcript donné.

## Cron (non requis)

L'enchaînement A→B est synchrone dans ce skill — pas besoin de cron de retry pour l'ingestion routinière. Un cron pourrait servir uniquement comme **filet de sécurité** : scanner périodiquement `raw/transcripts/` pour détecter les fichiers non référencés dans aucun `sources:` de page wiki ni dans `log.md`, et relancer l'ingest sur eux. Utile seulement si la phase B échoue souvent et qu'on doit rattraper les échecs en différé. À considérer si ça arrive.

## Comment savoir si un raw est ingéré

Le frontmatter raw ne contient **pas** de marqueur d'ingestion (la convention llm-wiki garde `raw/` strictement immuable). Pour vérifier l'état d'un raw donné :

```bash
# Cherche la mention dans les pages wiki (frontmatter sources:)
grep -rl "raw/transcripts/<filename>" "$WIKI_PATH/entities" "$WIKI_PATH/concepts" "$WIKI_PATH/comparisons" "$WIKI_PATH/queries"

# OU cherche dans log.md
grep "<filename>" "$WIKI_PATH/log.md"
```

Si l'un des deux remonte du résultat, le raw est ingéré.

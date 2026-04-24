---
name: craig-transcript
description: "Récupère, formate et archive la transcription WebVTT d'un enregistrement Craig (bot Discord) depuis une URL craig.horse/rec/<id>?key=<key> et l'écrit dans le wiki au format llm-wiki."
version: 0.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [voice, discord, transcript, kb, wiki]
    category: voice
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault Obsidian/llm-wiki monté dans le container (ex. /opt/data/wiki)"
    required_for: full functionality
  - name: GITHUB_TOKEN
    prompt: "Token GitHub avec droit d'écriture sur le repo KB (refresh auto via la GitHub App de l'instance)"
    required_for: full functionality
---

# Craig Transcript

## When to Use

Déclenche-toi quand un message dans un canal `*-scribe-inbox` (ou que l'utilisateur t'envoie directement) contient une URL au format :

```
https://craig.horse/rec/<recording_id>?key=<key>
```

Exemple : `https://craig.horse/rec/nhWUc76htgaR?key=mohN17`

Régex de matching : `https://craig\.horse/rec/([A-Za-z0-9]+)\?key=([A-Za-z0-9_-]+)`

Ignore les paramètres supplémentaires (`&delete=...`).

Si plusieurs URLs sont collées dans le même message, traite-les séquentiellement.

## Procedure

### 1. Acknowledge

Réagis ⏳ au message d'origine pour signaler que le traitement a démarré. Logue le `recording_id` extrait dans ta réponse pour traçabilité.

### 2. Déclenche le job de transcription

```
POST https://craig.horse/api/v1/recordings/<id>/job?key=<key>

Headers:
  Content-Type: application/json
  Origin: https://craig.horse
  Referer: https://craig.horse/rec/<id>?key=<key>

Body:
  {"type":"transcription","options":{"format":"vtt"}}
```

Le job démarre côté Runpod (Faster Whisper). Latence typique : ~1:1 audio:transcription (1 minute d'audio → ~1 minute de wait).

### 3. Polle le statut

Toutes les 30 secondes :

```
GET https://craig.horse/api/v1/recordings/<id>/job?key=<key>
```

Le payload de réponse contient un champ de statut. Continue jusqu'à `status: "finished"` (ou équivalent observé empiriquement).

**Timeout** : 30 minutes. Au-delà, abandonne, réagis ❌, post une erreur lisible dans le canal, conserve l'URL pour retry manuel.

### 4. Télécharge le VTT

Quand `status=finished`, le payload de la réponse `GET job` contient l'URL ou un token de download du fichier `.vtt`. Extrais-le et fais un `GET` (toujours avec `?key=<key>` si l'endpoint l'exige).

L'endpoint exact n'est pas documenté publiquement — découvre-le en inspectant la réponse JSON au premier run et mémorise le pattern.

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

- **Speaker** = contenu du tag `<v NAME>` (c'est le global name Discord du locuteur, déjà diarisé par Craig)
- **Texte** = contenu du tag `<v>`, en strippant les balises de per-word timing
  - Regex de nettoyage : `/<\d+:\d+:\d+\.\d+>|<\/?[cv][^>]*>/g` → remplacer par chaîne vide
  - Puis trim + collapse des espaces multiples
- **Timestamp** = début du cue (`00:00:03.820`), à reformater en `MM:SS` (ou `HH:MM:SS` si > 1h)

Liste des speakers détectés = `Set` des `<v NAME>` rencontrés (utile pour le frontmatter).

### 6. Compose la page Markdown

**Chemin** : `$WIKI_PATH/raw/transcripts/YYYY-MM-DD-<slug>.md`

- `YYYY-MM-DD` = date du recording (depuis le payload `GET recording` si dispo, sinon date du jour)
- `<slug>` : 
  - si tu peux extraire le nom du salon Discord depuis le payload Craig, utilise-le (slugifié : minuscules, accents retirés, espaces → tirets)
  - sinon demande à l'utilisateur un nom court avant d'écrire
  - en fallback ultime : `craig-<recording_id>`

**Frontmatter** (convention llm-wiki, voir `$WIKI_PATH/SCHEMA.md`) :

```yaml
---
title: "<titre humain — typiquement le nom du salon ou un titre court>"
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
type: transcript
tags: [raw, transcript, voice, meeting]
sources:
  - craig_recording_id: <id>
    duration: <MMmSSs>          # depuis le payload, ex. 35m12s
    participants: [<liste des speakers détectés>]
    recorded_at: <ISO 8601 si dispo, sinon date du jour>
---
```

**Corps** :

```markdown
[00:03] **Jérôme**: Salut Greg, je suis très curieux de savoir...

[00:24] **Jérôme**: Je ne peux pas t'inviter, j'ai obligé...
```

Une ligne vide entre chaque tour de parole. Pas de wikilinks à ce stade — `raw/` est immuable, l'enrichissement se fait dans une page wiki séparée si besoin (cf. SCHEMA.md).

### 7. Commit + push

Avant écriture :

```bash
cd "$WIKI_PATH" && git pull --rebase
```

Après écriture :

```bash
cd "$WIKI_PATH"
git add "raw/transcripts/<filename>"
git commit -m "transcript: <date> <slug>"
git push
```

Le `$GITHUB_TOKEN` est rafraîchi automatiquement par le sidecar GitHub App (toutes les 45 minutes). Si le push échoue avec une erreur d'auth, attends 60s et retry.

### 8. Confirme à l'utilisateur

Réagis ✅ au message d'origine et poste dans le même canal :

```
✅ Transcript écrit dans `raw/transcripts/<filename>`
   Durée : <Xm Ys> · Participants : <liste>
```

## Pitfalls

- **Idempotence** : si `raw/transcripts/YYYY-MM-DD-<slug>.md` existe déjà, ne re-déclenche **pas** le job (coûte des tokens Runpod). Demande à l'utilisateur s'il veut écraser ou si c'est une session différente (auquel cas, suffixe le slug : `-2`, `-bis`, …).
- **Job qui échoue côté Craig** : réagis ❌, poste l'erreur lisible, conserve l'URL dans la réponse pour permettre retry manuel.
- **Key invalide / recording supprimé** : Craig renvoie 403 ou 410. Réagis ❌, signale clairement la cause.
- **Réseau qui drop pendant un poll** : retry 3 fois avec backoff exponentiel (30s → 60s → 120s) avant d'abandonner.
- **Encodage UTF-8** : le VTT contient des accents français, vérifie que ton parsing et ton écriture fichier sont en UTF-8 strict (pas latin-1).
- **Speaker name avec caractères spéciaux** : le tag `<v NAME>` peut contenir des espaces, accents, emojis. Préserve-les tels quels dans le rendu Markdown.
- **Cues sans speaker** : rare mais possible si Whisper rate la diarisation. Utilise `???` comme speaker placeholder dans ce cas.
- **VTT vide** : un recording sans paroles produit un VTT avec juste l'en-tête. Préviens l'utilisateur, n'écris rien dans le wiki.
- **`raw/` est immuable** (cf. SCHEMA.md llm-wiki) : ne réédite jamais un transcript après l'avoir écrit. Les corrections vivent dans des pages wiki séparées (`entities/`, `concepts/`).

## Verification

Après exécution, vérifie :

1. Le fichier `$WIKI_PATH/raw/transcripts/<filename>.md` existe et contient le frontmatter + le corps
2. `git log -1 --oneline` dans `$WIKI_PATH` montre ton commit
3. `git push` a réussi (pas de message d'erreur)
4. Tu as réagi ✅ au message d'origine dans Discord
5. Le message de confirmation est posté dans le canal

Si l'un de ces points échoue, signale-le clairement à l'utilisateur plutôt que de prétendre que ça a marché.

## Notes

- **Auth Craig** : le couple `(recording_id, key)` est la seule auth requise. Pas de cookie, pas d'OAuth, pas de header `Authorization`. La key est dans la query string, point.
- **Coût** : ~0€ par transcription côté API Craig (inclus dans l'abo Tier 3 Patreon de Jérôme, $8/mois fixe).
- **Latence Runpod** : environ 1:1 audio:transcription. Pour une réunion de 1h, prévois ~1h de wait. Le job continue côté Craig même si tu te déconnectes — tu peux re-poll après n'importe quel délai (dans les 7 jours de rétention free, 14 jours en Tier 3).
- **Format alternatifs** : Craig propose aussi SubRip (.srt) et Plain Text (.txt). On a choisi WebVTT car les speakers sont structurés via le tag standard W3C `<v NAME>`, parsing fiable.

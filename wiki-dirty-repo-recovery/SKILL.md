---
name: wiki-dirty-repo-recovery
description: "Recovers a wiki/KB repo when a workflow (wiki-attach, wiki-quick, craig-pipeline, llm-wiki ingest) fails because the working tree was already dirty before the ingest started. Handles `git pull --rebase` refusing unstaged changes, stash/pop conflicts in `index.md` / `log.md`, and partial leftover ingests. Use when you see errors like `git-pull-failed`, `cannot pull with rebase: You have unstaged changes`, or post-stash-pop merge conflicts."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [wiki, kb, git, recovery, conflict, ingest]
    category: research
    related_skills: [wiki-attach, wiki-quick, llm-wiki]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: WIKI_PATH
    prompt: "Chemin absolu vers le vault llm-wiki (ex. /opt/data/wiki)."
    required_for: full functionality
---

# wiki-dirty-repo-recovery

## When to Use

A wiki/KB workflow has failed mid-flight because `$WIKI_PATH` is not clean. Common symptoms :

- `git-pull-failed` / `cannot pull with rebase: You have unstaged changes`
- `git stash pop` left unmerged conflicts in `index.md` or `log.md`
- A previous ingest was interrupted and left half-written `raw/` files alongside untracked `entities/` / `concepts/` pages
- `wiki-attach` step 3 reported a dirty tree you don't fully understand

**Contre-indications** :

- Working tree clean → tu n'as pas besoin de ce skill, lance directement l'ingest.
- Branche divergée d'origin avec commits remote inconnus → c'est un autre cas (`git fetch && git log HEAD..origin/main`) ; ce skill ne couvre que le state local.

## Procedure

### 1. Inspect repo state first

```bash
cd "$WIKI_PATH"
git status --short --branch
git log --oneline origin/main..HEAD 2>/dev/null   # commits locaux non pushés
```

Identifie :

- Les fichiers modifiés/untracked qui appartiennent à un **ingest en cours** (legit, à preserver).
- Les fichiers qui ressemblent à du **state runtime** (`.craig-debriefs-pending/`, temp files, etc.) → ignorables.
- Les fichiers complètement inconnus → surface à l'utilisateur, **ne masque rien en commitant tout**.

### 2. Ne re-lance pas l'action bloquée à l'aveugle

Re-tenter `wiki-quick` / `wiki-attach` / `git pull --rebase` sans nettoyer le state échouera identique. **Ne boucle pas** sur la même commande.

### 3. Parker les changements existants si à preserver

Deux options selon la nature du WIP :

- **Commit propre** : si les changements forment un ensemble cohérent et prêt → `git add <files> && git commit -m "<message descriptif>"`. **`git add` sélectif**, pas `git add .` (cf. wiki-attach).
- **Stash temporaire** : si tu veux juste débloquer l'ingest en cours → `git stash push -u -m "<nom-descriptif>"`. Le `-u` stashe aussi les untracked. **Note** : les conflicts au `pop` sont fréquents sur `index.md` et `log.md`.

### 4. Lancer l'action bloquée

Une fois la working tree clean (`git status --short` vide), re-run le `wiki-attach` / `wiki-quick` / ingest qui avait échoué.

### 5. Restaurer les changements parkés (si stash)

```bash
git stash pop
```

**Attends-toi à des conflicts**, particulièrement sur `index.md` et `log.md` — ces deux fichiers sont append-only par convention, et le WIP + le nouvel ingest auront chacun ajouté leurs entrées.

### 6. Résoudre les conflicts explicitement

Pour chaque fichier en conflit :

- Re-lis le fichier avec `read_file`.
- Garde les contributions des **deux** côtés quand elles concernent des actions différentes.
- Pour `index.md` :
  - Recompute le compteur de pages si présent.
  - Garde **toutes** les entrées valides (les deux côtés).
- Pour `log.md` :
  - Préserve l'ordre chronologique.
  - Les deux entrées (WIP + ingest) coexistent généralement sans conflit logique.

Après résolution :

```bash
git add <conflicted-files>
git commit -m "reconcile: post-stash-pop merge"
```

### 7. Verify final state

```bash
git status --short      # doit être vide
git log --oneline -5    # doit montrer le commit du WIP + le commit de l'ingest + le merge
git push                # si commits non pushés
```

## Pitfalls

- **Ne pas écraser `index.md` / `log.md` depuis un seul côté** sans vérifier ce que l'autre a ajouté. Les deux fichiers sont append-only par design.
- **Le stash n'est pas safe tant que le pop n'est pas résolu et commité**. Tant que `git stash list` montre l'entrée, le state est fragile.
- **Si le repo contient des fichiers raw inconnus** (`raw/papers/*.pdf` orphelins, transcripts partiels) → garde-les visibles dans `git status` et demande à l'utilisateur s'ils doivent être conservés avant de les inclure dans le commit de reconciliation.
- **Pas de `git reset --hard`** : tu perdrais les changements légitimes. Si vraiment besoin, fais d'abord un `git stash` ou `git diff > /tmp/backup.patch`.

## Heuristic

Si l'action bloquée est petite (un seul ingest court) mais les changements parkés sont substantiels :

1. **Finis et commit le WIP existant en premier** (étape 3 option commit).
2. **Puis lance le nouvel ingest** sur un tree propre.
3. Pas de stash, pas de pop, pas de conflict.

L'inverse — stash → ingest → pop avec conflicts — coûte plus cher dès que le WIP touche `index.md` / `log.md`.

## Verification

- `git status --short` retourne vide.
- `git log --oneline -10` montre une chronologie cohérente : WIP committé OU ingest committé + reconciliation au-dessus.
- Aucune entrée dans `git stash list`.
- `git push` est passé sans `non-fast-forward`.

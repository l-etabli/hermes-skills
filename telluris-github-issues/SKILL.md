---
name: telluris-github-issues
description: "Lit, recherche, crée et gère les issues du dépôt produit telluris-ai/telluris avec la GitHub App Hermes. Use when a Telluris user asks to inspect, deduplicate, create, edit, comment, close, or reopen a GitHub issue, including validated Craig debrief actions."
version: 0.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [github, issues, telluris, product, craig]
    category: development
    related_skills: [meeting-debrief]
    requires_toolsets: [terminal]
required_environment_variables: []
---

# Telluris GitHub Issues

## When to Use

Utilise ce skill pour toute opération sur les issues de
`telluris-ai/telluris`, notamment après validation d’une action
`github-issue` dans un débrief Craig.

## Procedure

CLI canonique :

```bash
ISSUES=/opt/data/skills-shared/telluris-github-issues/github_issues.py
```

1. Recherche toujours un doublon avant de créer :

   ```bash
   uv run "$ISSUES" search --query "termes distinctifs"
   ```

2. Si une issue existante couvre l’action, retourne son URL. Commente-la
   seulement si l’utilisateur a demandé d’ajouter le nouveau contexte.

3. Sinon, écris le body Markdown dans un fichier temporaire puis crée :

   ```bash
   uv run "$ISSUES" create \
     --title "Titre concis et actionnable" \
     --body-file /tmp/telluris-issue.md \
     --labels "bug"
   ```

4. Pour gérer une issue existante :

   ```bash
   uv run "$ISSUES" view 42
   uv run "$ISSUES" update 42 --title "Nouveau titre" \
     --body-file /tmp/telluris-issue.md --labels "bug,validated"
   uv run "$ISSUES" comment 42 --body-file /tmp/telluris-comment.md
   uv run "$ISSUES" close 42
   uv run "$ISSUES" reopen 42
   ```

5. Lis la sortie JSON. Un succès contient toujours une URL GitHub à reporter
   à l’utilisateur ou dans le thread Craig.

## Pitfalls

- Le dépôt est verrouillé dans le script. Ne tente jamais un autre repo.
- N’utilise ni `gh`, ni `curl`, ni `GITHUB_TOKEN` directement. Le script lit
  le token éphémère rafraîchi dans `/opt/data/.env` sans l’exposer au sandbox.
- Ne mets pas un body Markdown long sur la ligne de commande : passe un
  `--body-file`.
- En cas de `status: error`, surface `reason` et `detail`. Ne contourne pas le
  script et ne prétends jamais qu’une issue a été créée sans URL retournée.
- Ne termine jamais un tour sur « je vais créer l’issue ». Enchaîne
  immédiatement avec le tool call ; confirme seulement après succès.

## Verification

- `search` a été exécuté avant `create`.
- La sortie référence uniquement `telluris-ai/telluris`.
- L’URL retournée s’ouvre sur l’issue attendue.

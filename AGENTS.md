# AGENTS.md

Notes pour un LLM qui code dans ce repo. Lis-les avant d'editer un skill ou d'en ajouter un nouveau.

## Pieges runtime hermes-agent (a connaitre AVANT de debug une skill qui plante en prod)

### 1. Sandbox `execute_code` / `terminal` strip les env vars

Hermes strip par defaut les secrets du subprocess env quand le LLM invoque `execute_code` ou `terminal` (cf. `/opt/hermes/tools/env_passthrough.py` upstream). Donc une var dans le `os.environ` du gateway n'est PAS automatiquement visible pour les scripts Python que tes skills lancent via ces tools.

**Le mecanisme upstream officiel `required_environment_variables` dans le frontmatter SKILL.md ne suffit pas en pratique** — observe en e2e le 2026-04-26 : meme apres un `skill_view: craig-watch` (qui declare `GROQ_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `DISCORD_BOT_TOKEN`...), le LLM voyait `printenv | grep GROQ_API_KEY` retourner vide. La registration est `ContextVar` (session-scoped) et/ou ne s'applique pas a tous les subprocesses.

**Workaround** : declarer explicitement les vars dont ton skill a besoin dans le `terminal.env_passthrough` du `instances/<inst>/config.yaml` cote `hermes-infra` (process-wide, no ordering dep). Garde quand meme le `required_environment_variables` dans le frontmatter SKILL.md — c'est la doc canonique de ce dont le skill a besoin pour qu'un humain ou un autre LLM puisse provisionner correctement.

A reviser si une fix upstream debloque `required_environment_variables`.

### 2. UID 10000 trap sur `docker exec`

Le process agent tourne en `hermes:hermes` UID 10000, mais `docker exec <name> <cmd>` execute par defaut en `root`. Tout fichier ou directory cree par une commande lancee en root via `docker exec` sera owned by root et **invisible/illisible** pour l'agent runtime.

S'applique notamment a tout test depuis l'host comme `docker exec hermes-<inst> /opt/hermes/.venv/bin/hermes <cmd>` qui ecrit du state (`cron create/remove`, `skill install`, `memory save`, `plan create`, etc.) — ou aux scripts de skill qui ecrivent sous `/opt/data/` quand tu les debug en root pour repro un bug.

**Vu en prod** : un test `hermes cron create` lance en root a laisse `/opt/data/cron/jobs.json` en `root:root 0600` -> le `hermes cron create` du listener craig (lance dans le subprocess de l'agent en UID 10000) echouait silencieusement, et le LLM downstream a halluciné « cron auto-créé » dans Discord.

**Regles** :
- Pour tester depuis l'host : `sudo docker exec -u hermes hermes-<inst> ...` (force UID 10000).
- Pour debug les permissions runtime : demander au bot via Discord (`@bot execute id`), pas `docker exec id` qui repond root.
- Si du state a ete corrompu : `sudo docker exec -u root hermes-<inst> chown hermes:hermes <path>`.

## Anti-patterns a ne pas reproduire (lecons apprises sur la chaine craig-*)

Vus en sessions LLM en prod — refletes deja dans les SKILL.md et les scripts existants, mais a garder en tete avant d'editer ou d'ajouter un skill :

1. **Le LLM hallucine systematiquement les snowflake IDs** (Discord channel/message/user IDs, Drive folder IDs, recording IDs Craig). Solution structurelle : faire le script self-discoverer via API REST ou via le fichier de state, et **supprimer les CLI args** que le LLM pourrait remplir avec des valeurs inventees. Cf. `craig-listener/listener.py:270-283` qui n'a aucun argument, par exemple.
2. **Le LLM ignore les instructions « passe pas cet arg »** dans le SKILL.md — preferer SUPPRIMER l'arg du CLI plutot que documenter qu'il ne faut pas l'utiliser.
3. **Le LLM bypass les scripts en cas d'erreur et invente du state** (ecrit lui-meme un pending JSON, dit qu'il a « force l'enregistrement », etc.). SKILL.md doit dire explicitement « si erreur, surface, ne pas ecrire toi-meme ». Cf. craig-listener/SKILL.md § « Anti-pattern : ne JAMAIS bypass le script ».
4. **Discord Components V2** (Craig) : le content texte est dans `components[].components[].content` recursivement, PAS dans `content` ni `embeds`. Walker recursif obligatoire (`extract_message_text` dans `craig-listener/listener.py`).
5. **Cloudflare devant discord.com** refuse le User-Agent `python-urllib` par defaut (error code 1010). Tous les calls REST Discord doivent inclure le header `User-Agent: DiscordBot (https://github.com/l-etabli/hermes-skills, 1.0)`.
6. **Le LLM hand-roll des ingest llm-wiki en python heredoc** au lieu d'utiliser le skill llm-wiki proprement. Si tu invoques llm-wiki depuis un autre skill, sois explicite dans la procedure : « active le skill llm-wiki et applique son operation ingest, NE PAS reimplementer en Python ».
7. **Le LLM termine son tour apres avoir annonce la suite** ("Je vais maintenant proceder a..."). Vu en prod 2026-04-27 sur craig-transcript-record : scan.py a ecrit le transcript, le LLM a poste un message "C'est passe ! Je vais maintenant 1) commit, 2) ingest, 3) debrief" puis a termine son tour — runtime considere la reponse texte comme finale, attend l'utilisateur, et le pipeline reste en plan (transcript ecrit mais pas commit ni ingere). Tout SKILL.md decrivant un pipeline multi-step doit l'interdire explicitement : **"Ne jamais terminer un tour sur un message d'annonce ('Je vais maintenant...', 'On enchaine avec...'). Chaque etape doit etre suivie immediatement par le tool call qui l'execute. Le seul message texte autorise en fin de tour est la confirmation finale, apres que toutes les etapes ont reussi."**

## Workflow de deploy

- **Push sur `main`** = pas de deploy auto. Le re-deploy est volontairement explicite.
- **Pour propager une modif sur le VPS** : sur le serveur, `sudo /usr/local/sbin/hermes-skills-pull <inst>`. Pas besoin de container restart (les scripts sont `uv run` per-call, SKILL.md lu on-demand).
- **Pour tester un skill en local sans deploy** : execute le `.py` du skill directement en CLI avec les bonnes env vars (cf. PEP-723 `# /// script` header pour les deps, `uv run <skill>/<script>.py` resoud les deps automatiquement).
- **Modif d'env var ou de config Hermes** : se fait dans `hermes-infra/instances/<inst>/config.yaml` (autre repo). Push la-bas declenche un CI deploy + container restart.

## Ajouter un skill

Cf. `README.md` § « Anatomie d'un SKILL.md » et « Comment Hermes utilise ces skills ». TL;DR :

1. Cree `<skill-name>/SKILL.md` avec frontmatter YAML (`name`, `description`, `version`, `platforms`, `metadata.hermes`, `required_environment_variables`).
2. Si le skill a un script Python : utilise PEP-723 `# /// script` header pour declarer les deps inline (pas de `requirements.txt` ni `pyproject.toml` par skill).
3. Documente la procedure step-by-step dans le corps du SKILL.md, en restant explicite sur **quoi NE PAS faire** (le LLM est plus fiable avec des interdictions concretes qu'avec des best-practices abstraites).
4. Push, puis sur le VPS `hermes-skills-pull <inst>`.
5. Si le skill a besoin d'env vars : ajoute-les dans le `terminal.env_passthrough` du `config.yaml` cote `hermes-infra` (cf. § Pieges runtime ci-dessus).

## Verifier upstream avant d'inventer

Pour tout comportement upstream (Hermes, Craig API, Discord API, llm-wiki, agentskills.io), **lire la doc ou le code upstream** avant de repondre. Dire « doc unclear » plutot qu'inventer un mecanisme. Le code upstream Hermes est dans le container : `/opt/hermes/` (gateway, agent, tools, hermes_cli).

## Pointeurs

- `README.md` — pourquoi ce repo, layout, anatomie SKILL.md, comment Hermes utilise les skills
- `<skill>/SKILL.md` — chaque skill documente sa propre procedure et ses env vars
- `hermes-infra` (autre repo) — deploy, config par instance, runtime gotchas detailles

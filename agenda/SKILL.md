---
name: agenda
description: "Agendas de Jérôme : lit ses agendas Google (perso et pro) et crée dans le bon agenda Google les RDV qu'il dicte (« RDV dentiste jeudi 14h », « bloque-moi vendredi matin pour Telluris »). Sert aussi les crons proactifs : brief du matin, rappel avant chaque RDV, alerte conflits. À utiliser dès qu'il parle d'un RDV, d'une réunion, d'un créneau, de son planning, ou demande « j'ai quoi aujourd'hui / demain / cette semaine ? »."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [agenda, calendar, rdv, google-calendar, cron]
    category: productivity
    related_skills: [llm-wiki]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: AGENDA_CALENDARS
    prompt: "JSON : liste des agendas {key, label, project, google, default}. Chaque agenda doit être partagé avec le compte de service de l'instance (« Modifier les événements »)."
    required_for: full functionality
# Pas de GOOGLE_APPLICATION_CREDENTIALS ici : le cron Hermes bloque un skill
# dont une variable requise manque, et le script retombe déjà sur
# /opt/data/google-drive-sa.json (gdrive_enabled côté hermes-infra).
---

# agenda (lecture multi-agendas + RDV dictés)

Un seul script fait tout, et c'est la seule source de vérité :

```bash
uv run /opt/data/skills-shared/agenda/agenda.py <commande> [options]
```

Les dates s'écrivent en ISO, heure de Paris : `2026-11-20T14:00` (ou `2026-11-20` pour une journée entière).

## When to Use

- Jérôme dicte un RDV (« dentiste jeudi 14h », « call avec Paul demain 10h-10h30 », « vacances du 22 au 26 déc. »), même en passant, sans dire « ajoute à mon agenda ».
- Il veut déplacer, renommer ou annuler un RDV qu'il t'avait dicté.
- Il demande son planning : « j'ai quoi demain ? », « je suis libre jeudi aprèm ? », « ma semaine ».
- Un cron t'envoie la sortie de `brief`, `reminders` ou `conflicts` (voir plus bas).

## Agendas : lesquels, et où écrire

```bash
uv run /opt/data/skills-shared/agenda/agenda.py calendars
```

La commande liste chaque agenda : sa `key`, son projet, et lequel est le défaut. **Lance-la avant ta première création de la conversation** : les `key` valides sont celles-là et aucune autre.

Pour choisir l'agenda d'un RDV dicté :
1. Le RDV concerne clairement un projet (Telluris, Piloti…) et un agenda porte ce projet : prends cet agenda.
2. Sinon (perso, santé, famille, ou doute) : prends l'agenda par défaut.
3. Tu n'arrives vraiment pas à trancher entre deux projets : demande, en une question courte.

## Procédure — RDV dicté

1. Résous la date et l'heure à partir de la date du jour. Si l'heure manque (« jeudi, dentiste »), ou si le jour est ambigu (« mardi » alors qu'on est mardi soir), pose UNE question courte et attends la réponse. Sans durée donnée, ne passe ni `--end` ni `--minutes` : 60 min par défaut.
2. Crée :
   ```bash
   uv run /opt/data/skills-shared/agenda/agenda.py create --calendar <key> \
     --title "Dentiste" --start 2026-11-26T14:00 [--end 2026-11-26T14:30 | --minutes 30] \
     [--location "12 rue X"] [--description "…"]
   # journée(s) entière(s) :
   uv run /opt/data/skills-shared/agenda/agenda.py create --calendar perso \
     --title "Vacances" --start 2026-12-22 --all-day --days 5
   ```
3. Réponds en recopiant la ligne renvoyée par le script (elle affiche le jour de la semaine : c'est ce qui permet à Jérôme de repérer une erreur de date), le lien, et l'avertissement « chevauche » s'il y en a un. Une ou deux lignes, pas plus.

Pour modifier ou annuler un RDV que tu as créé :

```bash
uv run /opt/data/skills-shared/agenda/agenda.py mine            # liste + id de chaque RDV créé par Hermes
uv run /opt/data/skills-shared/agenda/agenda.py update --event-id <id copié de mine> [--title …] [--start …] [--end …|--minutes …] [--location …]
uv run /opt/data/skills-shared/agenda/agenda.py delete --event-id <id copié de mine>
```

`update` sans `--end` ni `--minutes` garde la durée d'origine.

## Procédure — consulter le planning

```bash
uv run /opt/data/skills-shared/agenda/agenda.py events [--date 2026-11-26] [--days 7]
```

Pour chaque RDV un peu important, regarde dans le wiki (`$WIKI_PATH`) la page du projet ou de la personne et ajoute une ligne de contexte utile (où en est le sujet, ce qu'il faut préparer). Pas de contexte inventé : si le wiki ne dit rien, n'ajoute rien.

## Crons (tu reçois la sortie du script dans ton prompt)

- **Brief du matin** (`brief`) : reformule pour Discord en restant court. Pour chaque RDV du jour, une ligne de contexte tirée du wiki quand elle existe. Garde telles quelles les sections Conflits, Journées chargées et Agendas illisibles.
- **Rappels** (`reminders`, monitor) : chaque ligne **ajoutée** dans le diff = un RDV qui commence dans 30 min ou moins. Envoie un rappel par ligne ajoutée : heure, titre, lieu, et une ligne de contexte wiki si utile. Les lignes retirées (changement de jour, RDV supprimé) ne demandent aucun message. S'il n'y a aucune ligne ajoutée, ou si l'heure de début est déjà passée, réponds exactement `[SILENT]`.
- **Conflits** (`conflicts`, monitor) : chaque ligne ajoutée = un nouveau chevauchement dans les 7 jours. Signale-le en une phrase et propose de déplacer si l'un des deux RDV a été créé par toi. Aucune ligne ajoutée → `[SILENT]`.
- Une ligne `AGENDA ILLISIBLE | <key>` ajoutée : signale-la une fois (« je ne lis plus l'agenda X »), sans chercher à la réparer.

## Interdits

- **N'invente jamais une `key` d'agenda ni un id d'événement.** Les keys viennent de `calendars`, les ids de `mine`. Si l'id voulu n'apparaît pas dans `mine`, c'est que tu n'as pas créé ce RDV.
- **Ne touche jamais un RDV que tu n'as pas créé** : le script le refuse, et tu ne contournes pas ce refus (pas d'appel direct à l'API Google, pas de « je supprime et je recrée »). Dis à Jérôme de le modifier lui-même.
- **En cas d'erreur, recopie-la telle quelle.** N'affirme jamais qu'un RDV est créé, déplacé ou supprimé sans la ligne de confirmation du script. Pas de contournement (écriture dans le wiki, mémoire, autre agenda) présenté comme équivalent.
- **Pas d'invitations** : le script ne peut pas inviter de participants. Si Jérôme veut inviter quelqu'un, crée le RDV et dis-lui d'ajouter l'invité depuis Google Agenda. Une fois qu'un RDV a des invités, le script refuse de le modifier ou de le supprimer (les invités ne seraient pas prévenus) : renvoie Jérôme vers Google Agenda.
- **Ne termine jamais un tour sur une annonce** (« Je vais maintenant créer… »). Chaque étape est suivie immédiatement du tool call qui l'exécute. Le seul message texte de fin est la confirmation (ou la question de clarification).
- **Ne réimplémente rien en Python** : pas de heredoc qui appelle l'API Google. Tout passe par `agenda.py`.

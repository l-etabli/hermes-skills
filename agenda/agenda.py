#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "google-auth"]
# ///
"""
agenda : lit les agendas Google de Jérôme partagés avec le compte de service
de l'instance, et y écrit les RDV qu'il dicte. Le compte de service ne voit que
ces agendas : ni mails, ni Drive, ni autre agenda.

Configuration : $AGENDA_CALENDARS (JSON).
    [
      {"key": "perso", "label": "Perso", "project": "Perso",
       "google": "jerome@gmail.com", "default": true},
      {"key": "telluris", "label": "Telluris", "project": "Telluris",
       "google": "jerome@telluris.example"}
    ]

Usage :
    uv run agenda.py calendars
    uv run agenda.py events [--date YYYY-MM-DD] [--days N]
    uv run agenda.py brief        # cron du matin (--script)
    uv run agenda.py reminders    # cron monitor : RDV du jour dont le rappel est dû
    uv run agenda.py conflicts    # cron monitor : chevauchements des 7 prochains jours
    uv run agenda.py create --calendar KEY --title T --start ISO [--end ISO | --minutes N]
    uv run agenda.py create --calendar KEY --title T --start YYYY-MM-DD --all-day [--days N]
    uv run agenda.py mine [--days N]
    uv run agenda.py update --event-id KEY:ID [--title T] [--start ISO] [--end ISO | --minutes N] ...
    uv run agenda.py delete --event-id KEY:ID

Le script ne modifie ni ne supprime JAMAIS un événement qu'il n'a pas créé
(marque privée HERMES_TAG posée à la création).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

HERMES_TAG = "hermesAgenda"
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar"
DEFAULT_SA_PATH = "/opt/data/google-drive-sa.json"
TZ = ZoneInfo(os.environ.get("AGENDA_TZ") or "Europe/Paris")
REMINDER_MINUTES = int(os.environ.get("AGENDA_REMINDER_MINUTES") or 30)
OVERLOAD_HOURS = float(os.environ.get("AGENDA_OVERLOAD_HOURS") or 6)
CONFLICT_DAYS = 7
HTTP_TIMEOUT = 20

WEEKDAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
WEEKDAYS_LONG = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]


class AgendaError(Exception):
    """Erreur à remonter telle quelle à l'utilisateur."""


# ─── Configuration ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Calendar:
    key: str
    label: str
    project: str
    google_id: str
    default: bool = False


def parse_config(raw: str | None) -> list[Calendar]:
    if not raw or not raw.strip():
        raise AgendaError("AGENDA_CALENDARS est vide : aucun agenda configuré.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AgendaError(f"AGENDA_CALENDARS n'est pas du JSON valide ({error.msg}).") from None
    if not isinstance(data, list) or not data:
        raise AgendaError("AGENDA_CALENDARS doit être une liste non vide.")
    calendars: list[Calendar] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise AgendaError(f"AGENDA_CALENDARS[{index}] doit être un objet.")
        key = str(item.get("key") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
            raise AgendaError(f"AGENDA_CALENDARS[{index}] : key invalide (kebab-case attendu).")
        google_id = str(item.get("google") or "").strip()
        if not google_id:
            raise AgendaError(f"Agenda {key} : `google` (id de l'agenda) manquant.")
        calendars.append(Calendar(
            key=key,
            label=str(item.get("label") or key),
            project=str(item.get("project") or item.get("label") or key),
            google_id=google_id,
            default=bool(item.get("default")),
        ))
    keys = [c.key for c in calendars]
    if len(set(keys)) != len(keys):
        raise AgendaError("AGENDA_CALENDARS : chaque key doit être unique.")
    defaults = [c for c in calendars if c.default]
    if len(defaults) > 1:
        raise AgendaError("AGENDA_CALENDARS : un seul agenda peut avoir default=true.")
    return calendars


def load_config() -> list[Calendar]:
    return parse_config(os.environ.get("AGENDA_CALENDARS"))


def writable_calendar(calendars: list[Calendar], key: str) -> Calendar:
    for calendar in calendars:
        if calendar.key == key:
            return calendar
    choices = ", ".join(c.key for c in calendars)
    raise AgendaError(f"Agenda « {key} » inconnu. Agendas configurés : {choices}.")


# ─── Modèle d'événement ──────────────────────────────────────────────────────

@dataclass
class Event:
    calendar: str
    project: str
    uid: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    busy: bool = True
    hermes: bool = False
    link: str = ""
    also_in: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.calendar}:{self.uid}"


def local(value: datetime) -> datetime:
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, TZ)


def parse_moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise AgendaError(f"Date/heure illisible : « {value} » (format attendu : 2026-11-20T10:00).") from None
    return local(parsed)


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise AgendaError(f"Date illisible : « {value} » (format attendu : 2026-11-20).") from None


# ─── Google ──────────────────────────────────────────────────────────────────

class GoogleClient:
    """Calendar API v3 avec le compte de service de l'instance (lazy : pas de
    dépendance google-auth tant qu'aucun agenda Google n'est touché)."""

    def __init__(self) -> None:
        self._credentials = None
        self._session = None

    def _headers(self) -> dict[str, str]:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._credentials is None:
            path = (os.environ.get("AGENDA_GOOGLE_CREDENTIALS")
                    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                    or DEFAULT_SA_PATH)
            if not os.path.isfile(path):
                raise AgendaError("Compte de service Google introuvable sur l'instance.")
            self._credentials = service_account.Credentials.from_service_account_file(
                path, scopes=[GOOGLE_SCOPE])
        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return {"Authorization": f"Bearer {self._credentials.token}"}

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None) -> dict[str, Any]:
        import requests

        if self._session is None:
            self._session = requests.Session()
        response = self._session.request(
            method, f"{GOOGLE_API}{path}", params=params, json=body,
            headers=self._headers(), timeout=HTTP_TIMEOUT)
        if response.status_code == 204:
            return {}
        if response.status_code >= 400:
            try:
                message = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = response.reason
            if response.status_code in (403, 404):
                message += (" — l'agenda est-il partagé avec le compte de service "
                            "(droit « Modifier les événements ») ?")
            raise AgendaError(f"Google Calendar {response.status_code} : {message}")
        return response.json()


def google_events_path(calendar: Calendar) -> str:
    return f"/calendars/{quote(calendar.google_id, safe='')}/events"


def event_from_google(calendar: Calendar, item: dict[str, Any]) -> Event | None:
    if item.get("status") == "cancelled":
        return None
    owner = calendar.google_id.casefold()
    for attendee in item.get("attendees") or []:
        if (attendee.get("email") or "").casefold() == owner and attendee.get("responseStatus") == "declined":
            return None
    start_raw, end_raw = item.get("start") or {}, item.get("end") or {}
    if "date" in start_raw:
        start = day_start(date.fromisoformat(start_raw["date"]))
        end = day_start(date.fromisoformat(end_raw.get("date") or start_raw["date"]))
        if end <= start:
            end = start + timedelta(days=1)
        all_day = True
    elif "dateTime" in start_raw:
        start = local(datetime.fromisoformat(start_raw["dateTime"]))
        end = local(datetime.fromisoformat(end_raw.get("dateTime") or start_raw["dateTime"]))
        all_day = False
    else:
        return None
    private = (item.get("extendedProperties") or {}).get("private") or {}
    return Event(
        calendar=calendar.key,
        project=calendar.project,
        uid=str(item.get("id") or ""),
        title=(item.get("summary") or "(sans titre)").strip(),
        start=start,
        end=end,
        all_day=all_day,
        location=(item.get("location") or "").strip(),
        busy=item.get("transparency") != "transparent",
        hermes=private.get(HERMES_TAG) == "1",
        link=item.get("htmlLink") or "",
    )


def fetch_google(client: GoogleClient, calendar: Calendar, start: datetime, end: datetime,
                 *, hermes_only: bool = False) -> list[Event]:
    params: dict[str, Any] = {
        "timeMin": start.isoformat(), "timeMax": end.isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 2500,
    }
    if hermes_only:
        params["privateExtendedProperty"] = f"{HERMES_TAG}=1"
    events: list[Event] = []
    while True:
        page = client.request("GET", google_events_path(calendar), params=params)
        for item in page.get("items") or []:
            event = event_from_google(calendar, item)
            if event is not None:
                events.append(event)
        token = page.get("nextPageToken")
        if not token:
            return events
        params["pageToken"] = token


# ─── Agrégation ──────────────────────────────────────────────────────────────

def collect(calendars: list[Calendar], start: datetime, end: datetime,
            client: GoogleClient | None = None) -> tuple[list[Event], dict[str, str]]:
    """Événements de tous les agendas + erreurs par agenda (un agenda en panne
    ne bloque pas les autres)."""
    client = client or GoogleClient()
    events: list[Event] = []
    errors: dict[str, str] = {}
    for calendar in calendars:
        try:
            events.extend(fetch_google(client, calendar, start, end))
        except AgendaError as error:
            errors[calendar.key] = str(error)
        except Exception as error:  # noqa: BLE001 — un agenda cassé ne doit pas tout casser
            errors[calendar.key] = f"{type(error).__name__}"
    return dedupe(events), errors


def dedupe(events: Iterable[Event]) -> list[Event]:
    """Une invitation présente dans deux agendas n'apparaît qu'une fois."""
    kept: dict[tuple, Event] = {}
    for event in sorted(events, key=lambda e: (e.start, e.end, e.calendar)):
        signature = (event.title.casefold(), event.start, event.end)
        if signature in kept:
            if event.calendar not in kept[signature].also_in and event.calendar != kept[signature].calendar:
                kept[signature].also_in.append(event.calendar)
            continue
        kept[signature] = event
    return list(kept.values())


def conflicts(events: Iterable[Event]) -> list[tuple[Event, Event]]:
    timed = sorted((e for e in events if not e.all_day and e.busy and e.end > e.start),
                   key=lambda e: (e.start, e.end))
    pairs: list[tuple[Event, Event]] = []
    for index, first in enumerate(timed):
        for second in timed[index + 1:]:
            if second.start >= first.end:
                break
            pairs.append((first, second))
    return pairs


def overloaded_days(events: Iterable[Event], threshold_hours: float = OVERLOAD_HOURS) -> list[tuple[date, float, int]]:
    per_day: dict[date, list[Event]] = {}
    for event in events:
        if event.all_day or not event.busy:
            continue
        per_day.setdefault(event.start.date(), []).append(event)
    result = []
    for day, day_events in sorted(per_day.items()):
        # Union des intervalles : deux RDV qui se chevauchent ne comptent pas double.
        busy = 0.0
        cursor: datetime | None = None
        for event in sorted(day_events, key=lambda e: e.start):
            begin = event.start if cursor is None else max(event.start, cursor)
            if event.end > begin:
                busy += (event.end - begin).total_seconds()
            cursor = event.end if cursor is None else max(cursor, event.end)
        hours = busy / 3600
        if hours > threshold_hours:
            result.append((day, hours, len(day_events)))
    return result


# ─── Formatage ───────────────────────────────────────────────────────────────

def fmt_day(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day:02d}/{day.month:02d}"


def fmt_day_long(day: date) -> str:
    return f"{WEEKDAYS_LONG[day.weekday()]} {day.day} {MONTHS[day.month - 1]} {day.year}"


def fmt_hours(hours: float) -> str:
    whole = int(hours)
    minutes = round((hours - whole) * 60)
    return f"{whole} h {minutes:02d}" if minutes else f"{whole} h"


def fmt_slot(event: Event) -> str:
    if event.all_day:
        return "journée entière"
    end = event.end.strftime("%H:%M")
    if event.end.date() != event.start.date():
        end = f"{fmt_day(event.end.date())} {end}"
    return f"{event.start:%H:%M}–{end}"


def fmt_event(event: Event, *, with_day: bool = False, with_ref: bool = False) -> str:
    when = f"{fmt_day(event.start.date())} {fmt_slot(event)}" if with_day else fmt_slot(event)
    line = f"- {when} · {event.title} [{event.project}]"
    if event.location:
        line += f" · {event.location}"
    if not event.busy:
        line += " · (disponible)"
    if with_ref:
        line += f" · id: {event.ref}"
    return line


def fmt_errors(errors: dict[str, str]) -> list[str]:
    if not errors:
        return []
    return ["", "Agendas illisibles (à signaler) :"] + [f"- {key} : {message}" for key, message in sorted(errors.items())]


# ─── Commandes ───────────────────────────────────────────────────────────────

def cmd_calendars(calendars: list[Calendar], client: GoogleClient) -> str:
    today = day_start(datetime.now(TZ).date())
    lines = ["Agendas configurés :"]
    for calendar in calendars:
        flag = " (défaut)" if calendar.default else ""
        try:
            count = len(fetch_google(client, calendar, today, today + timedelta(days=7)))
            status = f"OK, {count} RDV sur 7 jours"
        except AgendaError as error:
            status = f"ERREUR : {error}"
        lines.append(f"- {calendar.key}{flag} · {calendar.label} · projet {calendar.project} · {status}")
    return "\n".join(lines)


def cmd_events(calendars: list[Calendar], first_day: date, days: int, client: GoogleClient) -> str:
    start = day_start(first_day)
    events, errors = collect(calendars, start, start + timedelta(days=days), client)
    lines: list[str] = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        day_events = [e for e in events if e.start.date() <= day < (e.end - timedelta(microseconds=1)).date() + timedelta(days=1)]
        lines.append(f"{fmt_day(day)} :")
        lines.extend(fmt_event(e) for e in day_events) if day_events else lines.append("- rien")
    found = conflicts(events)
    if found:
        lines += ["", "Conflits :"] + [fmt_conflict(a, b) for a, b in found]
    return "\n".join(lines + fmt_errors(errors))


def fmt_conflict(first: Event, second: Event) -> str:
    return (f"- {fmt_day(first.start.date())} : « {first.title} » [{first.project}] {fmt_slot(first)} "
            f"chevauche « {second.title} » [{second.project}] {fmt_slot(second)}")


def build_brief(events: list[Event], errors: dict[str, str], now: datetime) -> str:
    today = now.date()
    tomorrow = today + timedelta(days=1)
    today_events = [e for e in events if e.start.date() <= today < e.end.date() or e.start.date() == today]
    lines = [f"Agenda du {fmt_day_long(today)}", "", "Aujourd'hui :"]
    lines += [fmt_event(e) for e in today_events] or ["- aucun RDV"]
    week_conflicts = conflicts(events)
    if week_conflicts:
        lines += ["", f"Conflits ({CONFLICT_DAYS} prochains jours) :"] + [fmt_conflict(a, b) for a, b in week_conflicts]
    heavy = overloaded_days(events)
    if heavy:
        lines += ["", f"Journées chargées (plus de {fmt_hours(OVERLOAD_HOURS)} de RDV) :"]
        lines += [f"- {fmt_day(day)} : {fmt_hours(hours)} sur {count} RDV" for day, hours, count in heavy]
    next_day = [e for e in events if e.start.date() == tomorrow and not e.all_day]
    if next_day:
        lines += ["", f"Demain : {len(next_day)} RDV, premier à {next_day[0].start:%H:%M} ({next_day[0].title})."]
    return "\n".join(lines + fmt_errors(errors))


def reminder_lines(events: list[Event], errors: dict[str, str], now: datetime,
                   lead_minutes: int = REMINDER_MINUTES) -> str:
    """Sortie STABLE pour --monitor-script : RDV du jour dont l'heure de rappel
    est passée. L'ensemble ne fait que grossir dans la journée, donc chaque
    nouvelle ligne du diff = un rappel à envoyer ; il se vide à minuit (diff
    sans ajout → l'agent répond [SILENT])."""
    lead = timedelta(minutes=lead_minutes)
    lines = [
        f"{e.start:%H:%M}–{e.end:%H:%M} | {e.title} | {e.project} | {e.location or '-'} | {e.ref}"
        for e in sorted(events, key=lambda e: (e.start, e.title))
        if not e.all_day and e.start.date() == now.date() and e.start - lead <= now
    ]
    lines += [f"AGENDA ILLISIBLE | {key}" for key in sorted(errors)]
    return "\n".join(lines)


def conflict_lines(events: list[Event], errors: dict[str, str]) -> str:
    """Sortie STABLE pour --monitor-script : une ligne par chevauchement."""
    lines = [
        f"{fmt_day(a.start.date())} | {a.title} [{a.project}] {fmt_slot(a)} | {b.title} [{b.project}] {fmt_slot(b)}"
        for a, b in conflicts(events)
    ]
    lines += [f"AGENDA ILLISIBLE | {key}" for key in sorted(errors)]
    return "\n".join(lines)


def build_event_body(title: str, start: datetime | date, end: datetime | date,
                     location: str | None, description: str | None) -> dict[str, Any]:
    if isinstance(start, datetime):
        start_field = {"dateTime": start.isoformat(), "timeZone": str(TZ)}
        end_field = {"dateTime": end.isoformat(), "timeZone": str(TZ)}  # type: ignore[union-attr]
    else:
        start_field, end_field = {"date": start.isoformat()}, {"date": end.isoformat()}
    body: dict[str, Any] = {
        "summary": title.strip(),
        "start": start_field,
        "end": end_field,
        "extendedProperties": {"private": {HERMES_TAG: "1"}},
        "reminders": {"useDefault": True},
    }
    if location:
        body["location"] = location.strip()
    if description:
        body["description"] = description.strip()
    return body


def resolve_times(start_raw: str, end_raw: str | None, minutes: int | None,
                  all_day: bool, days: int | None) -> tuple[datetime | date, datetime | date]:
    if all_day:
        first = parse_day(start_raw)
        return first, first + timedelta(days=max(days or 1, 1))
    start = parse_moment(start_raw)
    if end_raw:
        end = parse_moment(end_raw)
    else:
        end = start + timedelta(minutes=minutes or 60)
    if end <= start:
        raise AgendaError("La fin doit être après le début.")
    return start, end


def overlaps_with(new: Event, calendars: list[Calendar], client: GoogleClient) -> list[str]:
    if new.all_day:
        return []
    others, _ = collect(calendars, day_start(new.start.date()), day_start(new.start.date()) + timedelta(days=1), client)
    return [fmt_event(e) for e in others
            if e.uid != new.uid and not e.all_day and e.busy and e.start < new.end and new.start < e.end]


def cmd_create(calendars: list[Calendar], args: argparse.Namespace, client: GoogleClient) -> str:
    calendar = writable_calendar(calendars, args.calendar)
    start, end = resolve_times(args.start, args.end, args.minutes, args.all_day, args.days)
    body = build_event_body(args.title, start, end, args.location, args.description)
    created = client.request("POST", google_events_path(calendar), body=body)
    event = event_from_google(calendar, created)
    if event is None:
        raise AgendaError("Google a accepté la création mais l'événement est illisible.")
    lines = [f"Créé dans {calendar.label} :", fmt_event(event, with_day=True, with_ref=True)]
    if event.link:
        lines.append(f"Lien : {event.link}")
    clash = overlaps_with(event, calendars, client)
    if clash:
        lines += ["", "Attention, chevauche :"] + clash
    return "\n".join(lines)


def load_owned(calendars: list[Calendar], ref: str, client: GoogleClient) -> tuple[Calendar, dict[str, Any]]:
    key, _, event_id = ref.partition(":")
    if not event_id:
        raise AgendaError("id attendu au format agenda:identifiant (cf. `mine`).")
    calendar = writable_calendar(calendars, key)
    item = client.request("GET", f"{google_events_path(calendar)}/{quote(event_id, safe='')}")
    private = (item.get("extendedProperties") or {}).get("private") or {}
    if private.get(HERMES_TAG) != "1":
        raise AgendaError("Refusé : cet événement n'a pas été créé par Hermes, je n'y touche pas.")
    if has_guests(calendar, item):
        # Les invités ne seraient pas prévenus d'un déplacement ou d'une
        # annulation faite par le compte de service.
        raise AgendaError("Refusé : ce RDV a des invités, modifie-le depuis Google Agenda pour qu'ils soient prévenus.")
    return calendar, item


def has_guests(calendar: Calendar, item: dict[str, Any]) -> bool:
    owner = calendar.google_id.casefold()
    return any((attendee.get("email") or "").casefold() != owner and not attendee.get("organizer")
               for attendee in item.get("attendees") or [])


def cmd_update(calendars: list[Calendar], args: argparse.Namespace, client: GoogleClient) -> str:
    calendar, item = load_owned(calendars, args.event_id, client)
    current = event_from_google(calendar, item)
    if current is None:
        raise AgendaError("Événement annulé ou illisible.")
    patch: dict[str, Any] = {}
    if args.title:
        patch["summary"] = args.title.strip()
    if args.location is not None:
        patch["location"] = args.location.strip()
    if args.description is not None:
        patch["description"] = args.description.strip()
    if args.start or args.end or args.minutes:
        if current.all_day:
            raise AgendaError("Pour déplacer un RDV journée entière : delete puis create.")
        start = parse_moment(args.start) if args.start else current.start
        if args.end:
            end = parse_moment(args.end)
        elif args.minutes:
            end = start + timedelta(minutes=args.minutes)
        else:
            end = start + (current.end - current.start)
        if end <= start:
            raise AgendaError("La fin doit être après le début.")
        patch["start"] = {"dateTime": start.isoformat(), "timeZone": str(TZ)}
        patch["end"] = {"dateTime": end.isoformat(), "timeZone": str(TZ)}
    if not patch:
        raise AgendaError("Rien à modifier.")
    updated = client.request("PATCH", f"{google_events_path(calendar)}/{quote(item['id'], safe='')}", body=patch)
    event = event_from_google(calendar, updated)
    return "Modifié :\n" + (fmt_event(event, with_day=True, with_ref=True) if event else updated.get("id", ""))


def cmd_delete(calendars: list[Calendar], args: argparse.Namespace, client: GoogleClient) -> str:
    calendar, item = load_owned(calendars, args.event_id, client)
    event = event_from_google(calendar, item)
    client.request("DELETE", f"{google_events_path(calendar)}/{quote(item['id'], safe='')}")
    return "Supprimé :\n" + (fmt_event(event, with_day=True) if event else item.get("id", ""))


def cmd_mine(calendars: list[Calendar], days: int, client: GoogleClient) -> str:
    start = day_start(datetime.now(TZ).date() - timedelta(days=1))
    events: list[Event] = []
    for calendar in calendars:
        events.extend(fetch_google(client, calendar, start, start + timedelta(days=days + 1), hermes_only=True))
    events.sort(key=lambda e: e.start)
    if not events:
        return "Aucun RDV créé par Hermes sur la période."
    return "RDV créés par Hermes :\n" + "\n".join(fmt_event(e, with_day=True, with_ref=True) for e in events)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agendas Google de Jérôme (lecture multi-agendas, écriture).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("calendars", help="agendas configurés + test d'accès")
    events = sub.add_parser("events", help="RDV d'une période")
    events.add_argument("--date", help="premier jour (YYYY-MM-DD), défaut aujourd'hui")
    events.add_argument("--days", type=int, default=1)
    sub.add_parser("brief", help="brief du matin")
    sub.add_parser("reminders", help="monitor : rappels dus")
    sub.add_parser("conflicts", help="monitor : chevauchements à 7 jours")
    create = sub.add_parser("create", help="créer un RDV")
    create.add_argument("--calendar", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--start", required=True)
    create.add_argument("--end")
    create.add_argument("--minutes", type=int)
    create.add_argument("--all-day", action="store_true")
    create.add_argument("--days", type=int)
    create.add_argument("--location")
    create.add_argument("--description")
    mine = sub.add_parser("mine", help="RDV créés par Hermes")
    mine.add_argument("--days", type=int, default=30)
    update = sub.add_parser("update", help="modifier un RDV créé par Hermes")
    update.add_argument("--event-id", required=True)
    update.add_argument("--title")
    update.add_argument("--start")
    update.add_argument("--end")
    update.add_argument("--minutes", type=int)
    update.add_argument("--location")
    update.add_argument("--description")
    delete = sub.add_parser("delete", help="supprimer un RDV créé par Hermes")
    delete.add_argument("--event-id", required=True)
    return parser


def run(argv: list[str]) -> str:
    args = build_parser().parse_args(argv)
    calendars = load_config()
    client = GoogleClient()
    now = datetime.now(TZ)
    if args.command == "calendars":
        return cmd_calendars(calendars, client)
    if args.command == "events":
        first = parse_day(args.date) if args.date else now.date()
        return cmd_events(calendars, first, max(1, min(args.days, 31)), client)
    if args.command in ("brief", "conflicts"):
        start = day_start(now.date())
        events, errors = collect(calendars, start, start + timedelta(days=CONFLICT_DAYS), client)
        return build_brief(events, errors, now) if args.command == "brief" else conflict_lines(events, errors)
    if args.command == "reminders":
        start = day_start(now.date())
        events, errors = collect(calendars, start, start + timedelta(days=1), client)
        return reminder_lines(events, errors, now)
    if args.command == "create":
        return cmd_create(calendars, args, client)
    if args.command == "mine":
        return cmd_mine(calendars, max(1, min(args.days, 365)), client)
    if args.command == "update":
        return cmd_update(calendars, args, client)
    if args.command == "delete":
        return cmd_delete(calendars, args, client)
    raise AgendaError(f"Commande inconnue : {args.command}")


def main(argv: list[str]) -> int:
    try:
        output = run(argv)
    except AgendaError as error:
        print(f"ERREUR agenda : {error}", file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

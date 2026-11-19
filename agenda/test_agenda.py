"""Tests de l'agrégation d'agendas (fonctions pures + client Google simulé).

    uv run --with pytest --with requests --with google-auth pytest agenda
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
from datetime import date, datetime, timedelta

import pytest

HERE = pathlib.Path(__file__).resolve().parent


@pytest.fixture
def agenda(monkeypatch):
    monkeypatch.setenv("AGENDA_TZ", "Europe/Paris")
    monkeypatch.delenv("AGENDA_REMINDER_MINUTES", raising=False)
    monkeypatch.delenv("AGENDA_OVERLOAD_HOURS", raising=False)
    spec = importlib.util.spec_from_file_location("agenda_under_test", HERE / "agenda.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CONFIG = json.dumps([
    {"key": "perso", "label": "Perso", "project": "Perso", "google": "me@gmail.com", "default": True},
    {"key": "telluris", "label": "Telluris", "project": "Telluris", "google": "me@telluris.example"},
    {"key": "famille", "label": "Famille", "project": "Perso", "google": "family@group.calendar.google.com"},
])


def at(agenda, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=agenda.TZ)


def ev(agenda, title, start, end, calendar="perso", **extra):
    return agenda.Event(calendar=calendar, project=calendar.capitalize(), uid=f"{title}-{start:%H%M}",
                        title=title, start=start, end=end, **extra)


# ─── Config ──────────────────────────────────────────────────────────────────

def test_config_parses_google_calendars(agenda):
    calendars = agenda.parse_config(CONFIG)
    assert [c.key for c in calendars] == ["perso", "telluris", "famille"]
    assert [c.default for c in calendars] == [True, False, False]


@pytest.mark.parametrize("raw, message", [
    ("", "vide"),
    ("{", "JSON"),
    ('[{"key": "a"}]', "manquant"),
    ('[{"key": "a", "google": "x"}, {"key": "a", "google": "y"}]', "unique"),
    ('[{"key": "a", "google": "x", "default": true}, {"key": "b", "google": "y", "default": true}]', "un seul"),
    ('[{"key": "A B", "google": "x"}]', "key invalide"),
])
def test_config_rejects_ambiguous_setups(agenda, raw, message):
    with pytest.raises(agenda.AgendaError, match=message):
        agenda.parse_config(raw)


def test_writing_requires_a_configured_google_calendar(agenda):
    calendars = agenda.parse_config(CONFIG)
    assert agenda.writable_calendar(calendars, "telluris").google_id == "me@telluris.example"
    with pytest.raises(agenda.AgendaError, match="perso, telluris, famille"):
        agenda.writable_calendar(calendars, "me@gmail.com")


# ─── Agrégation ──────────────────────────────────────────────────────────────

def test_dedupe_merges_an_invitation_seen_in_two_calendars(agenda):
    first = ev(agenda, "Point Telluris", at(agenda, 20, 10), at(agenda, 20, 11), "telluris")
    copy = ev(agenda, "point telluris", at(agenda, 20, 10), at(agenda, 20, 11), "perso")
    merged = agenda.dedupe([copy, first])
    assert len(merged) == 1
    assert merged[0].also_in


def test_conflicts_ignore_all_day_free_and_back_to_back_events(agenda):
    events = [
        ev(agenda, "A", at(agenda, 20, 10), at(agenda, 20, 11)),
        ev(agenda, "B", at(agenda, 20, 10, 30), at(agenda, 20, 12), "telluris"),
        ev(agenda, "C", at(agenda, 20, 12), at(agenda, 20, 13)),  # enchaîne, pas de conflit
        ev(agenda, "Dispo", at(agenda, 20, 10), at(agenda, 20, 18), busy=False),
        ev(agenda, "Férié", agenda.day_start(date(2026, 11, 20)), agenda.day_start(date(2026, 11, 21)), all_day=True),
    ]
    assert [(a.title, b.title) for a, b in agenda.conflicts(events)] == [("A", "B")]


def test_overload_counts_overlapping_time_once(agenda):
    events = [
        ev(agenda, "Atelier", at(agenda, 20, 9), at(agenda, 20, 13)),
        ev(agenda, "Doublon", at(agenda, 20, 11), at(agenda, 20, 13)),
        ev(agenda, "Après-midi", at(agenda, 20, 14), at(agenda, 20, 16, 30)),
    ]
    assert agenda.overloaded_days(events, threshold_hours=6) == [(date(2026, 11, 20), 6.5, 3)]
    assert agenda.overloaded_days(events, threshold_hours=7) == []


def test_one_broken_calendar_does_not_hide_the_others(agenda):
    calendars = agenda.parse_config(CONFIG)

    class FakeGoogle:
        def request(self, method, path, params=None, body=None):
            if "telluris" in path:
                raise agenda.AgendaError("Google Calendar 404 : Not Found")
            if "family" in path:
                raise RuntimeError("boom")
            return {"items": [{"id": "x", "summary": "Dentiste",
                               "start": {"dateTime": "2026-11-20T09:00:00+01:00"},
                               "end": {"dateTime": "2026-11-20T09:30:00+01:00"}}]}

    events, errors = agenda.collect(calendars, at(agenda, 20, 0), at(agenda, 21, 0), FakeGoogle())
    assert [e.title for e in events] == ["Dentiste"]
    assert errors == {"telluris": "Google Calendar 404 : Not Found", "famille": "RuntimeError"}


def test_google_parsing_skips_declined_and_cancelled_events(agenda):
    calendar = agenda.parse_config(CONFIG)[0]
    base = {"id": "1", "summary": "X", "start": {"dateTime": "2026-11-20T09:00:00Z"},
            "end": {"dateTime": "2026-11-20T10:00:00Z"}}
    declined = dict(base, attendees=[{"email": "ME@gmail.com", "responseStatus": "declined"}])
    assert agenda.event_from_google(calendar, dict(base, status="cancelled")) is None
    assert agenda.event_from_google(calendar, declined) is None
    parsed = agenda.event_from_google(calendar, dict(base, extendedProperties={"private": {"hermesAgenda": "1"}}))
    assert parsed.start == at(agenda, 20, 10)  # converti en heure de Paris
    assert parsed.hermes


# ─── Sorties des crons ───────────────────────────────────────────────────────

def test_reminders_only_grow_during_the_day(agenda):
    events = [
        ev(agenda, "Dentiste", at(agenda, 20, 9), at(agenda, 20, 9, 30)),
        ev(agenda, "Point Telluris", at(agenda, 20, 14), at(agenda, 20, 15), "telluris"),
        ev(agenda, "Demain", at(agenda, 21, 8), at(agenda, 21, 9)),
    ]
    early = agenda.reminder_lines(events, {}, at(agenda, 20, 8, 25))
    on_time = agenda.reminder_lines(events, {}, at(agenda, 20, 8, 30))
    later = agenda.reminder_lines(events, {}, at(agenda, 20, 13, 35))
    assert early == ""
    assert on_time.splitlines() == ["09:00–09:30 | Dentiste | Perso | - | perso:Dentiste-0900"]
    assert later.splitlines()[0] == on_time  # stable : la ligne déjà rappelée ne bouge pas
    assert len(later.splitlines()) == 2
    assert "Demain" not in later


def test_brief_lists_today_conflicts_and_failures(agenda):
    events = [
        ev(agenda, "A", at(agenda, 20, 10), at(agenda, 20, 11)),
        ev(agenda, "B", at(agenda, 20, 10, 30), at(agenda, 20, 12), "telluris"),
        ev(agenda, "Demain", at(agenda, 21, 8), at(agenda, 21, 9)),
    ]
    brief = agenda.build_brief(events, {"famille": "Google Calendar 404"}, at(agenda, 20, 7, 30))
    assert brief.startswith("Agenda du vendredi 20 novembre 2026")
    assert "- 10:00–11:00 · A [Perso]" in brief
    assert "chevauche" in brief
    assert "Demain : 1 RDV, premier à 08:00" in brief
    assert "famille : Google Calendar 404" in brief


def test_empty_day_brief_is_explicit(agenda):
    brief = agenda.build_brief([], {}, at(agenda, 22, 7, 30))
    assert "- aucun RDV" in brief


# ─── Écriture ────────────────────────────────────────────────────────────────

class RecordingGoogle:
    def __init__(self, stored=None):
        self.calls = []
        self.stored = stored or {}

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            return dict(body, id="new1", htmlLink="https://calendar.google.com/event?eid=new1")
        if method == "GET" and path.endswith("/events"):
            return {"items": []}
        if method == "GET":
            return self.stored
        if method == "PATCH":
            return dict(self.stored, **body)
        return {}


def create_args(**overrides):
    values = dict(calendar="telluris", title="Démo client", start="2026-11-20T15:00", end=None, minutes=None,
                  all_day=False, days=None, location="Visio", description=None)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_create_tags_the_event_and_echoes_the_day(agenda):
    calendars = agenda.parse_config(CONFIG)
    google = RecordingGoogle()
    output = agenda.cmd_create(calendars, create_args(), google)
    method, path, body = google.calls[0]
    assert (method, path) == ("POST", "/calendars/me%40telluris.example/events")
    assert body["extendedProperties"]["private"]["hermesAgenda"] == "1"
    assert body["start"] == {"dateTime": "2026-11-20T15:00:00+01:00", "timeZone": "Europe/Paris"}
    assert body["end"]["dateTime"] == "2026-11-20T16:00:00+01:00"  # 60 min par défaut
    assert "ven. 20/11 15:00–16:00 · Démo client [Telluris] · Visio · id: telluris:new1" in output
    assert "https://calendar.google.com/event?eid=new1" in output


def test_create_all_day_uses_exclusive_end_date(agenda):
    calendars = agenda.parse_config(CONFIG)
    google = RecordingGoogle()
    agenda.cmd_create(calendars, create_args(start="2026-11-24", all_day=True, days=2, calendar="perso"), google)
    body = google.calls[0][2]
    assert body["start"] == {"date": "2026-11-24"}
    assert body["end"] == {"date": "2026-11-26"}


def test_create_refuses_end_before_start(agenda):
    with pytest.raises(agenda.AgendaError, match="après"):
        agenda.resolve_times("2026-11-20T15:00", "2026-11-20T14:00", None, False, None)


def test_update_and_delete_refuse_events_not_created_by_hermes(agenda):
    calendars = agenda.parse_config(CONFIG)
    foreign = {"id": "abc", "summary": "Réunion du board",
               "start": {"dateTime": "2026-11-20T09:00:00+01:00"}, "end": {"dateTime": "2026-11-20T10:00:00+01:00"}}
    google = RecordingGoogle(foreign)
    with pytest.raises(agenda.AgendaError, match="pas été créé par Hermes"):
        agenda.cmd_delete(calendars, argparse.Namespace(event_id="perso:abc"), google)
    assert not [call for call in google.calls if call[0] == "DELETE"]


def test_update_and_delete_refuse_owned_events_once_guests_are_invited(agenda):
    calendars = agenda.parse_config(CONFIG)
    invited = {"id": "abc", "summary": "Déj Paul", "extendedProperties": {"private": {"hermesAgenda": "1"}},
               "attendees": [{"email": "me@gmail.com", "organizer": True}, {"email": "paul@example.com"}],
               "start": {"dateTime": "2026-11-20T12:00:00+01:00"}, "end": {"dateTime": "2026-11-20T13:00:00+01:00"}}
    google = RecordingGoogle(invited)
    with pytest.raises(agenda.AgendaError, match="invités"):
        agenda.cmd_delete(calendars, argparse.Namespace(event_id="perso:abc"), google)
    args = argparse.Namespace(event_id="perso:abc", title="X", start=None, end=None, minutes=None,
                              location=None, description=None)
    with pytest.raises(agenda.AgendaError, match="invités"):
        agenda.cmd_update(calendars, args, google)
    assert [call[0] for call in google.calls] == ["GET", "GET"]


def test_owner_alone_in_attendees_is_not_a_guest(agenda):
    calendar = agenda.parse_config(CONFIG)[0]
    assert not agenda.has_guests(calendar, {"attendees": [{"email": "Me@Gmail.com"}]})
    assert not agenda.has_guests(calendar, {})


def test_update_moves_an_owned_event_keeping_its_duration(agenda):
    calendars = agenda.parse_config(CONFIG)
    owned = {"id": "abc", "summary": "Coiffeur", "extendedProperties": {"private": {"hermesAgenda": "1"}},
             "start": {"dateTime": "2026-11-20T09:00:00+01:00"}, "end": {"dateTime": "2026-11-20T09:45:00+01:00"}}
    google = RecordingGoogle(owned)
    args = argparse.Namespace(event_id="perso:abc", title=None, start="2026-11-21T10:00", end=None, minutes=None,
                              location=None, description=None)
    output = agenda.cmd_update(calendars, args, google)
    patch = google.calls[-1][2]
    assert patch["end"]["dateTime"] == (agenda.parse_moment("2026-11-21T10:00") + timedelta(minutes=45)).isoformat()
    assert "sam. 21/11 10:00–10:45 · Coiffeur" in output

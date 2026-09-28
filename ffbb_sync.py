from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from icalendar import Calendar, Event
from zoneinfo import ZoneInfo


LOG = logging.getLogger("ffbb-sync")

API_BASE = "https://ffbb-api.desimone.fr"


@dataclass
class Match:
    uid: str
    child: str
    phase: str
    match_id: str
    round_name: str
    date: str
    time: str
    home_away: str
    opponent: str
    team: str
    detail_url: str
    venue_name: str = ""
    venue_address: str = ""
    venue_url: str = ""


def slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "child"


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def iter_phases(child_cfg: dict):
    """
    Accepte deux formats de config.yaml :
      - à plat (recommandé) : name / organisme_id / team directement sur l'enfant
      - imbriqué (rétro-compatibilité) : name + une liste "phases", chacune
        avec son propre organisme_id / team
    """
    if "phases" in child_cfg:
        for phase in child_cfg["phases"]:
            yield phase
    else:
        yield {
            "name": child_cfg.get("team") or child_cfg["name"],
            "organisme_id": child_cfg["organisme_id"],
            "team": child_cfg.get("team"),
        }


def fetch_club_matches(organisme_id, team, limit=500, timeout=30):
    """
    Interroge l'API FFBB hébergée (ffbb-api.desimone.fr), qui résout déjà
    côté serveur les rencontres du club, l'équipe correspondante (via le
    filtre texte "team") et l'adresse exacte de la salle.
    """
    url = f"{API_BASE}/api/v1/club/{organisme_id}/matches"
    params = {"limit": limit}
    if team:
        params["team"] = team

    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def build_match(child: str, phase_name: str, raw: dict) -> Match | None:
    match_id = str(raw.get("ffbbMatchId") or "")
    date_iso = raw.get("dateISO") or ""
    time_str = raw.get("time") or ""

    if not match_id or not date_iso or not time_str:
        LOG.warning(
            "Match ignoré pour %s (%s) : données incomplètes (id=%r, date=%r, heure=%r)",
            child, phase_name, match_id, date_iso, time_str
        )
        return None

    is_home = bool(raw.get("isHome"))
    round_num = raw.get("round")
    round_name = f"J{round_num}" if round_num else ""

    uid = f"ffbb-{match_id}-{slug(child)}@basket-calendar"

    return Match(
        uid=uid,
        child=child,
        phase=phase_name,
        match_id=match_id,
        round_name=round_name,
        date=date_iso,
        time=time_str,
        home_away="Domicile" if is_home else "Extérieur",
        opponent=raw.get("opponent") or "",
        team=raw.get("team") or "",
        detail_url=raw.get("competitionUrl") or "",
        venue_name=raw.get("location") or "",
        venue_address="",
        venue_url="",
    )


def build_ics(cfg, matches):
    cal = Calendar()
    cal.add("prodid", "-//FFBB Basket Enfants//FR//")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", cfg["calendar"]["name"])
    cal.add("x-wr-timezone", cfg["calendar"]["timezone"])

    tz_name = cfg["calendar"]["timezone"]
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(
            f"Impossible de charger le fuseau horaire {tz_name!r} : {exc}\n"
            "Si l'erreur mentionne 'No time zone found' ou 'ZoneInfoNotFoundError', "
            "la base de fuseaux horaires du système est absente (fréquent sur Windows "
            "ou certaines images Docker/CI minimalistes). Solution : "
            "ajouter 'tzdata' dans requirements.txt (pip install tzdata)."
        ) from exc
    duration = timedelta(minutes=cfg["calendar"]["duration_minutes"])

    for m in sorted(matches, key=lambda x: (x.date, x.time, x.child)):
        # L'heure vient de l'API FFBB en heure locale (Europe/Paris). On la
        # convertit explicitement en UTC avant de l'écrire dans l'ICS, pour
        # une compatibilité sans ambiguïté avec tous les lecteurs de calendrier.
        start_local = datetime.fromisoformat(f"{m.date}T{m.time}").replace(tzinfo=tzinfo)
        if start_local.utcoffset() is None:
            raise ValueError(
                f"Échec de localisation de la date pour le match #{m.match_id} "
                f"({m.date} {m.time}) : le fuseau horaire n'a pas pu être appliqué."
            )
        end_local = start_local + duration
        start = start_local.astimezone(timezone.utc)
        end = end_local.astimezone(timezone.utc)

        event = Event()
        event.add("uid", m.uid)
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("summary", f"Match basket {m.child}")

        location = ", ".join(x for x in [m.venue_name, m.venue_address] if x)
        if location:
            event.add("location", location)

        description_lines = [
            f"Enfant : {m.child}",
            f"Équipe : {m.team}",
            f"Adversaire : {m.opponent or 'Non détecté'}",
            f"Type : {m.home_away or 'Non précisé'}",
            f"Journée : {m.round_name or 'Non précisée'}",
            f"Phase : {m.phase}",
        ]
        if m.detail_url:
            description_lines.append(f"FFBB : {m.detail_url}")
        if m.venue_url:
            description_lines.append(f"Détail lieu/rencontre : {m.venue_url}")

        event.add("description", "\n".join(description_lines))
        if m.detail_url:
            event.add("url", m.detail_url)
        cal.add_component(event)

    return cal.to_ical()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()

    all_matches = []
    for child_cfg in cfg["children"]:
        child = child_cfg["name"]
        for phase in iter_phases(child_cfg):
            phase_name = phase.get("name") or phase.get("team") or "principal"
            LOG.info("Lecture %s / %s", child, phase_name)
            try:
                data = fetch_club_matches(
                    organisme_id=phase["organisme_id"],
                    team=phase.get("team"),
                )
                raw_matches = data.get("matches", [])
                LOG.info(
                    "%s / %s : %d rencontre(s) reçue(s) de l'API",
                    child, phase_name, len(raw_matches)
                )
                for raw in raw_matches:
                    match = build_match(child, phase_name, raw)
                    if match:
                        all_matches.append(match)
            except Exception as exc:
                LOG.exception("Erreur pour %s/%s: %s", child, phase_name, exc)

    # Déduplication par UID.
    unique = {m.uid: m for m in all_matches}
    data = build_ics(cfg, list(unique.values()))

    out = Path(cfg["calendar"]["output"])
    out.write_bytes(data)
    LOG.info("%d événements écrits dans %s", len(unique), out)


if __name__ == "__main__":
    main()

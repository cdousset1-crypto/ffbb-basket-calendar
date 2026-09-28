from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from icalendar import Calendar, Event
from zoneinfo import ZoneInfo

from ffbb_data_client import FFBBDataClient


LOG = logging.getLogger("ffbb-sync")


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


def resolve_equipe_id(phase: dict) -> str:
    """
    Retourne l'identifiant d'équipe (engagement) FFBB à utiliser pour filtrer
    les rencontres. Accepte soit un "equipe_id" explicite dans config.yaml,
    soit (pour ne pas casser une config existante) une "url" du type
    ".../equipes/200000005368033", dont on extrait l'identifiant numérique.
    """
    equipe_id = phase.get("equipe_id")
    if equipe_id:
        return str(equipe_id)

    url = phase.get("url", "")
    m = re.search(r"/equipes/(\d+)", url)
    if m:
        return m.group(1)

    raise ValueError(
        f"Phase {phase!r} : impossible de déterminer l'ID d'équipe. "
        "Ajoute 'equipe_id: \"...\"' dans config.yaml (ou garde une 'url' "
        "contenant '/equipes/<id>')."
    )


def slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "child"


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class FFBBDataSource:
    """
    Récupère les rencontres directement depuis l'API de données FFBB (via la
    bibliothèque ffbb-data-client), au lieu de scraper le HTML du site
    competitions.ffbb.com. Cela évite le blocage 403 (protection anti-bot du
    site) et fournit des données déjà structurées (date, heure, salle,
    adresse) sans avoir à parser du HTML fragile ni à recouper plusieurs
    pages entre elles.
    """

    def __init__(self):
        self.client = FFBBDataClient.create()

    def fetch_matches(self, child: str, phase: dict) -> list[Match]:
        equipe_id = resolve_equipe_id(phase)

        # Un match peut placer notre équipe en "équipe 1" (domicile, par
        # convention FFBB) ou en "équipe 2" (extérieur) : on doit chercher
        # les deux cas avec un OR dans le filtre Meilisearch.
        filter_expr = (
            f'idEngagementEquipe1.id = "{equipe_id}" '
            f'OR idEngagementEquipe2.id = "{equipe_id}"'
        )

        result = self.client.search_rencontres(filter=[filter_expr], limit=200)
        hits = result.hits if result else []

        if not hits:
            LOG.warning(
                "Aucune rencontre trouvée pour %s / %s (equipe_id=%s) : "
                "vérifie l'ID d'équipe dans config.yaml",
                child, phase["name"], equipe_id
            )

        matches = []
        for hit in hits:
            match = self._build_match(child, phase, equipe_id, hit)
            if match:
                matches.append(match)
        return matches

    def _build_match(self, child, phase, equipe_id, hit) -> Match | None:
        if hit.date_rencontre is None or hit.horaire is None:
            LOG.warning(
                "Match %s ignoré : date ou heure manquante côté API", hit.id
            )
            return None

        is_home = bool(
            hit.id_engagement_equipe1 and hit.id_engagement_equipe1.id == equipe_id
        )
        is_away = bool(
            hit.id_engagement_equipe2 and hit.id_engagement_equipe2.id == equipe_id
        )
        if not (is_home or is_away):
            # Ne devrait pas arriver vu le filtre utilisé, mais on se protège
            # d'un éventuel comportement inattendu de l'API.
            LOG.warning(
                "Match %s ignoré : ne correspond à aucune des deux équipes "
                "attendues (equipe_id=%s)", hit.id, equipe_id
            )
            return None

        team = hit.nom_equipe1 if is_home else hit.nom_equipe2
        opponent = hit.nom_equipe2 if is_home else hit.nom_equipe1
        home_away = "Domicile" if is_home else "Extérieur"

        round_name = f"J{hit.numero_journee}" if hit.numero_journee else ""

        venue_name = hit.salle.libelle if hit.salle else ""
        venue_address = hit.salle.adresse if hit.salle else ""

        uid = f"ffbb-{hit.id}-{slug(child)}@basket-calendar"

        return Match(
            uid=uid,
            child=child,
            phase=phase["name"],
            match_id=str(hit.id),
            round_name=round_name,
            date=hit.date_rencontre.date().isoformat(),
            time=hit.horaire.strftime("%H:%M"),
            home_away=home_away,
            opponent=opponent or "",
            team=team or "",
            detail_url="",  # L'API ne fournit pas d'URL de page de détail.
            venue_name=venue_name or "",
            venue_address=venue_address or "",
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
    source = FFBBDataSource()

    all_matches = []
    for child_cfg in cfg["children"]:
        child = child_cfg["name"]
        for phase in child_cfg.get("phases", []):
            LOG.info("Lecture %s / %s", child, phase["name"])
            try:
                all_matches.extend(source.fetch_matches(child, phase))
            except Exception as exc:
                LOG.exception("Erreur pour %s/%s: %s", child, phase["name"], exc)

    # Déduplication par UID.
    unique = {m.uid: m for m in all_matches}
    data = build_ics(cfg, list(unique.values()))

    out = Path(cfg["calendar"]["output"])
    out.write_bytes(data)
    LOG.info("%d événements écrits dans %s", len(unique), out)


if __name__ == "__main__":
    main()

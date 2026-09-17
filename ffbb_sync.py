from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from dateutil import tz


LOG = logging.getLogger("ffbb-sync")
MONTHS = {
    "janv.": 1, "janv": 1, "janvier": 1,
    "févr.": 2, "févr": 2, "février": 2,
    "mars": 3,
    "avr.": 4, "avr": 4, "avril": 4,
    "mai": 5,
    "juin": 6,
    "juil.": 7, "juil": 7, "juillet": 7,
    "août": 8, "aout": 8,
    "sept.": 9, "sept": 9, "septembre": 9,
    "oct.": 10, "oct": 10, "octobre": 10,
    "nov.": 11, "nov": 11, "novembre": 11,
    "déc.": 12, "dec": 12, "décembre": 12,
}


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


class FFBBClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg["http"]["user_agent"],
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
        })
        self.timeout = cfg["http"]["timeout_seconds"]

    def get(self, url):
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    @staticmethod
    def clean(s: str) -> str:
        return re.sub(r"\s+", " ", s or "").strip()

    def team_page(self, url):
        return BeautifulSoup(self.get(url), "html.parser")

    def extract_team_name(self, soup):
        h1 = soup.find("h1")
        return self.clean(h1.get_text(" ", strip=True)) if h1 else ""

    def extract_match_links(self, soup, base_url):
        """
        FFBB associe le détail d'une rencontre au lien portant l'image versus.svg.
        On privilégie cette signature, puis quelques variantes de nom de fichier.
        """
        found = []
        for img in soup.find_all("img"):
            src = (img.get("href") or "").lower()
            if "versus.svg" not in src:
                continue
            a = img.find_parent("a", href=True)
            if not a:
                continue
            href = urljoin(base_url, a["href"])
            if href not in [x[0] for x in found]:
                found.append((href, a))
        return found

    def parse_card_text(self, anchor):
        """
        Remonte dans le DOM jusqu'à trouver un conteneur raisonnable contenant
        #ID, Jn, une date et une heure. La structure FFBB pouvant évoluer, on
        évite de dépendre de classes CSS minifiées.
        """
        node = anchor
        candidates = []
        for _ in range(7):
            if not node:
                break
            text = self.clean(node.get_text(" ", strip=True))
            if text:
                candidates.append((len(text), node, text))
            node = node.parent

        # Le plus petit conteneur contenant les marqueurs utiles.
        for _, node, text in sorted(candidates, key=lambda x: x[0]):
            if re.search(r"#\d+", text) and re.search(
                r"\bJ\d+\b", text, re.I
            ) and re.search(r"\b\d{1,2}\s+\w+\.?\s+\d{1,2}h\d{2}\b", text):
                return text
        return candidates[-1][2] if candidates else ""

    def parse_match_meta(self, text):
        text = self.clean(text)
        mid = re.search(r"#(\d+)", text)
        rnd = re.search(r"\b(J\d+)\b", text, re.I)
        dt = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ.]+)\s+(\d{1,2})h(\d{2})\b", text)
        side = re.search(r"\b(Domicile|Extérieur)\b", text, re.I)

        if not (mid and dt):
            return None

        day = int(dt.group(1))
        month_key = dt.group(2).lower()
        hour = int(dt.group(3))
        minute = int(dt.group(4))
        month = MONTHS.get(month_key)
        if not month:
            return None

        start_year = self.cfg["season"]["start_year"]
        start_month = self.cfg["season"]["start_month"]
        year = start_year if month >= start_month else start_year + 1

        return {
            "match_id": mid.group(1),
            "round_name": rnd.group(1).upper() if rnd else "",
            "day": day,
            "month": month,
            "year": year,
            "hour": hour,
            "minute": minute,
            "home_away": side.group(1).capitalize() if side else "",
        }

    def find_opponent(self, card_text, team_name):
        t = self.clean(card_text)
        # Retire les champs connus.
        t = re.sub(r"#\d+", " ", t)
        t = re.sub(r"\bJ\d+\b", " ", t, flags=re.I)
        t = re.sub(r"\b\d{1,2}\s+[A-Za-zÀ-ÿ.]+\s+\d{1,2}h\d{2}\b", " ", t)
        t = re.sub(r"\b(Domicile|Extérieur)\b", " ", t, flags=re.I)
        t = self.clean(t)

        # Le nom d'équipe apparaît dans le conteneur ; l'autre texte est généralement
        # l'adversaire. Nettoyage prudent des mots parasites.
        parts = [self.clean(p) for p in re.split(r"\s{2,}", t) if self.clean(p)]
        for p in parts:
            if p.lower() != team_name.lower() and len(p) > 2:
                if not re.fullmatch(r"\d+", p):
                    return p

        # Fallback : utiliser les liens texte du conteneur.
        return ""

    def parse_jsonld_address(self, soup):
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or script.get_text())
            except Exception:
                continue
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                addr = obj.get("address")
                if isinstance(addr, dict):
                    parts = [
                        addr.get("streetAddress"),
                        addr.get("postalCode"),
                        addr.get("addressLocality"),
                    ]
                    parts = [self.clean(x) for x in parts if x]
                    if parts:
                        return ", ".join(parts)
        return ""

    def extract_venue(self, detail_url):
        """
        Cherche d'abord les données sémantiques/JSON-LD, puis les blocs textuels
        contenant salle/adresse. Le parseur est volontairement tolérant pour
        absorber les évolutions de la page FFBB.
        """
        if not detail_url:
            return "", "", ""

        soup = BeautifulSoup(self.get(detail_url), "html.parser")

        address = self.parse_jsonld_address(soup)
        text = self.clean(soup.get_text(" ", strip=True))

        venue_name = ""
        # Plusieurs formulations possibles sur les pages de rencontre.
        labels = [
            r"(?:Salle|Gymnase|Lieu)\s*[:\-]\s*([^|]+?)(?=\s+(?:Adresse|Date|Heure)\b|$)",
            r"(?:Nom de la salle)\s*[:\-]\s*([^|]+?)(?=\s+(?:Adresse|Date|Heure)\b|$)",
        ]
        for pat in labels:
            m = re.search(pat, text, flags=re.I)
            if m:
                venue_name = self.clean(m.group(1))
                break

        if not address:
            m = re.search(
                r"(?:Adresse)\s*[:\-]\s*(.+?)(?=\s+(?:Date|Heure|Arbitre|Officiels)\b|$)",
                text, flags=re.I
            )
            if m:
                address = self.clean(m.group(1))

        # Dernier recours : chercher un bloc qui ressemble à une adresse française.
        if not address:
            m = re.search(
                r"\b\d{1,4}\s+[A-Za-zÀ-ÿ0-9'’ .-]{3,60}\s+\d{5}\s+[A-Za-zÀ-ÿ'’ -]{2,50}\b",
                text
            )
            if m:
                address = self.clean(m.group(0))

        # Si aucun nom de salle n'a été isolé, tenter un élément proche d'une adresse.
        if not venue_name and address:
            for tag in soup.find_all(["div", "section", "li", "p", "td"]):
                tx = self.clean(tag.get_text(" ", strip=True))
                if address in tx and len(tx) < 300:
                    tx2 = re.sub(re.escape(address), " ", tx, flags=re.I)
                    tx2 = self.clean(tx2.strip(" -:|"))
                    if 2 <= len(tx2) <= 100:
                        venue_name = tx2
                        break

        return venue_name, address, detail_url

    def scrape_child(self, child, phase):
        url = phase["url"]
        soup = self.team_page(url)
        team = self.extract_team_name(soup)
        results = []

        links = self.extract_match_links(soup, url)
        if not links:
            LOG.warning("Aucun lien versus.svg trouvé sur %s", url)
            return results

        for detail_url, anchor in links:
            card_text = self.parse_card_text(anchor)
            meta = self.parse_match_meta(card_text)
            if not meta:
                LOG.warning("Impossible de lire le match %s (%s)", detail_url, card_text[:180])
                continue

            # Opponent : on exploite les liens du même conteneur.
            opponent = ""
            node = anchor
            for _ in range(7):
                if not node:
                    break
                for a in node.find_all("a", href=True):
                    txt = self.clean(a.get_text(" ", strip=True))
                    if txt and txt.lower() != team.lower() and txt.lower() != child.lower():
                        if not re.fullmatch(r"\d+", txt):
                            opponent = txt
                            break
                if opponent:
                    break
                node = node.parent

            if not opponent:
                opponent = self.find_opponent(card_text, team)

            # Le match ID FFBB est notre meilleure clé stable.
            uid = f"ffbb-{meta['match_id']}-{slug(child)}@basket-calendar"

            venue_name, venue_address, venue_url = self.extract_venue(detail_url)

            dt = datetime(
                meta["year"], meta["month"], meta["day"],
                meta["hour"], meta["minute"]
            )

            results.append(Match(
                uid=uid,
                child=child,
                phase=phase["name"],
                match_id=meta["match_id"],
                round_name=meta["round_name"],
                date=dt.date().isoformat(),
                time=dt.strftime("%H:%M"),
                home_away=meta["home_away"],
                opponent=opponent,
                team=team,
                detail_url=detail_url,
                venue_name=venue_name,
                venue_address=venue_address,
                venue_url=venue_url,
            ))

        return results


def slug(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "child"


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_ics(cfg, matches):
    cal = Calendar()
    cal.add("prodid", "-//FFBB Basket Enfants//FR//")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", cfg["calendar"]["name"])
    cal.add("x-wr-timezone", cfg["calendar"]["timezone"])

    tzinfo = tz.gettz(cfg["calendar"]["timezone"])
    duration = timedelta(minutes=cfg["calendar"]["duration_minutes"])

    for m in sorted(matches, key=lambda x: (x.date, x.time, x.child)):
        start = datetime.fromisoformat(f"{m.date}T{m.time}").replace(tzinfo=tzinfo)
        end = start + duration

        event = Event()
        event.add("uid", m.uid)
        event.add("dtstamp", datetime.now(tzinfo))
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("summary", f"Match basket {m.child}")

        location = ", ".join(x for x in [m.venue_name, m.venue_address] if x)
        if location:
            event.add("location", location)

        description = "\n".join([
            f"Enfant : {m.child}",
            f"Équipe : {m.team}",
            f"Adversaire : {m.opponent or 'Non détecté'}",
            f"Type : {m.home_away or 'Non précisé'}",
            f"Journée : {m.round_name or 'Non précisée'}",
            f"Phase : {m.phase}",
            f"FFBB : {m.detail_url}",
        ])
        if m.venue_url:
            description += f"\nDétail lieu/rencontre : {m.venue_url}"

        event.add("description", description)
        event.add("url", m.detail_url)
        cal.add_component(event)

    return cal.to_ical()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    client = FFBBClient(cfg)

    all_matches = []
    for child_cfg in cfg["children"]:
        child = child_cfg["name"]
        for phase in child_cfg.get("phases", []):
            LOG.info("Lecture %s / %s", child, phase["name"])
            try:
                all_matches.extend(client.scrape_child(child, phase))
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

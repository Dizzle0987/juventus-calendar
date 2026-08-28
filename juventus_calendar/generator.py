from __future__ import annotations

import hashlib
import html as html_module
import json
import logging
import os
import re
import tempfile
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from icalendar import Alarm, Calendar, Event
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)
ROME = ZoneInfo("Europe/Rome")
OFFICIAL_PAGE = "https://www.juventus.com/en/teams/first-team-men/fixtures-results/"
OFFICIAL_API = "https://www.juventus.com/en/api/v1/matcheslist/team-first-team-men,season-{tag}"
ESPN_API = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
    "{competition}/teams/111/schedule?season={season}"
)
ESPN_STANDINGS_URL = (
    "https://site.api.espn.com/apis/v2/sports/soccer/ita.1/standings?season={season}"
)
UEFA_DRAW_URLS = {
    "champions-league": (
        "UEFA Champions League",
        "https://www.uefa.com/uefachampionsleague/draws/",
    ),
    "europa-league": (
        "UEFA Europa League",
        "https://www.uefa.com/uefaeuropaleague/draws/",
    ),
    "conference-league": (
        "UEFA Conference League",
        "https://www.uefa.com/uefaconferenceleague/draws/",
    ),
}
LEGA_NEWS_URL = "https://www.legaseriea.it/serie-a/news"
THESPORTSDB_API = "https://www.thesportsdb.com/api/v1/json/123/eventsnext.php?id=133676"
OFFICIAL_OPTA_API = (
    "https://api.performfeeds.com/soccerdata/match/1beaeep63zsv71a04kk2qk29pw"
    "?ctst=bqbbqm98ud8obe45ds9ohgyrd&_lcl=en&_pgSz=200&_rt=c&_fmt=json&live=yes"
)

JUVENTUS_ALIASES = {
    "juventus",
    "juventus fc",
    "juventus football club",
    "juve",
}
EXCLUDED_SQUADS = (
    "women",
    "femminile",
    "next gen",
    "nextgen",
    "primavera",
    "under 23",
    "u23",
    "under 20",
    "u20",
    "under 19",
    "u19",
)
TEAM_EQUIVALENTS = {
    "internazionale": "inter",
    "inter milan": "inter",
    "fc internazionale": "inter",
    "ogc nice": "nice",
    "ogc nice cote dazur": "nice",
}

ESPN_COMPETITIONS = {
    "ita.1": "Serie A",
    "ita.2": "Serie B",
    "ita.coppa_italia": "Coppa Italia",
    "ita.super_cup": "Supercoppa Italiana",
    "uefa.champions": "UEFA Champions League",
    "uefa.europa": "UEFA Europa League",
    "uefa.europa.conf": "UEFA Conference League",
    "uefa.super_cup": "Supercoppa UEFA",
    "fifa.cwc": "FIFA Club World Cup",
    "fifa.intercontinental_cup": "Coppa Intercontinentale FIFA",
    "global.club_challenge": "UEFA–CONMEBOL Club Challenge",
    "club.friendly": "Amichevole",
}

# These pages are consulted only for explicit fixture/time statements. Their
# entries enrich a fixture already found through Juventus/ESPN/TheSportsDB.
# Keep these values aligned with Milan Calendar: the higher value wins.
TIME_SOURCE_PRIORITY = {
    "Juventus": 10,
    "Gazzetta dello Sport": 20,
    "Sky Sport": 40,
    "Mediaset Infinity": 40,
    "Prime Video": 40,
    "NOW": 50,
    "DAZN": 60,
}
TIME_SOURCES = (
    ("Juventus", OFFICIAL_PAGE, TIME_SOURCE_PRIORITY["Juventus"], ""),
    ("Gazzetta dello Sport", "https://www.gazzetta.it/Calcio/Serie-A/Juventus/", TIME_SOURCE_PRIORITY["Gazzetta dello Sport"], ""),
    ("Sky Sport", "https://sport.sky.it/calcio/serie-a", TIME_SOURCE_PRIORITY["Sky Sport"], "Sky Sport e NOW"),
    ("NOW", "https://www.nowtv.it/sport/calcio/juventus", TIME_SOURCE_PRIORITY["NOW"], "Sky Sport e NOW"),
    ("DAZN", "https://www.dazn.com/it-IT/schedule", TIME_SOURCE_PRIORITY["DAZN"], "DAZN"),
    (
        "Mediaset Infinity",
        "https://mediasetinfinity.mediaset.it/calcio-e-sport/",
        TIME_SOURCE_PRIORITY["Mediaset Infinity"],
        "Mediaset e Mediaset Infinity",
    ),
    ("Prime Video", "https://www.primevideo.com/storefront/sports", TIME_SOURCE_PRIORITY["Prime Video"], "Prime Video"),
)

BROADCASTERS_BY_COMPETITION = {
    "serie-a": ("DAZN; alcune partite anche su Sky Sport/NOW", "https://www.dazn.com/it-IT/competition/Competition:1pq3co4h7b7h5p8rqq2s8e4r6"),
    "coppa-italia": ("Mediaset e Mediaset Infinity", "https://mediasetinfinity.mediaset.it/calcio-e-sport/"),
    "supercoppa-italiana": ("Mediaset e Mediaset Infinity", "https://mediasetinfinity.mediaset.it/calcio-e-sport/"),
    "champions-league": ("Sky Sport/NOW; possibile esclusiva Prime Video da verificare per la singola partita", "https://sport.sky.it/calcio/champions-league"),
    "europa-league": ("Sky Sport e NOW", "https://sport.sky.it/calcio/europa-league"),
    "conference-league": ("Sky Sport e NOW", "https://sport.sky.it/calcio/conference-league"),
}


class UpdateError(RuntimeError):
    """Raised when no discovery source succeeds, preserving published files."""


@dataclass
class FetchResult:
    events: list[dict[str, Any]]
    successful_sources: list[str]
    errors: list[str]
    serie_a_standing: dict[str, Any] | None = None
    calendar_events: list[dict[str, Any]] | None = None


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": "juventus-calendar/1.0 (+https://github.com/Dizzle0987/juventus-calendar)",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        }
    )
    return session


def season_start(today: date) -> int:
    return today.year if today.month >= 7 else today.year - 1


def season_tag(start_year: int) -> str:
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def _normalize(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(char for char in plain if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def _is_juventus(team: str) -> bool:
    return _normalize(team) in JUVENTUS_ALIASES


def _is_excluded_squad(team: str) -> bool:
    normalized = _normalize(team)
    return any(marker in normalized for marker in EXCLUDED_SQUADS)


def _team_key(team: str) -> str:
    normalized = TEAM_EQUIVALENTS.get(_normalize(team), _normalize(team))
    ignored = {"afc", "cf", "fc", "football", "club", "calcio"}
    return "-".join(token for token in normalized.split() if token not in ignored)


def _competition_family(name: str) -> str:
    value = _normalize(name)
    mappings = (
        (("serie a", "italian serie a"), "serie-a"),
        (("serie b", "italian serie b"), "serie-b"),
        (("coppa italia", "italian coppa italia"), "coppa-italia"),
        (("uefa super cup", "supercoppa uefa"), "supercoppa-uefa"),
        (("supercoppa", "italian super cup"), "supercoppa-italiana"),
        (("champions",), "champions-league"),
        (("europa conference", "conference league"), "conference-league"),
        (("europa",), "europa-league"),
        (("club world cup", "coppa del mondo per club"), "fifa-club-world-cup"),
        (("intercontinental", "coppa intercontinentale"), "coppa-intercontinentale-fifa"),
        (
            ("uefa conmebol club challenge", "conmebol uefa club challenge", "club challenge"),
            "uefa-conmebol-club-challenge",
        ),
        (("friendly", "amichevole", "friendlies"), "amichevole"),
    )
    for needles, family in mappings:
        if any(needle in value for needle in needles):
            return family
    return value.replace(" ", "-") or "altra-competizione"


def _valid_first_team_fixture(home: str, away: str) -> bool:
    # Follow the men's first team, regardless of its opponent. This keeps the
    # traditional Juventus v Next Gen friendly while standalone Next Gen,
    # Women and youth fixtures still fail the exact first-team alias check.
    return bool(home and away and (_is_juventus(home) or _is_juventus(away)))


def parse_official_json(payload: Any, source_url: str = OFFICIAL_PAGE) -> list[dict[str, Any]]:
    """Parse Juventus' structured first-team match endpoint."""
    items = payload if isinstance(payload, list) else payload.get("matches", [])
    events: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        home = str(item.get("TeamHome") or item.get("homeTeam") or "").strip()
        away = str(item.get("TeamAway") or item.get("awayTeam") or "").strip()
        if not _valid_first_team_fixture(home, away):
            continue
        raw_start = str(item.get("KickOffDateTime") or item.get("datetime") or "").strip()
        if not raw_start:
            continue
        parsed = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        is_tbc = bool(item.get("IgnoreTime") or item.get("datetimeTBC"))
        start = parsed.astimezone(ROME).date().isoformat() if is_tbc else parsed.isoformat()
        provider_id = str(item.get("MatchProviderId") or item.get("providerId") or "")
        events.append(
            {
                "source_id": provider_id,
                "source": "Juventus",
                "source_url": source_url,
                "home_team": home,
                "away_team": away,
                "competition": str(item.get("CompetitionName") or item.get("competitionName") or "Partita"),
                "round": str(item.get("Round") or item.get("MatchDay") or item.get("matchDay") or ""),
                "venue": str(item.get("Venue") or item.get("stadiumName") or ""),
                "location": str(item.get("Location") or ""),
                "start": start,
                "all_day": is_tbc,
                "status": "finished" if item.get("IsFinished") else "scheduled",
                "time_source": "Juventus" if not is_tbc else "",
                "time_source_url": source_url if not is_tbc else "",
            }
        )
    return events


def parse_official_html(html: str, source_url: str = OFFICIAL_PAGE) -> list[dict[str, Any]]:
    """Fallback parser for the server-rendered 'Next matches' cards."""
    cards = re.findall(r'<div class="next-match swiper-slide">(.*?)</div>\s*</div>\s*</div>', html, re.S)
    events: list[dict[str, Any]] = []
    for card in cards:
        teams = re.findall(r'jcom-nm__team__name">\s*([^<]+)', card)
        stamp = re.search(r'data-matchtime="([^"]+)"', card)
        competition = re.search(r'next-match-content__header">\s*<span>([^<]+)', card)
        venue = re.search(r'data-venue="([^"]*)"', card)
        if len(teams) < 2 or not stamp or not _valid_first_team_fixture(teams[0], teams[1]):
            continue
        parsed = datetime.strptime(stamp.group(1), "%d/%m/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
        events.append(
            {
                "source_id": "",
                "source": "Juventus",
                "source_url": source_url,
                "home_team": html_module.unescape(teams[0]).strip(),
                "away_team": html_module.unescape(teams[1]).strip(),
                "competition": html_module.unescape(competition.group(1)).strip() if competition else "Partita",
                "round": "",
                "venue": html_module.unescape(venue.group(1)).strip() if venue else "",
                "location": "",
                "start": parsed.isoformat(),
                "all_day": False,
                "status": "scheduled",
                "time_source": "Juventus",
                "time_source_url": source_url,
            }
        )
    return events


def parse_espn_json(payload: dict[str, Any], default_competition: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in payload.get("events", []):
        competition = (item.get("competitions") or [{}])[0]
        competitors = competition.get("competitors") or []
        home_entry = next((x for x in competitors if x.get("homeAway") == "home"), None)
        away_entry = next((x for x in competitors if x.get("homeAway") == "away"), None)
        if not home_entry or not away_entry:
            continue
        home = str((home_entry.get("team") or {}).get("displayName") or "").strip()
        away = str((away_entry.get("team") or {}).get("displayName") or "").strip()
        if not _valid_first_team_fixture(home, away):
            continue
        raw_start = str(item.get("date") or "")
        if not raw_start:
            continue
        parsed = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        status = (item.get("status") or {}).get("type") or {}
        detail = str(status.get("detail") or "").upper()
        time_valid = not any(marker in detail for marker in ("TBD", "TBA", "TBC"))
        event_url = next((x.get("href") for x in item.get("links", []) if x.get("href")), "")
        events.append(
            {
                "source_id": str(item.get("id") or ""),
                "source": "ESPN",
                "source_url": event_url or "https://www.espn.com/soccer/team/fixtures/_/id/111/juventus",
                "home_team": home,
                "away_team": away,
                "competition": str((item.get("league") or {}).get("name") or default_competition),
                "round": str(competition.get("round") or ""),
                "venue": str((competition.get("venue") or {}).get("fullName") or ""),
                "location": str(((competition.get("venue") or {}).get("address") or {}).get("city") or ""),
                "start": parsed.isoformat() if time_valid else parsed.astimezone(ROME).date().isoformat(),
                "all_day": not time_valid,
                "status": str(status.get("name") or "scheduled"),
                "time_source": "ESPN" if time_valid else "",
                "time_source_url": event_url if time_valid else "",
            }
        )
    return events


def parse_espn_standings_json(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract Juventus and its nearest Serie A neighbours from ESPN standings."""
    entries: list[dict[str, Any]] = []
    for child in payload.get("children") or []:
        entries.extend(((child.get("standings") or {}).get("entries") or []))
    entries.extend(((payload.get("standings") or {}).get("entries") or []))

    rows: list[dict[str, Any]] = []
    juventus_row: dict[str, Any] | None = None
    seen_teams: set[str] = set()
    for entry in entries:
        team = entry.get("team") or {}
        team_names = {
            _normalize(str(team.get(key) or ""))
            for key in ("displayName", "shortDisplayName", "name", "abbreviation")
        }
        is_juventus = str(team.get("id") or "") == "111" or bool(
            {"juventus", "juventus fc", "juve"} & team_names
        )

        stats: dict[str, Any] = {}
        for stat in entry.get("stats") or []:
            value = stat.get("value")
            if value is None:
                value = stat.get("displayValue")
            for key in (stat.get("name"), stat.get("abbreviation"), stat.get("shortDisplayName")):
                if key:
                    stats[_normalize(str(key))] = value

        def number(*names: str) -> int | None:
            for name in names:
                value = stats.get(_normalize(name))
                if value not in (None, ""):
                    try:
                        return int(float(str(value).replace(",", ".")))
                    except ValueError:
                        continue
            return None

        position = number("rank", "position", "rk")
        points = number("points", "pts")
        played = number("gamesPlayed", "games played", "gp")
        if position is None or points is None or played is None:
            continue
        team_name = str(
            team.get("shortDisplayName")
            or team.get("displayName")
            or team.get("name")
            or ""
        ).strip()
        team_key = str(team.get("id") or _normalize(team_name))
        if not team_name or team_key in seen_teams:
            continue
        seen_teams.add(team_key)
        row = {
            "team": "Juventus" if is_juventus else team_name,
            "position": position,
            "points": points,
            "played": played,
            "wins": number("wins", "w"),
            "draws": number("ties", "draws", "d"),
            "losses": number("losses", "l"),
            "goal_difference": number("pointDifferential", "goalDifference", "goal difference", "gd"),
        }
        rows.append(row)
        if is_juventus:
            juventus_row = row

    if not juventus_row:
        return None
    rows.sort(key=lambda row: int(row["position"]))
    juventus_index = rows.index(juventus_row)
    window_start = max(0, min(juventus_index - 2, len(rows) - 5))
    result = deepcopy(juventus_row)
    result["context"] = [
        {
            "team": row["team"],
            "position": row["position"],
            "points": row["points"],
            "played": row["played"],
        }
        for row in rows[window_start : window_start + 5]
    ]
    result.pop("team", None)
    result["source"] = "ESPN"
    return result


def parse_uefa_draw_html(
    html: str, competition: str, source_url: str
) -> list[dict[str, Any]]:
    """Parse the current official UEFA draw from structured page metadata."""
    decoded_html = html_module.unescape(html)
    target_dates = re.findall(r'targetDate"\s*:\s*"([^"}]+)', decoded_html)
    structured_events: list[dict[str, Any]] = []
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            value = json.loads(html_module.unescape(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        structured_events.extend(
            item
            for item in values
            if isinstance(item, dict) and str(item.get("@type") or "") == "SportsEvent"
        )

    results: list[dict[str, Any]] = []
    for index, item in enumerate(structured_events):
        name = str(item.get("name") or item.get("description") or "").strip()
        if "draw" not in name.lower():
            continue
        raw_start = target_dates[index] if index < len(target_dates) else str(item.get("startDate") or "")
        if not raw_start:
            continue
        try:
            start = datetime.fromisoformat(raw_start.replace("Z", "+00:00")).astimezone(ROME)
        except ValueError:
            continue
        page_title = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        season_match = re.search(
            r"(20\d{2})[/\\-](\d{2,4})",
            html_module.unescape(page_title.group(1) if page_title else ""),
        )
        season = (
            f"{season_match.group(1)}/{season_match.group(2)[-2:]}"
            if season_match
            else f"{season_start(start.date())}/{str(season_start(start.date()) + 1)[-2:]}"
        )
        phase = re.sub(r"^UEFA\s+.+?\s+-\s+", "", name, flags=re.IGNORECASE)
        phase = re.sub(r"\s+draw$", "", phase, flags=re.IGNORECASE).strip()
        phase_it = {
            "league phase": "fase campionato",
            "knockout phase play-off": "play-off fase a eliminazione diretta",
            "round of 16": "ottavi di finale",
        }.get(phase.lower(), phase)
        location = item.get("location") or []
        places = location if isinstance(location, list) else [location]
        place = next(
            (
                value
                for value in places
                if isinstance(value, dict) and str(value.get("@type") or "") == "Place"
            ),
            {},
        )
        source_id = str(item.get("@id") or "").rsplit("#", 1)[-1]
        results.append(
            {
                "source_id": source_id or f"uefa-draw-{_normalize(competition)}-{season}-{_normalize(phase)}",
                "source": "UEFA",
                "source_url": source_url,
                "event_kind": "draw",
                "title": f"Sorteggio {phase_it} {competition} {season}",
                "competition": competition,
                "start": start.isoformat(),
                "all_day": False,
                "venue": str(place.get("name") or ""),
                "location": str(place.get("address") or place.get("name") or ""),
                "status": "scheduled",
                "reminder_minutes": 30,
                "notes": "Data e orario recuperati automaticamente dalla pagina ufficiale UEFA.",
            }
        )
    return results


def find_lega_calendar_articles(html: str, source_url: str = LEGA_NEWS_URL) -> list[str]:
    """Find recent official Lega articles that may announce a calendar event."""
    links: list[str] = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        url = urljoin(source_url, html_module.unescape(href))
        slug = _normalize(url.rsplit("/", 1)[-1])
        if "/serie-a/news/" not in url or not any(
            word in slug for word in ("sorteggio", "calendario", "tabellone")
        ):
            continue
        if url not in links:
            links.append(url)
    return links


def parse_lega_calendar_article(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse only explicit dates from an official Lega calendar/draw article."""
    headline = ""
    published_year: int | None = None
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            value = json.loads(html_module.unescape(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        article = next(
            (
                item
                for item in values
                if isinstance(item, dict) and str(item.get("@type") or "") == "NewsArticle"
            ),
            None,
        )
        if not article:
            continue
        headline = str(article.get("headline") or "")
        published = str(article.get("datePublished") or "")
        if published[:4].isdigit():
            published_year = int(published[:4])
        break
    normalized_headline = _normalize(headline)
    if not headline or not any(
        word in normalized_headline for word in ("sorteggio", "calendario", "tabellone")
    ):
        return []
    if "coppa italia" in normalized_headline:
        competition = "Coppa Italia"
    elif "supercoppa" in normalized_headline:
        competition = "Supercoppa Italiana"
    elif "serie a" in normalized_headline:
        competition = "Serie A"
    else:
        return []

    text = html_module.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)
    months = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
        "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
        "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    }
    match = re.search(
        rf"(?P<day>\d{{1,2}})\s+(?P<month>{'|'.join(months)})"
        r"(?:\s+(?P<year>20\d{2}))?"
        r"(?:.{0,45}?(?:alle\s+ore|ore)\s*(?P<hour>\d{1,2})[.:](?P<minute>\d{2}))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return []
    year = int(match.group("year") or published_year or 0)
    if not year:
        return []
    month = months[match.group("month").lower()]
    if match.group("hour") is not None:
        start_value = datetime(
            year, month, int(match.group("day")),
            int(match.group("hour")), int(match.group("minute")), tzinfo=ROME,
        ).isoformat()
        all_day = False
    else:
        start_value = date(year, month, int(match.group("day"))).isoformat()
        all_day = True
    season_match = re.search(r"(20\d{2})[/\\-](\d{2,4})", headline)
    season = (
        f"{season_match.group(1)}/{season_match.group(2)[-2:]}" if season_match else str(year)
    )
    if "sorteggio" in normalized_headline:
        kind = "draw"
        title = f"Sorteggio {competition} {season}"
    else:
        kind = "calendar_publication"
        title = (
            f"Presentazione calendario {competition} {season}"
            if competition == "Serie A"
            else f"Pubblicazione tabellone {competition} {season}"
        )
    return [{
        "source_id": f"lega-{_normalize(source_url.rsplit('/', 1)[-1])}",
        "source": "Lega Serie A",
        "source_url": source_url,
        "event_kind": kind,
        "title": title,
        "competition": competition,
        "start": start_value,
        "all_day": all_day,
        "venue": "",
        "location": "",
        "status": "scheduled",
        "reminder_minutes": 30,
        "notes": "Data e orario recuperati automaticamente da un annuncio ufficiale Lega Serie A.",
    }]


def parse_thesportsdb_json(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in payload.get("events") or []:
        home = str(item.get("strHomeTeam") or "").strip()
        away = str(item.get("strAwayTeam") or "").strip()
        if not _valid_first_team_fixture(home, away):
            continue
        timestamp = str(item.get("strTimestamp") or "").strip()
        if timestamp:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            start, all_day = parsed.isoformat(), False
        else:
            start = str(item.get("dateEvent") or "").strip()
            if not start:
                continue
            all_day = True
        event_id = str(item.get("idEvent") or "")
        events.append(
            {
                "source_id": event_id,
                "source": "TheSportsDB",
                "source_url": f"https://www.thesportsdb.com/event/{event_id}" if event_id else "https://www.thesportsdb.com/",
                "home_team": home,
                "away_team": away,
                "competition": str(item.get("strLeague") or "Partita"),
                "round": str(item.get("intRound") or ""),
                "venue": str(item.get("strVenue") or ""),
                "location": str(item.get("strCity") or item.get("strCountry") or ""),
                "start": start,
                "all_day": all_day,
                "status": str(item.get("strStatus") or "scheduled"),
                "time_source": "TheSportsDB" if not all_day else "",
                "time_source_url": f"https://www.thesportsdb.com/event/{event_id}" if not all_day and event_id else "",
            }
        )
    return events


def parse_official_opta_json(payload: dict[str, Any], source_url: str = OFFICIAL_PAGE) -> list[dict[str, Any]]:
    """Parse the structured Opta feed loaded by Juventus' official calendar."""
    events: list[dict[str, Any]] = []
    for wrapper in payload.get("match") or []:
        info = wrapper.get("matchInfo") or {}
        contestants = info.get("contestant") or []
        home_entry = next((x for x in contestants if x.get("position") == "home"), None)
        away_entry = next((x for x in contestants if x.get("position") == "away"), None)
        if not home_entry or not away_entry:
            continue
        home = str(home_entry.get("name") or home_entry.get("officialName") or "").strip()
        away = str(away_entry.get("name") or away_entry.get("officialName") or "").strip()
        if not _valid_first_team_fixture(home, away):
            continue
        raw_date = str(info.get("date") or "").removesuffix("Z")
        raw_time = str(info.get("time") or "").removesuffix("Z")
        if not raw_date:
            continue
        if raw_time:
            parsed = datetime.fromisoformat(f"{raw_date}T{raw_time}+00:00")
            start, all_day = parsed.isoformat(), False
        else:
            start, all_day = raw_date, True
        competition = info.get("competition") or {}
        venue = info.get("venue") or {}
        stage = info.get("stage") or {}
        round_parts = [str(stage.get("name") or "").strip()]
        if info.get("week"):
            round_parts.append(f"Giornata {info['week']}")
        events.append(
            {
                "source_id": str(info.get("id") or ""),
                "source": "Juventus",
                "source_url": source_url,
                "home_team": home,
                "away_team": away,
                "competition": str(competition.get("name") or competition.get("knownName") or "Partita"),
                "round": " · ".join(x for x in round_parts if x),
                "venue": str(venue.get("longName") or venue.get("shortName") or ""),
                "location": str((competition.get("country") or {}).get("name") or ""),
                "neutral": str(venue.get("neutral") or "").lower() == "yes",
                "start": start,
                "all_day": all_day,
                "status": "finished" if (wrapper.get("liveData") or {}).get("matchDetails", {}).get("matchStatus") == "Played" else "scheduled",
                "time_source": "Juventus" if not all_day else "",
                "time_source_url": source_url if not all_day else "",
            }
        )
    return events


def _json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_strings(child)


def parse_schedule_html(
    html: str,
    source: str,
    source_url: str,
    year: int,
    priority: int = 0,
    broadcaster: str = "",
) -> list[dict[str, Any]]:
    """Parse explicit Italian date/time statements from JSON-LD or page state."""
    fragments: list[str] = []
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S
    ):
        try:
            fragments.extend(_json_strings(json.loads(html_module.unescape(raw))))
        except (json.JSONDecodeError, TypeError):
            continue
    fragments.append(html_module.unescape(re.sub(r"<[^>]+>", " ", html)))
    text = " ".join(re.sub(r"\s+", " ", part) for part in fragments)
    months = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
        "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    }
    weekday = r"(?:lun(?:ed[iì])?|mar(?:ted[iì])?|mer(?:coled[iì])?|gio(?:ved[iì])?|ven(?:erd[iì])?|sab(?:ato)?|dom(?:enica)?)"
    team = r"[A-Za-zÀ-ÿ0-9 .']+?"
    patterns = (
        re.compile(
            rf"(?:{weekday}\s+)?(?P<day>\d{{1,2}})\s+(?P<month>{'|'.join(months)})"
            rf"\s*[,]?\s*(?:ore\s*)?(?P<hour>\d{{1,2}})[:.](?P<minute>\d{{2}})\s*[-–:]\s*"
            rf"(?P<home>{team})\s+(?:vs|[-–])\s+(?P<away>{team})(?=[.;]|\s{{2,}}|$)", re.I
        ),
        re.compile(
            rf"(?P<home>{team})\s+(?:vs|[-–])\s+(?P<away>{team})\s*[:,\-]\s*"
            rf"(?:{weekday}\s+)?(?P<day>\d{{1,2}})\s+(?P<month>{'|'.join(months)})"
            rf"\s*[,]?\s*(?:ore\s*)?(?P<hour>\d{{1,2}})[:.](?P<minute>\d{{2}})", re.I
        ),
    )
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            home = match.group("home").strip(" .,-–")
            away = match.group("away").strip(" .,-–")
            if not _valid_first_team_fixture(home, away):
                continue
            start = datetime(
                year, months[match.group("month").lower()], int(match.group("day")),
                int(match.group("hour")), int(match.group("minute")), tzinfo=ROME,
            )
            key = tuple(sorted((_team_key(home), _team_key(away)))) + (start.isoformat(),)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "source": source,
                    "source_url": source_url,
                    "home_team": home,
                    "away_team": away,
                    "competition": "Partita",
                    "start": start.isoformat(),
                    "all_day": False,
                    "_time_overlay": True,
                    "_time_priority": priority,
                    "broadcast_it": broadcaster,
                    "broadcast_source_url": source_url if broadcaster else "",
                }
            )
    return found


def fetch_remote_events(session: requests.Session, today: date) -> FetchResult:
    events: list[dict[str, Any]] = []
    calendar_events: list[dict[str, Any]] = []
    successful: list[str] = []
    errors: list[str] = []
    start_year = season_start(today)
    serie_a_standing: dict[str, Any] | None = None

    official_ok = False
    # Il feed sottoscrivibile rappresenta la stagione attiva. Caricare anche
    # quella precedente raddoppiava quasi il calendario con gare già concluse.
    for year in (start_year,):
        url = OFFICIAL_API.format(tag=season_tag(year))
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            parsed = parse_official_json(response.json(), OFFICIAL_PAGE)
            events.extend(parsed)
            official_ok = official_ok or bool(parsed)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"Juventus {year}: {exc}")
    if official_ok:
        successful.append("Juventus")
    else:
        try:
            response = session.get(OFFICIAL_PAGE, timeout=30)
            response.raise_for_status()
            parsed = parse_official_html(response.text)
            events.extend(parsed)
            if parsed:
                successful.append("Juventus")
        except requests.RequestException as exc:
            errors.append(f"Juventus pagina: {exc}")

    try:
        response = session.get(
            OFFICIAL_OPTA_API,
            timeout=30,
            headers={"Origin": "https://www.juventus.com", "Referer": OFFICIAL_PAGE},
        )
        response.raise_for_status()
        opta_events = parse_official_opta_json(response.json(), OFFICIAL_PAGE)
        active_seasons = {start_year}
        events.extend(item for item in opta_events if _season_for(item) in active_seasons)
        if opta_events and "Juventus" not in successful:
            successful.append("Juventus")
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"Juventus Opta: {exc}")

    espn_ok = False
    for competition, name in ESPN_COMPETITIONS.items():
        for season in (start_year, start_year + 1):
            url = ESPN_API.format(competition=competition, season=season)
            try:
                response = session.get(url, timeout=20)
                response.raise_for_status()
                parsed = parse_espn_json(response.json(), name)
                events.extend(parsed)
                espn_ok = espn_ok or bool(parsed)
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"ESPN {competition}/{season}: {exc}")
    if espn_ok:
        successful.append("ESPN")

    standings_url = ESPN_STANDINGS_URL.format(season=start_year)
    try:
        response = session.get(standings_url, timeout=20)
        response.raise_for_status()
        serie_a_standing = parse_espn_standings_json(response.json())
        if not serie_a_standing:
            raise ValueError("classifica Juventus non presente nella risposta")
        serie_a_standing["source_url"] = standings_url
        successful.append("ESPN classifica")
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"ESPN classifica: {exc}")
        LOGGER.info("Classifica ESPN non disponibile: %s", exc)

    try:
        response = session.get(THESPORTSDB_API, timeout=20)
        response.raise_for_status()
        parsed = parse_thesportsdb_json(response.json())
        events.extend(parsed)
        if parsed:
            successful.append("TheSportsDB")
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"TheSportsDB: {exc}")

    for source, url, priority, broadcaster in TIME_SOURCES:
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            overlays = parse_schedule_html(response.text, source, url, start_year, priority, broadcaster)
            events.extend(overlays)
            if overlays:
                successful.append(source)
        except requests.RequestException as exc:
            errors.append(f"{source}: {exc}")

    uefa_draws_ok = False
    for competition, url in UEFA_DRAW_URLS.values():
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            parsed_draws = parse_uefa_draw_html(response.text, competition, url)
            if not parsed_draws:
                raise ValueError("nessun sorteggio strutturato disponibile")
            calendar_events.extend(parsed_draws)
            uefa_draws_ok = True
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"UEFA sorteggi {competition}: {exc}")
            LOGGER.info("Fonte sorteggi UEFA %s non disponibile: %s", competition, exc)
    if uefa_draws_ok:
        successful.append("UEFA sorteggi")

    try:
        response = session.get(LEGA_NEWS_URL, timeout=20)
        response.raise_for_status()
        lega_events: list[dict[str, Any]] = []
        for article_url in find_lega_calendar_articles(response.text)[:8]:
            try:
                article = session.get(article_url, timeout=20)
                article.raise_for_status()
                lega_events.extend(parse_lega_calendar_article(article.text, article_url))
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"Lega calendario {article_url}: {exc}")
        if lega_events:
            calendar_events.extend(lega_events)
            successful.append("Lega calendario")
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"Lega calendario: {exc}")
        LOGGER.info("Fonte calendario Lega Serie A non disponibile: %s", exc)

    return FetchResult(
        events,
        list(dict.fromkeys(successful)),
        errors,
        serie_a_standing,
        calendar_events,
    )


def _event_datetime(event: dict[str, Any]) -> datetime:
    raw = str(event["start"])
    if event.get("all_day") or len(raw) == 10:
        return datetime.combine(date.fromisoformat(raw[:10]), time.min, tzinfo=ROME)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ROME)


def _season_for(event: dict[str, Any]) -> int:
    value = _event_datetime(event).astimezone(ROME)
    return value.year if value.month >= 7 else value.year - 1


def _semantic_base(event: dict[str, Any]) -> str:
    if str(event.get("event_kind") or "match") != "match":
        return "|".join(
            (
                str(_season_for(event)),
                str(event.get("event_kind") or "calendar-event"),
                _normalize(str(event.get("source_id") or event.get("id") or event.get("title") or "")),
                _competition_family(str(event.get("competition") or "")),
            )
        )
    teams = (_team_key(str(event.get("home_team") or "")), _team_key(str(event.get("away_team") or "")))
    return "|".join((str(_season_for(event)), *teams, _competition_family(str(event.get("competition") or ""))))


def _uid_for(event: dict[str, Any]) -> str:
    explicit = str(event.get("uid") or "").strip()
    if explicit:
        return explicit if "@" in explicit else f"{explicit}@juventus-calendar"
    return f"{hashlib.sha256(_semantic_base(event).encode()).hexdigest()[:24]}@juventus-calendar"


def _merge_broadcaster_overlay(event: dict[str, Any], overlay: dict[str, Any]) -> None:
    rights = BROADCASTERS_BY_COMPETITION.get(
        _competition_family(str(event.get("competition") or ""))
    )
    if rights and not event.get("broadcast_it"):
        event["broadcast_it"], event["broadcast_source_url"] = rights

    candidate = str(overlay.get("broadcast_it") or "").strip()
    existing = str(event.get("broadcast_it") or "").strip()
    if candidate:
        if not existing:
            event["broadcast_it"] = candidate
        elif candidate.lower() not in existing.lower() and existing.lower() not in candidate.lower():
            event["broadcast_it"] = f"{existing}; {candidate}"

    source_urls = [
        str(value)
        for value in (event.get("broadcast_source_url"), overlay.get("broadcast_source_url"))
        if value
    ]
    if source_urls:
        event["broadcast_source_urls"] = list(dict.fromkeys(source_urls))
        event["broadcast_source_url"] = event["broadcast_source_urls"][-1]


def _same_source_id(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(
        left.get("source")
        and left.get("source") == right.get("source")
        and left.get("source_id")
        and str(left.get("source_id")) == str(right.get("source_id"))
    )


def _is_postponed(event: dict[str, Any]) -> bool:
    if event.get("postponed") is True:
        return True
    status = _normalize(str(event.get("status") or ""))
    return status in {"pst", "ppd"} or any(
        marker in status for marker in ("postponed", "rinviat", "suspended")
    )


def _same_fixture(left: dict[str, Any], right: dict[str, Any], *, unordered: bool = False) -> bool:
    left_teams = (_team_key(str(left.get("home_team") or "")), _team_key(str(left.get("away_team") or "")))
    right_teams = (_team_key(str(right.get("home_team") or "")), _team_key(str(right.get("away_team") or "")))
    if unordered:
        teams_match = sorted(left_teams) == sorted(right_teams)
    else:
        teams_match = left_teams == right_teams
    if not teams_match:
        return False
    left_family = _competition_family(str(left.get("competition") or ""))
    right_family = _competition_family(str(right.get("competition") or ""))
    generic = {"partita", "altra-competizione"}
    if left_family != right_family and not ({left_family, right_family} & generic):
        return False
    return abs((_event_datetime(left) - _event_datetime(right)).total_seconds()) <= 72 * 3600


def _same_long_range_fixture(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_teams = tuple(_team_key(str(left.get(key) or "")) for key in ("home_team", "away_team"))
    right_teams = tuple(_team_key(str(right.get(key) or "")) for key in ("home_team", "away_team"))
    if left_teams != right_teams:
        return False
    left_family = _competition_family(str(left.get("competition") or ""))
    right_family = _competition_family(str(right.get("competition") or ""))
    if left_family != right_family or left_family in {"partita", "altra-competizione"}:
        return False
    left_round = _normalize(str(left.get("round") or ""))
    right_round = _normalize(str(right.get("round") or ""))
    if left_round and right_round and left_round != right_round:
        return False
    return abs((_event_datetime(left) - _event_datetime(right)).total_seconds()) <= 240 * 24 * 3600


def merge_remote_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = list(events)
    base = [x for x in candidates if not x.get("_time_overlay")]
    overlays = [x for x in candidates if x.get("_time_overlay")]
    priority = {"Juventus": 0, "ESPN": 1, "TheSportsDB": 2}
    merged: list[dict[str, Any]] = []
    for candidate in sorted(base, key=lambda x: priority.get(str(x.get("source")), 9)):
        existing = next(
            (x for x in merged if _same_source_id(x, candidate) or _same_fixture(x, candidate)),
            None,
        )
        if existing is None:
            long_range_matches = [x for x in merged if _same_long_range_fixture(x, candidate)]
            existing = long_range_matches[0] if len(long_range_matches) == 1 else None
        if existing is None:
            merged.append(deepcopy(candidate))
        else:
            for key, value in candidate.items():
                if not existing.get(key) and value:
                    existing[key] = value
    for overlay in sorted(overlays, key=lambda x: int(x.get("_time_priority") or 0)):
        existing = next((x for x in merged if _same_fixture(x, overlay, unordered=True)), None)
        if existing is None:
            continue
        previous_start = str(existing.get("start") or "")
        candidate_start = str(overlay.get("start") or "")
        same_instant = (
            previous_start
            and candidate_start
            and not existing.get("all_day")
            and _event_datetime(existing).astimezone(timezone.utc)
            == _event_datetime(overlay).astimezone(timezone.utc)
        )
        if previous_start and not existing.get("all_day") and not same_instant:
            conflict = {
                "source": str(existing.get("time_source") or existing.get("source") or ""),
                "source_url": str(existing.get("time_source_url") or existing.get("source_url") or ""),
                "start": previous_start,
            }
            conflicts = existing.setdefault("time_conflicts", [])
            if conflict not in conflicts:
                conflicts.append(conflict)
        existing["start"] = candidate_start
        existing["all_day"] = False
        existing["time_source"] = overlay["source"]
        existing["time_source_url"] = overlay["source_url"]
        if overlay.get("broadcast_it"):
            _merge_broadcaster_overlay(existing, overlay)
    return sorted(merged, key=_event_datetime)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_manual_events(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path, {"events": []})
    items = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("data/manual_events.json deve contenere una lista o un oggetto con 'events'")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Evento manuale #{index + 1} non valido")
        if item.get("enabled") is False:
            continue
        missing = sorted({"home_team", "away_team", "competition", "start"} - item.keys())
        if missing:
            raise ValueError(f"Evento manuale #{index + 1}: campi mancanti: {', '.join(missing)}")
        event = deepcopy(item)
        event.pop("enabled", None)
        event.setdefault("source", "Manuale")
        event.setdefault("source_url", "")
        event.setdefault("round", "")
        event.setdefault("venue", "")
        event.setdefault("location", "")
        event.setdefault("all_day", len(str(event["start"])) == 10)
        event.setdefault("status", "scheduled")
        result.append(event)
    return result


def load_calendar_events(
    path: Path, participating_competitions: set[str]
) -> list[dict[str, Any]]:
    """Load official non-match dates, filtering competitions Juventus does not play."""
    payload = load_json(path, {"events": []})
    events = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError("data/calendar_events.json deve contenere una lista o un oggetto con 'events'")
    result: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"Evento calendario #{index + 1} non valido")
        if event.get("enabled") is False:
            continue
        required = {"title", "competition", "start", "source_url"}
        missing = sorted(required - event.keys())
        if missing:
            raise ValueError(
                f"Evento calendario #{index + 1}: campi mancanti: {', '.join(missing)}"
            )
        family = _competition_family(str(event["competition"]))
        if (
            event.get("requires_participation", True)
            and not event.get("participation_confirmed", False)
            and family not in participating_competitions
        ):
            continue
        normalized = deepcopy(event)
        normalized.pop("enabled", None)
        normalized.pop("requires_participation", None)
        normalized.pop("participation_confirmed", None)
        normalized.setdefault("event_kind", "draw")
        normalized.setdefault("source", "Calendario ufficiale")
        normalized.setdefault("source_id", str(event.get("id") or f"calendar-{index + 1}"))
        normalized.setdefault("round", "")
        normalized.setdefault("venue", "")
        normalized.setdefault("location", "")
        normalized.setdefault("all_day", len(str(event["start"])) == 10)
        normalized.setdefault("status", "scheduled")
        normalized.setdefault("reminder_minutes", 30)
        result.append(normalized)
    return sorted(result, key=_event_datetime)


def _same_calendar_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if str(left.get("event_kind") or "match") == "match" or str(
        right.get("event_kind") or "match"
    ) == "match":
        return False
    return (
        str(left.get("event_kind") or "") == str(right.get("event_kind") or "")
        and _competition_family(str(left.get("competition") or ""))
        == _competition_family(str(right.get("competition") or ""))
        and abs((_event_datetime(left) - _event_datetime(right)).total_seconds())
        <= 36 * 60 * 60
    )


def merge_calendar_events(*sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge automatic, configured and previously discovered non-match events."""
    merged: list[dict[str, Any]] = []
    for source in sources:
        for candidate in source:
            existing = next(
                (
                    event
                    for event in merged
                    if _same_source_id(event, candidate)
                    or _same_calendar_event(event, candidate)
                ),
                None,
            )
            if existing is None:
                merged.append(deepcopy(candidate))
                continue
            for key, value in candidate.items():
                if not existing.get(key) and value:
                    existing[key] = deepcopy(value)
    return sorted(merged, key=_event_datetime)


def merge_manual_events(remote: list[dict[str, Any]], manual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = deepcopy(remote)
    for candidate in manual:
        uid = _uid_for(candidate)
        index = next(
            (
                i for i, event in enumerate(merged)
                if _uid_for(event) == uid
                or _same_source_id(event, candidate)
                or _same_fixture(event, candidate, unordered=True)
            ),
            None,
        )
        if index is None:
            long_range_matches = [
                i for i, event in enumerate(merged) if _same_long_range_fixture(event, candidate)
            ]
            index = long_range_matches[0] if len(long_range_matches) == 1 else None
        manual_event = deepcopy(candidate)
        locked = bool(manual_event.pop("locked", False))
        if index is None:
            merged.append(manual_event)
        else:
            protected: dict[str, Any] = {}
            existing = merged[index]
            if not locked and TIME_SOURCE_PRIORITY.get(str(existing.get("time_source") or ""), 0):
                for key in (
                    "start", "all_day", "time_source", "time_source_url",
                    "broadcast_it", "broadcast_source_url", "broadcast_source_urls",
                    "time_conflicts",
                ):
                    if key in existing:
                        protected[key] = deepcopy(existing[key])
            existing.update(manual_event)
            existing.update(protected)
    return sorted(merged, key=_event_datetime)


def _canonical_event(
    event: dict[str, Any],
    previous: list[dict[str, Any]],
    changed_at: str,
    used_uids: set[str] | None = None,
) -> dict[str, Any]:
    result = deepcopy(event)
    result.pop("_time_overlay", None)
    result.pop("_time_priority", None)
    result.setdefault("location", "")
    result.setdefault("neutral", False)
    result.setdefault("time_source", "")
    result.setdefault("time_source_url", "")
    is_match = str(result.get("event_kind") or "match") == "match"
    if result.get("all_day"):
        result["start"] = str(result["start"])[:10]
    else:
        result["start"] = _event_datetime(result).astimezone(ROME).isoformat()
    result["uid"] = _uid_for(result)
    generated_uid = result["uid"]
    old = next((x for x in previous if _same_source_id(x, result)), None)
    if old is None and not is_match:
        old = next((x for x in previous if _same_calendar_event(x, result)), None)
    if old is None and is_match:
        long_range_matches = [x for x in previous if _same_long_range_fixture(x, result)]
        old = long_range_matches[0] if len(long_range_matches) == 1 else None
    if old is None and is_match:
        old = next((x for x in previous if _same_fixture(x, result, unordered=True)), None)
    if old is None:
        old = next((x for x in previous if x.get("uid") == generated_uid), None)
    if old is not None:
        if old.get("uid") and str(old["uid"]) not in (used_uids or set()):
            result["uid"] = str(old["uid"])
        else:
            result["uid"] = generated_uid
    if used_uids is not None and result["uid"] in used_uids:
        collision_base = "|".join(
            (
                _semantic_base(result),
                str(result.get("source") or ""),
                str(result.get("source_id") or ""),
                str(result.get("start") or ""),
            )
        )
        result["uid"] = f"{hashlib.sha256(collision_base.encode()).hexdigest()[:24]}@juventus-calendar"
    if is_match:
        result["home_away"] = "Casa" if _is_juventus(str(result.get("home_team"))) else "Trasferta"
        if result.get("neutral"):
            result["home_away"] = "Campo neutro"
        base_title = f"{result['home_team']} - {result['away_team']}"
    else:
        result["home_away"] = ""
        base_title = str(result["title"])
    family = _competition_family(str(result.get("competition") or ""))
    if is_match and not result.get("broadcast_it") and family in BROADCASTERS_BY_COMPETITION:
        result["broadcast_it"], result["broadcast_source_url"] = BROADCASTERS_BY_COMPETITION[family]
    if is_match:
        result.setdefault("broadcast_it", "Da definire")
        result.setdefault("broadcast_source_url", "")
    if is_match and not result.get("time_source") and not result.get("all_day"):
        result["time_source"] = str(result.get("source") or "")
        result["time_source_url"] = str(result.get("source_url") or "")

    explicitly_cleared = result.get("postponed") is False
    if is_match and not explicitly_cleared and _is_postponed(result):
        result["postponed"] = True
        result.setdefault(
            "postponed_from",
            str((old or {}).get("postponed_from") or (old or {}).get("start") or result["start"]),
        )
        result.setdefault("postponed_to", "")
    elif is_match and not explicitly_cleared and old and old.get("postponed"):
        if str(result.get("start")) != str(old.get("start")):
            result["postponed"] = True
            result["postponed_from"] = str(old.get("postponed_from") or old.get("start") or "")
            result["postponed_to"] = str(result["start"])
            if old.get("postponement_reason") and not result.get("postponement_reason"):
                result["postponement_reason"] = old["postponement_reason"]
        elif old.get("postponed_to"):
            for key in ("postponed", "postponed_from", "postponed_to", "postponement_reason"):
                if old.get(key) and not result.get(key):
                    result[key] = old[key]

    if is_match and result.get("postponed"):
        postponed_to = str(result.get("postponed_to") or "")
        if postponed_to:
            new_date = date.fromisoformat(postponed_to[:10]).strftime("%d/%m/%Y")
            result["title"] = f"RINVIATA AL {new_date} — {base_title}"
        else:
            result["title"] = f"RINVIATA — DATA DA DESTINARSI — {base_title}"
            result["start"] = str(result.get("postponed_from") or result["start"])[:10]
            result["all_day"] = True
    else:
        result.pop("postponed", None)
        result["title"] = base_title
    ignored = {"last_modified", "sequence"}
    comparable = {k: v for k, v in result.items() if k not in ignored}
    old_comparable = {k: v for k, v in (old or {}).items() if k not in ignored}
    changed = old is None or comparable != old_comparable
    result["last_modified"] = changed_at if changed else str(old.get("last_modified"))
    result["sequence"] = 0 if old is None else int(old.get("sequence") or 0) + (1 if changed else 0)
    if used_uids is not None:
        if result["uid"] in used_uids:
            raise ValueError(f"UID duplicato non risolvibile: {result['uid']}")
        used_uids.add(result["uid"])
    return result


def build_ical(events: list[dict[str, Any]]) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//Juventus Calendar//Dizzle0987//IT")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", "Juventus Calendar")
    calendar.add("x-wr-timezone", "Europe/Rome")
    calendar.add("x-published-ttl", "PT6H")
    calendar.add("refresh-interval", "PT6H", parameters={"VALUE": "DURATION"})
    for data in events:
        component = Event()
        component.add("uid", data["uid"])
        is_match = str(data.get("event_kind") or "match") == "match"
        summary_icon = "⚽" if is_match else ("🎲" if data.get("event_kind") == "draw" else "🗓️")
        component.add(
            "summary",
            f"⏸ {data['title']}" if data.get("postponed") else f"{summary_icon} {data['title']}",
        )
        start = _event_datetime(data).astimezone(ROME)
        if data.get("all_day"):
            component.add("dtstart", start.date())
            component.add("dtend", start.date() + timedelta(days=1))
        else:
            component.add("dtstart", start)
            component.add("dtend", start + timedelta(hours=2 if is_match else 1))
        modified = datetime.fromisoformat(str(data["last_modified"]).replace("Z", "+00:00"))
        component.add("dtstamp", modified.astimezone(timezone.utc))
        component.add("last-modified", modified.astimezone(timezone.utc))
        component.add("sequence", int(data.get("sequence") or 0))
        if data.get("postponed"):
            component.add("status", "CONFIRMED" if data.get("postponed_to") else "TENTATIVE")
        place = ", ".join(x for x in (str(data.get("venue") or ""), str(data.get("location") or "")) if x)
        if place:
            component.add("location", place)
        if data.get("source_url"):
            component.add("url", str(data["source_url"]))
        details = [f"Competizione: {data['competition']}"]
        if is_match:
            details.append(f"Juventus: {data['home_away']}")
        else:
            details.append(
                "Tipo: Sorteggio"
                if data.get("event_kind") == "draw"
                else "Tipo: Pubblicazione calendario/tabellone"
            )
        details.append(
            "Orario: da confermare"
            if data.get("all_day")
            else f"Orario (Roma): {start.strftime('%d/%m/%Y %H:%M')}"
        )
        if data.get("round"):
            details.append(f"Turno: {data['round']}")
        if data.get("postponed"):
            details.append(
                "Rinvio: "
                + (f"nuova data {str(data['postponed_to'])[:10]}" if data.get("postponed_to") else "data da destinarsi")
            )
            if data.get("postponed_from"):
                details.append(f"Data originaria: {str(data['postponed_from'])[:10]}")
            if data.get("postponement_reason"):
                details.append(f"Motivo: {data['postponement_reason']}")
        if data.get("venue"):
            details.append(f"Stadio: {data['venue']}")
        if data.get("location"):
            details.append(f"Località: {data['location']}")
        if is_match and data.get("broadcast_it"):
            details.append(f"Dove vederla in Italia: {data['broadcast_it']}")
        if is_match and data.get("time_source"):
            details.append(f"Fonte orario: {data['time_source']}")
        if not is_match and data.get("notes"):
            details.append(str(data["notes"]))
        standing = data.get("serie_a_standing") or {}
        if is_match and _competition_family(str(data.get("competition") or "")) == "serie-a" and standing:
            goal_difference = standing.get("goal_difference")
            goal_difference_text = (
                f" — DR {int(goal_difference):+d}" if goal_difference is not None else ""
            )
            context = standing.get("context") or []
            if context:
                details.append("Classifica Serie A:")
                for row in context:
                    is_juventus_row = _is_juventus(str(row.get("team") or ""))
                    marker = "▶" if is_juventus_row else " "
                    extra = (
                        f" — {standing['played']} PG{goal_difference_text}"
                        if is_juventus_row
                        else ""
                    )
                    details.append(
                        f"{marker} {row['position']}. {row['team']} — {row['points']} pt{extra}"
                    )
            else:
                details.append(
                    f"Classifica Juventus: {standing['position']}º — {standing['points']} pt — "
                    f"{standing['played']} PG{goal_difference_text}"
                )
            if standing.get("updated_at"):
                updated = datetime.fromisoformat(str(standing["updated_at"]).replace("Z", "+00:00"))
                details.append(
                    f"Classifica aggiornata: {updated.astimezone(ROME).strftime('%d/%m/%Y %H:%M')}"
                )
        component.add("description", "\n".join(details))
        component.add("categories", [str(data["competition"]), "Juventus"])
        component.add("transp", "OPAQUE")
        if not data.get("postponed") or data.get("postponed_to"):
            reminder_minutes = int(data.get("reminder_minutes") or (150 if is_match else 30))
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", f"Tra {reminder_minutes} minuti: {data['title']}")
            alarm.add("trigger", timedelta(minutes=-reminder_minutes))
            component.add_component(alarm)
        calendar.add_component(component)
    return calendar.to_ical()


def _atomic_write_many(files: dict[Path, bytes]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for path, content in files.items():
            if path.exists() and path.read_bytes() == content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temp_path = Path(temp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((temp_path, path))
        for temp_path, path in staged:
            os.replace(temp_path, path)
    finally:
        for temp_path, _ in staged:
            if temp_path.exists():
                temp_path.unlink()


def update_calendar(
    root: Path,
    session: requests.Session | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    root = root.resolve()
    events_path = root / "data" / "events.json"
    manual_path = root / "data" / "manual_events.json"
    calendar_events_path = root / "data" / "calendar_events.json"
    previous_payload = load_json(events_path, {"events": []})
    previous = previous_payload.get("events", []) if isinstance(previous_payload, dict) else []
    changed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fetched = fetch_remote_events(session or build_session(), today or datetime.now(ROME).date())
    discovery_sources = {"Juventus", "ESPN", "TheSportsDB"}
    if not discovery_sources.intersection(fetched.successful_sources) or not any(
        not item.get("_time_overlay") for item in fetched.events
    ):
        raise UpdateError(
            "Nessuna fonte di scoperta disponibile; gli output precedenti sono rimasti invariati. "
            + "; ".join(fetched.errors[:3])
        )
    remote = merge_remote_events(fetched.events)
    combined = merge_manual_events(remote, load_manual_events(manual_path))
    participating_competitions = {
        _competition_family(str(event.get("competition") or ""))
        for event in combined
        if str(event.get("event_kind") or "match") == "match"
    }
    configured_calendar_events = load_calendar_events(
        calendar_events_path, participating_competitions
    )
    participating_competitions.update(
        _competition_family(str(event.get("competition") or ""))
        for event in configured_calendar_events
    )
    automatic_calendar_events = [
        event
        for event in (fetched.calendar_events or [])
        if (
            _competition_family(str(event.get("competition") or ""))
            in participating_competitions
            or (
                _competition_family(str(event.get("competition") or "")) == "coppa-italia"
                and "serie-a" in participating_competitions
            )
        )
    ]
    previous_calendar_events = [
        event
        for event in previous
        if str(event.get("event_kind") or "match") != "match"
    ]
    combined.extend(
        merge_calendar_events(
            automatic_calendar_events,
            configured_calendar_events,
            previous_calendar_events,
        )
    )
    combined.sort(key=_event_datetime)
    standing_with_timestamp: dict[str, Any] | None = None
    if fetched.serie_a_standing:
        standing_with_timestamp = deepcopy(fetched.serie_a_standing)
        previous_standing = (
            previous_payload.get("serie_a_standing")
            if isinstance(previous_payload, dict)
            else None
        )
        previous_without_timestamp = {
            key: value
            for key, value in (previous_standing or {}).items()
            if key != "updated_at"
        }
        standing_with_timestamp["updated_at"] = (
            str(previous_standing["updated_at"])
            if previous_standing
            and previous_without_timestamp == fetched.serie_a_standing
            and previous_standing.get("updated_at")
            else changed_at
        )
    used_uids: set[str] = set()
    canonical: list[dict[str, Any]] = []
    for event in combined:
        if (
            str(event.get("event_kind") or "match") == "match"
            and
            _competition_family(str(event.get("competition") or "")) == "serie-a"
            and standing_with_timestamp
        ):
            event = deepcopy(event)
            event["serie_a_standing"] = deepcopy(standing_with_timestamp)
        canonical.append(_canonical_event(event, previous, changed_at, used_uids))
    old_core = [{k: v for k, v in x.items() if k not in {"last_modified", "sequence"}} for x in previous]
    new_core = [{k: v for k, v in x.items() if k not in {"last_modified", "sequence"}} for x in canonical]
    last_changed = (
        str(previous_payload.get("last_changed"))
        if isinstance(previous_payload, dict) and old_core == new_core and previous_payload.get("last_changed")
        else changed_at
    )
    payload = {
        "schema_version": 1,
        "timezone": "Europe/Rome",
        "last_changed": last_changed,
        "sources_used": fetched.successful_sources,
        "source_errors": fetched.errors,
        "serie_a_standing": standing_with_timestamp,
        "event_count": len(canonical),
        "events": canonical,
    }
    json_bytes = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    ical_bytes = build_ical(canonical)
    _atomic_write_many({events_path: json_bytes, root / "calendar.ics": ical_bytes})
    LOGGER.info("Generati %d eventi da %s", len(canonical), ", ".join(fetched.successful_sources))
    return canonical

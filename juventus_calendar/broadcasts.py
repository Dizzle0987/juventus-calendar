from __future__ import annotations

import html as html_module
import json
import logging
import re
import unicodedata
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

LOGGER = logging.getLogger(__name__)
ROME = ZoneInfo("Europe/Rome")
JUVENTUS_ALIASES = {"juventus", "juventus fc", "juventus football club", "juve"}
TEAM_EQUIVALENTS = {
    "internazionale": "inter",
    "inter-milan": "inter",
    "manchester-utd": "manchester-united",
    "man-utd": "manchester-united",
    "psg": "paris-saint-germain",
    "celta-de-vigo": "celta-vigo",
    "rc-celta-de-vigo": "celta-vigo",
    "az-alkmaar": "az",
    "nec-nijmegen": "nec",
    "real-sociedad-de-futbol": "real-sociedad",
}
TEAM_COUNTRY_OVERRIDES = {
    "celta-vigo": ("Spagna", "ES"),
    "real-sociedad": ("Spagna", "ES"),
    "rennes": ("Francia", "FR"),
    "stade-rennes": ("Francia", "FR"),
    "az": ("Paesi Bassi", "NL"),
    "omonia-nicosia": ("Cipro", "CY"),
    "hapoel-beer-sheva": ("Israele", "IL"),
    "hapoel-be-er-sheva": ("Israele", "IL"),
    "ferencvaros": ("Ungheria", "HU"),
}
BROADCAST_CONFIG_PATH = Path(__file__).parents[1] / "data" / "broadcast_sources.json"
INTERNATIONAL_COMPETITION_FAMILIES = {
    "champions-league", "europa-league", "conference-league", "supercoppa-uefa",
    "coppa-mondo-club-fifa", "coppa-intercontinentale-fifa", "uefa-conmebol-club-challenge",
}
BROADCAST_EVIDENCE_MARKERS = {
    "diretta", "live", "streaming", "tv", "televisione", "canale", "channel", "watch",
    "guarda", "vederla", "palinsesto", "programma", "programmazione", "broadcast",
    "ubertragung", "transmissao", "ao-vivo",
}
BROADCAST_GUIDE_HORIZON_DAYS = 21
TV8_PROGRAMMING_API = "https://www.tv8.it/api/programmingCarousel"
TEAM_COUNTRY_LOOKUP_URL = "https://www.thesportsdb.com/api/v1/json/123/searchteams.php"
BROADCAST_CONFIDENCE_THRESHOLD = 80
BROADCAST_GUIDE_SOURCE_TYPES = {"guide", "wordpress_search"}
MONTH_NAMES = {
    1: ("gennaio", "january", "januar", "janvier", "enero", "janeiro"),
    2: ("febbraio", "february", "februar", "fevrier", "febrero", "fevereiro"),
    3: ("marzo", "march", "marz", "mars", "marzo", "marco"),
    4: ("aprile", "april", "avril", "abril"), 5: ("maggio", "may", "mai", "mayo", "maio"),
    6: ("giugno", "june", "juni", "juin", "junio", "junho"),
    7: ("luglio", "july", "juli", "juillet", "julio", "julho"),
    8: ("agosto", "august", "aout", "agosto"),
    9: ("settembre", "september", "septembre", "septiembre", "setembro", "eylul", "sentyabr"),
    10: ("ottobre", "october", "oktober", "octobre", "octubre", "outubro"),
    11: ("novembre", "november", "noviembre"),
    12: ("dicembre", "december", "dezember", "decembre", "diciembre", "dezembro"),
}

def season_start(today: date) -> int:
    return today.year if today.month >= 7 else today.year - 1


def _normalize(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(char for char in plain if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def _competition_family(name: str) -> str:
    value = _normalize(name)
    mappings = (
        (("serie-a", "italian-serie-a"), "serie-a"),
        (("serie-b", "italian-serie-b"), "serie-b"),
        (("coppa-italia", "italian-coppa-italia"), "coppa-italia"),
        (("supercoppa", "italian-supercoppa"), "supercoppa-italiana"),
        (("champions",), "champions-league"),
        (("europa-conference", "conference-league"), "conference-league"),
        (("europa",), "europa-league"),
        (("uefa-super-cup", "supercoppa-uefa"), "supercoppa-uefa"),
        (("club-world-cup", "coppa-del-mondo-per-club"), "coppa-mondo-club-fifa"),
        (("intercontinental",), "coppa-intercontinentale-fifa"),
        (("club-challenge",), "uefa-conmebol-club-challenge"),
        (("friendly", "amichevole", "friendlies"), "amichevole"),
    )
    for needles, family in mappings:
        if any(needle in value for needle in needles):
            return family
    return value or "altra-competizione"


def _is_juventus(team: str) -> bool:
    return _normalize(team).replace("-", " ") in JUVENTUS_ALIASES


def _team_match_key(team: str) -> str:
    if _is_juventus(team):
        return "juventus"
    tokens = _normalize(team).split("-")
    ignored = {"afc", "cf", "fc", "football", "club"}
    key = "-".join(token for token in tokens if token not in ignored)
    return TEAM_EQUIVALENTS.get(key, key)


def _is_official_international_match(event: dict[str, Any]) -> bool:
    if str(event.get("event_kind") or "match") != "match":
        return False
    searchable = _normalize(
        " ".join(
            str(event.get(key) or "")
            for key in ("competition", "round", "title", "organizer", "source_url")
        )
    )
    if any(marker in searchable for marker in ("friendly", "friendlies", "amichevole", "tournee", "pre-season")):
        return False
    family = _competition_family(str(event.get("competition") or ""))
    if family in INTERNATIONAL_COMPETITION_FAMILIES:
        return True
    if family in {"serie-a", "serie-b", "coppa-italia", "supercoppa-italiana", "amichevole"}:
        return False
    return any(
        marker in searchable
        for marker in ("uefa", "fifa", "conmebol", "intercontinental", "international-club", "world-club")
    )


def load_broadcast_sources(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path, {"sources": []})
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    if not isinstance(sources, list):
        raise ValueError("data/broadcast_sources.json deve contenere una lista 'sources'")
    required = {"country", "country_code", "broadcaster", "access", "url"}
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or required - source.keys():
            raise ValueError(f"Fonte broadcast #{index + 1} non valida")
        if source.get("access") not in {"free", "included", "paid", "unknown"}:
            raise ValueError(f"Fonte broadcast #{index + 1}: access non valido")
        item = deepcopy(source)
        tiers = payload.get("market_tiers") or {}
        country_code = str(item.get("country_code") or "").upper()
        if not item.get("market_tier"):
            for tier, countries in tiers.items():
                if country_code in {str(code).upper() for code in countries or []}:
                    item["market_tier"] = int(tier)
                    break
        normalized.append(item)
    return normalized


def _fixture_date_markers(
    value: datetime, timezone_name: str = "Europe/Rome"
) -> set[str]:
    try:
        local = value.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        local = value.astimezone(ROME)
    markers = {
        local.strftime("%Y-%m-%d"),
        local.strftime("%Y%m%d"),
        local.strftime("%d-%m-%Y"),
        local.strftime("%d-%m"),
        local.strftime("%d.%m.%Y"),
        local.strftime("%d/%m/%Y"),
    }
    for month in MONTH_NAMES[local.month]:
        markers.add(_normalize(f"{local.day} {month} {local.year}"))
        markers.add(_normalize(f"{local.day} {month}"))
    return markers


def _fixture_time_markers(
    value: datetime, timezone_name: str = "Europe/Rome"
) -> set[str]:
    """Return normalised local kick-off spellings for match-specific pages."""
    try:
        local = value.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        local = value.astimezone(ROME)
    return {
        _normalize(local.strftime("%H:%M")),
        _normalize(f"{local.hour}:{local.minute:02d}"),
        _normalize(local.strftime("%H.%M")),
    }


def _team_broadcast_aliases(
    team: str, configured_aliases: dict[str, Any] | None = None
) -> set[str]:
    key = _team_match_key(team)
    aliases = {key}
    for canonical, values in (configured_aliases or {}).items():
        normalized_values = {_team_match_key(str(value)) for value in values or []}
        canonical_key = _team_match_key(str(canonical))
        if key == canonical_key or key in normalized_values:
            aliases.update(normalized_values)
            aliases.add(canonical_key)
    return aliases


def _fixture_windows(
    html: str,
    event: dict[str, Any],
    timezone_name: str = "Europe/Rome",
    team_aliases: dict[str, Any] | None = None,
) -> list[str]:
    text = _normalize(html_module.unescape(re.sub(r"<[^>]+>", " ", html)))
    ignored = {"club", "calcio", "football", "futbol", "sporting", "team"}
    home_tokens = {
        token
        for alias in _team_broadcast_aliases(
            str(event.get("home_team") or ""), team_aliases
        )
        for token in alias.split("-")
        if len(token) >= 3 and token not in ignored
    }
    away_tokens = {
        token
        for alias in _team_broadcast_aliases(
            str(event.get("away_team") or ""), team_aliases
        )
        for token in alias.split("-")
        if len(token) >= 3 and token not in ignored
    }
    if not home_tokens or not away_tokens:
        return []
    date_markers = _fixture_date_markers(_event_datetime(event), timezone_name)
    windows: list[str] = []
    anchors, counterparts = (
        (away_tokens, home_tokens)
        if max(map(len, away_tokens)) >= max(map(len, home_tokens))
        else (home_tokens, away_tokens)
    )
    for anchor in sorted(anchors, key=len, reverse=True):
        for match in re.finditer(re.escape(anchor), text):
            window = text[max(0, match.start() - 900) : match.end() + 900]
            if (
                any(token in window for token in counterparts)
                and any(marker in window for marker in date_markers)
                and window not in windows
            ):
                windows.append(window)
    return windows


def page_confirms_fixture(
    html: str,
    event: dict[str, Any],
    broadcaster: str,
    timezone_name: str = "Europe/Rome",
    require_kickoff_time: bool = False,
    team_aliases: dict[str, Any] | None = None,
) -> bool:
    """Require teams, exact date and viewing language in one nearby page fragment."""
    broadcaster_marker = _normalize(broadcaster)
    time_markers = _fixture_time_markers(_event_datetime(event), timezone_name)
    for window in _fixture_windows(html, event, timezone_name, team_aliases):
        if require_kickoff_time and not any(marker in window for marker in time_markers):
            continue
        has_viewing_evidence = any(marker in window for marker in BROADCAST_EVIDENCE_MARKERS)
        distinctive_broadcaster_tokens = [
            token
            for token in broadcaster_marker.split("-")
            if len(token) >= 3 and token not in {"sport", "sports", "video", "tv", "play"}
        ]
        if has_viewing_evidence and (
            broadcaster_marker in window
            or any(token in window for token in distinctive_broadcaster_tokens)
        ):
            return True
    return False


def parse_servus_epg(html: str) -> list[dict[str, Any]]:
    """Extract the official server-rendered ServusTV EPG cards."""
    pattern = re.compile(
        r'\\"title\\":\\"(?P<title>(?:[^\\]|\\.)*?)\\"(?:(?!\},\{).)*?'
        r'\\"start_time\\":\\"(?P<start>[^\\"]+)\\"(?:(?!\},\{).)*?'
        r'\\"end_time\\":\\"(?P<end>[^\\"]+)\\"',
        re.DOTALL,
    )
    rows: list[dict[str, Any]] = []
    for match in pattern.finditer(html):
        try:
            title = json.loads('"' + match["title"] + '"')
        except json.JSONDecodeError:
            title = html_module.unescape(match["title"].replace('\\"', '"'))
        rows.append(
            {"title": title, "start": match["start"], "end": match["end"]}
        )
    return rows


def parse_tv8_epg(payload: dict[str, Any], event_date: date) -> list[dict[str, Any]]:
    """Normalise TV8's structured schedule into dated programme intervals."""
    rows: list[dict[str, Any]] = []
    range_pattern = re.compile(r"^(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})$")
    for programme in payload.get("programs") or []:
        badge = str(
            ((programme.get("badge") or {}).get("label") or {}).get("text") or ""
        )
        match = range_pattern.match(badge)
        if not match:
            continue
        start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
        start = datetime.combine(event_date, time(start_hour, start_minute), ROME)
        end = datetime.combine(event_date, time(end_hour, end_minute), ROME)
        if end <= start:
            end += timedelta(days=1)
        title = str(((programme.get("title") or {}).get("text")) or "")
        description = str(((programme.get("description") or {}).get("text")) or "")
        rows.append(
            {
                "title": " ".join((title, description)).strip(),
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
    return rows


def _programme_confirms_fixture(
    programme: dict[str, Any], event: dict[str, Any]
) -> datetime | None:
    """Accept only a live-sized EPG block containing both clubs around kick-off."""
    title = _normalize(str(programme.get("title") or ""))
    if "juventus" not in title:
        return None
    opponent = (
        str(event.get("away_team") or "")
        if _is_juventus(str(event.get("home_team") or ""))
        else str(event.get("home_team") or "")
    )
    opponent_tokens = {
        token
        for token in _team_match_key(opponent).split("-")
        if len(token) >= 4 and token not in {"club", "calcio"}
    }
    if not opponent_tokens or not any(token in title for token in opponent_tokens):
        return None
    try:
        programme_start = datetime.fromisoformat(
            str(programme["start"]).replace("Z", "+00:00")
        ).astimezone(ROME)
        programme_end = datetime.fromisoformat(
            str(programme["end"]).replace("Z", "+00:00")
        ).astimezone(ROME)
    except (KeyError, TypeError, ValueError):
        return None
    kick_off = _event_datetime(event).astimezone(ROME)
    if programme_start.date() != kick_off.date():
        return None
    if not kick_off - timedelta(hours=2) <= programme_start <= kick_off + timedelta(minutes=10):
        return None
    # Requiring a long block rejects news, previews and highlights near kick-off.
    if programme_end < kick_off + timedelta(minutes=75):
        return None
    return programme_start


def _fixture_source_url(source: dict[str, Any], event: dict[str, Any]) -> str:
    family = _competition_family(str(event.get("competition") or ""))
    templates = source.get("url_templates") or {}
    template = str(templates.get(family) or source.get("url_template") or source["url"])
    slugs = source.get("team_slugs") or {}
    home = _team_match_key(str(event.get("home_team") or ""))
    away = _team_match_key(str(event.get("away_team") or ""))
    event_date = _event_datetime(event).date().isoformat()
    values = {
        "home": str(slugs.get(home) or home),
        "away": str(slugs.get(away) or away),
        "date": event_date,
    }
    return template.format(**values)


def _verified_official_url(source: dict[str, Any], field: str) -> str | None:
    """Accept configured user links only when they use an allow-listed official host."""
    value = str(source.get(field) or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    allowed = {
        str(domain).lower().lstrip(".")
        for domain in source.get("official_domains") or []
    }
    if parsed.scheme != "https" or not host or not allowed:
        return None
    if not any(host == domain or host.endswith(f".{domain}") for domain in allowed):
        return None
    return value


def _source_with_page_channels(source: dict[str, Any], html: str) -> dict[str, Any]:
    """Refine a confirmed source with channels explicitly named on its page."""
    channel_pattern = str(source.get("channel_pattern") or "")
    if not channel_pattern:
        return source
    visible = html_module.unescape(re.sub(r"<[^>]+>", " ", html))
    channels = list(
        dict.fromkeys(
            match.group(0)
            for match in re.finditer(channel_pattern, visible, re.IGNORECASE)
        )
    )
    normalized_visible = visible.lower()
    for additional in source.get("additional_channels") or []:
        marker = str(additional.get("marker") or "").lower()
        label = str(additional.get("label") or "").strip()
        if marker and label and marker in normalized_visible:
            channels.append(label)
    if not channels:
        return source
    refined = deepcopy(source)
    refined["broadcaster"] = " / ".join(channels)
    return refined


def _fetch_epg_rows(
    requester: Any,
    source: dict[str, Any],
    relevant: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    source_type = str(source.get("source_type") or "page")
    url = str(source.get("endpoint") or source["url"])
    if source_type == "servus_epg":
        response = requester.get(url, timeout=10)
        response.raise_for_status()
        return parse_servus_epg(response.text), url
    if source_type == "tv8_api":
        rows: list[dict[str, Any]] = []
        for event_date in sorted({_event_datetime(event).astimezone(ROME).date() for event in relevant}):
            local_start = datetime.combine(event_date, time.min, ROME)
            local_end = datetime.combine(event_date, time.max, ROME)
            query = urlencode(
                {
                    "from": local_start.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "to": local_end.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                }
            )
            response = requester.get(f"{url}?{query}", timeout=10)
            response.raise_for_status()
            rows.extend(parse_tv8_epg(response.json(), event_date))
        return rows, str(source.get("public_url") or source["url"])
    raise ValueError(f"tipo palinsesto non supportato: {source_type}")


def next_international_match(
    events: list[dict[str, Any]], now: datetime
) -> dict[str, Any] | None:
    """Return only the next future official international Juventus fixture."""
    future = [
        event
        for event in events
        if _is_official_international_match(event)
        and _event_datetime(event).astimezone(timezone.utc) > now.astimezone(timezone.utc)
        and _normalize(str(event.get("status") or ""))
        not in {"played", "final", "full-time", "ft", "completed", "status-final"}
    ]
    return min(future, key=_event_datetime) if future else None


def _opponent_name(event: dict[str, Any]) -> str:
    return (
        str(event.get("away_team") or "")
        if _is_juventus(str(event.get("home_team") or ""))
        else str(event.get("home_team") or "")
    )


def _country_from_registry(
    opponent: str, sources: list[dict[str, Any]]
) -> dict[str, str] | None:
    opponent_key = _team_match_key(opponent)
    if opponent_key in TEAM_COUNTRY_OVERRIDES:
        country, country_code = TEAM_COUNTRY_OVERRIDES[opponent_key]
        return {
            "country": country,
            "country_code": country_code,
            "source": "registro squadre",
        }
    for source in sources:
        if str(source.get("country_code") or "").upper() == "IT":
            continue
        if any(
            _normalize(str(keyword)) in opponent_key
            for keyword in source.get("team_keywords") or []
        ):
            return {
                "country": str(source["country"]),
                "country_code": str(source["country_code"]).upper(),
                "source": "broadcast source registry",
            }
    return None


def identify_opponent_country(
    requester: Any,
    event: dict[str, Any],
    sources: list[dict[str, Any]],
) -> tuple[dict[str, str] | None, str | None]:
    """Resolve the opponent country through structured team data, with registry fallback."""
    opponent = _opponent_name(event)
    registered = _country_from_registry(opponent, sources)
    if registered and registered.get("source") == "registro squadre":
        return registered, None
    url = f"{TEAM_COUNTRY_LOOKUP_URL}?{urlencode({'t': opponent})}"
    try:
        response = requester.get(url, timeout=10)
        response.raise_for_status()
        teams = response.json().get("teams") or []
        opponent_key = _team_match_key(opponent)
        ordered = sorted(
            teams,
            key=lambda team: _team_match_key(str(team.get("strTeam") or ""))
            != opponent_key,
        )
        for team in ordered:
            country_name = str(team.get("strCountry") or "").strip()
            if not country_name:
                continue
            country_key = _normalize(country_name)
            for source in sources:
                aliases = {
                    _normalize(str(source.get("country") or "")),
                    *(
                        _normalize(str(alias))
                        for alias in source.get("country_aliases") or []
                    ),
                }
                if country_key in aliases:
                    return {
                        "country": str(source["country"]),
                        "country_code": str(source["country_code"]).upper(),
                        "source": "TheSportsDB team lookup",
                        "source_url": url,
                    }, None
    except (
        requests.RequestException,
        ValueError,
        json.JSONDecodeError,
        AttributeError,
        TypeError,
    ) as exc:
        fallback = _country_from_registry(opponent, sources)
        return fallback, f"Paese avversaria ({opponent}): {exc}"
    fallback = _country_from_registry(opponent, sources)
    return fallback, None if fallback else f"Paese avversaria non identificato: {opponent}"


def _guide_broadcasters(
    html: str,
    event: dict[str, Any],
    broadcaster_sources: list[dict[str, Any]],
    timezone_name: str,
    require_kickoff_time: bool = False,
    team_aliases: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover known broadcasters named next to the exact fixture in a TV guide."""
    windows = _fixture_windows(html, event, timezone_name, team_aliases)
    windows = [window for window in windows if "canal-a-confirmar" not in window]
    if require_kickoff_time:
        time_markers = _fixture_time_markers(_event_datetime(event), timezone_name)
        windows = [
            window for window in windows
            if any(marker in window for marker in time_markers)
        ]
    if not windows:
        return []
    found: list[dict[str, Any]] = []
    for broadcaster in broadcaster_sources:
        marker = _normalize(str(broadcaster.get("broadcaster") or ""))
        terms = {
            marker,
            *(
                _normalize(str(term))
                for term in broadcaster.get("match_terms") or []
            ),
        }
        if any(term and term in window for term in terms for window in windows):
            found.append(broadcaster)
    return found


def _fetch_guide_documents(
    requester: Any,
    source: dict[str, Any],
    event: dict[str, Any],
) -> list[tuple[str, str]]:
    """Fetch one guide page or the first useful pages from a WordPress search."""
    url = _fixture_source_url(source, event)
    response = requester.get(url, timeout=10)
    response.raise_for_status()
    if str(source.get("source_type")) != "wordpress_search":
        return [(response.text, url)]

    results = response.json()
    if not isinstance(results, list):
        raise ValueError("risposta ricerca WordPress non valida")
    documents: list[tuple[str, str]] = []
    for item in results[:5]:
        article_url = str(item.get("url") or "") if isinstance(item, dict) else ""
        if not article_url:
            continue
        try:
            article = requester.get(article_url, timeout=10)
            article.raise_for_status()
        except requests.RequestException:
            continue
        documents.append((article.text, article_url))
    return documents


def _walk_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_objects(child)


def parse_structured_broadcast_guide(
    html: str,
    event: dict[str, Any],
    team_aliases: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover match-specific broadcasters from schema.org JSON-LD."""
    expected_start = _event_datetime(event).astimezone(timezone.utc)
    home_aliases = _team_broadcast_aliases(
        str(event.get("home_team") or ""), team_aliases
    )
    away_aliases = _team_broadcast_aliases(
        str(event.get("away_team") or ""), team_aliases
    )
    discovered: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for raw in scripts:
        try:
            payload = json.loads(html_module.unescape(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_json_objects(payload):
            if item.get("@type") != "BroadcastEvent":
                continue
            fixture = item.get("broadcastOfEvent") or {}
            if not isinstance(fixture, dict):
                continue
            home = _team_match_key(str((fixture.get("homeTeam") or {}).get("name") or ""))
            away = _team_match_key(str((fixture.get("awayTeam") or {}).get("name") or ""))
            if home not in home_aliases or away not in away_aliases:
                continue
            try:
                listed_start = datetime.fromisoformat(
                    str(fixture.get("startDate") or item.get("startDate") or "")
                    .replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError:
                continue
            if abs((listed_start - expected_start).total_seconds()) > 15 * 60:
                continue
            services = item.get("publishedOn") or []
            if isinstance(services, dict):
                services = [services]
            for service in services:
                if not isinstance(service, dict):
                    continue
                name = str(service.get("name") or "").strip()
                source_url = str(service.get("sameAs") or "").strip()
                if not name:
                    continue
                key = (_normalize(name), source_url)
                if key in seen:
                    continue
                seen.add(key)
                area = service.get("areaServed") or {}
                country = str(area.get("name") or "") if isinstance(area, dict) else ""
                discovered.append(
                    {
                        "broadcaster": name,
                        "source_url": source_url,
                        "country": country,
                        "programme_start": listed_start.isoformat(),
                    }
                )
    return discovered


def score_broadcast_evidence(
    evidence: list[dict[str, Any]], rights_confirmed: bool = False
) -> int:
    """Score independent evidence without turning generic rights into match proof."""
    types = {str(item.get("source_type") or "") for item in evidence}
    if "official_match" in types:
        return 100
    if "official_epg" in types:
        return 95
    guide_sources = {
        str(item.get("source_url") or "")
        for item in evidence
        if item.get("source_type") == "guide"
    }
    if len(guide_sources) >= 2:
        return 90
    if guide_sources and rights_confirmed:
        # Generic territorial rights add context, but do not confirm that this
        # particular fixture was selected by the broadcaster.
        return 75
    if guide_sources:
        return 70
    if rights_confirmed:
        return 60
    return 0


def classify_broadcast_candidate(
    access: str,
    confidence: int,
    *,
    rights_confirmed: bool,
    match_confirmed: bool,
) -> str:
    """Keep territorial rights separate from proof of the individual fixture."""
    if match_confirmed and confidence >= BROADCAST_CONFIDENCE_THRESHOLD:
        if access == "free":
            return "CONFIRMED_FREE"
        if access in {"paid", "included"}:
            return "CONFIRMED_PAY"
    if access == "free" and rights_confirmed:
        return "POSSIBLE_FREE"
    return "UNKNOWN"


def _broadcast_season(event: dict[str, Any]) -> tuple[int, str]:
    explicit = str(event.get("season") or "").strip()
    range_match = re.search(r"(20\d{2})\s*[/\-]\s*(?:20)?(\d{2})", explicit)
    if range_match:
        start = int(range_match.group(1))
        return start, f"{start}/{range_match.group(2)}"
    year_match = re.fullmatch(r"20\d{2}", explicit)
    if year_match:
        start = int(explicit)
        return start, explicit
    start = season_start(_event_datetime(event).astimezone(ROME).date())
    return start, f"{start}/{str(start + 1)[-2:]}"


def _source_temporal_scope(
    source: dict[str, Any], event: dict[str, Any]
) -> tuple[bool, bool]:
    """Return (source active, generic rights are season/date bounded)."""
    event_start = _event_datetime(event).astimezone(timezone.utc)
    event_season, _ = _broadcast_season(event)
    from_season = source.get("rights_from_season")
    through_season = source.get("rights_through_season")
    valid_from = source.get("rights_valid_from")
    valid_to = source.get("rights_valid_to")
    has_temporal_bounds = any(
        value not in (None, "")
        for value in (from_season, through_season, valid_from, valid_to)
    )
    if from_season not in (None, "") and event_season < int(from_season):
        return False, has_temporal_bounds
    if through_season not in (None, "") and event_season > int(through_season):
        return False, has_temporal_bounds
    for raw, is_start in ((valid_from, True), (valid_to, False)):
        if raw in (None, ""):
            continue
        try:
            boundary = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=timezone.utc)
            boundary = boundary.astimezone(timezone.utc)
        except ValueError:
            return True, False
        if is_start and event_start < boundary:
            return False, has_temporal_bounds
        if not is_start and event_start > boundary:
            return False, has_temporal_bounds
    return True, has_temporal_bounds


def _broadcast_cache_keys(
    event: dict[str, Any], country_code: str
) -> tuple[str, str]:
    _, season_key = _broadcast_season(event)
    competition = _competition_family(str(event.get("competition") or ""))
    date_key = _event_datetime(event).astimezone(ROME).date().isoformat()
    teams = "|".join(
        (
            _team_match_key(str(event.get("home_team") or "")),
            _team_match_key(str(event.get("away_team") or "")),
        )
    )
    return (
        f"{competition}|{season_key}|{country_code.upper()}",
        f"{competition}|{season_key}|{date_key}|{teams}",
    )


def apply_verified_broadcasts(
    session: requests.Session,
    events: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    checked_at: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Verify broadcasters for the next international fixture only."""
    result = deepcopy(events)
    target = next_international_match(result, now)
    if target is None:
        return result, []

    # This first rollout is deliberately scoped to one fixture. Fresh remote data
    # does not contain our enrichment fields, so retain them for every other event
    # instead of making unrelated calendar entries look changed.
    for event in result:
        if event is target:
            continue
        previous_event = next(
            (item for item in previous if _same_long_range_fixture(item, event)), None
        ) or next(
            (item for item in previous if _same_fixture(item, event, unordered=True)),
            None,
        )
        if not previous_event:
            continue
        for key, value in previous_event.items():
            if key.startswith("broadcast_") or key.startswith("opponent_country"):
                event[key] = deepcopy(value)

    errors: list[str] = []
    evidence_by_broadcaster: dict[tuple[str, str], dict[str, Any]] = {}
    failed_broadcasters: set[tuple[str, str]] = set()
    optional_session: requests.Session | None = None
    requester: Any = session
    if isinstance(session, requests.Session):
        optional_session = requests.Session()
        optional_session.headers.update(session.headers)
        requester = optional_session
    try:
        opponent_country, country_error = identify_opponent_country(
            requester, target, sources
        )
        if country_error:
            errors.append(country_error)
        if opponent_country:
            target["opponent_country"] = opponent_country["country"]
            target["opponent_country_code"] = opponent_country["country_code"]
            target["opponent_country_source"] = opponent_country["source"]
            if opponent_country.get("source_url"):
                target["opponent_country_source_url"] = opponent_country["source_url"]

        opponent_code = str((opponent_country or {}).get("country_code") or "").upper()
        configured_countries = {
            str(source.get("country_code") or "").upper() for source in sources
        }
        # The registry is intentionally finite. Query every configured free source,
        # while foreign pay sources are useful only for the opponent's market.
        allowed_countries = {
            country for country in configured_countries
            if country == "IT"
            or country == opponent_code
            or any(
                str(source.get("country_code") or "").upper() == country
                and (
                    source.get("access") == "free"
                    or str(source.get("source_type") or "") in BROADCAST_GUIDE_SOURCE_TYPES
                )
                for source in sources
            )
        }
        broadcaster_sources = [
            source
            for source in sources
            if str(source.get("source_type") or "page") not in BROADCAST_GUIDE_SOURCE_TYPES
            and str(source.get("country_code") or "").upper() in allowed_countries
        ]

        def add_evidence(
            broadcaster: dict[str, Any], item: dict[str, Any]
        ) -> None:
            key = (
                str(broadcaster["country_code"]).upper(),
                _normalize(str(broadcaster["broadcaster"])),
            )
            bucket = evidence_by_broadcaster.setdefault(
                key, {"source": deepcopy(broadcaster), "evidence": []}
            )
            if not any(
                existing.get("source_url") == item.get("source_url")
                and existing.get("source_type") == item.get("source_type")
                for existing in bucket["evidence"]
            ):
                bucket["evidence"].append(item)

        ordered_sources = sorted(
            sources,
            key=lambda item: (
                int(item.get("market_tier") or 3),
                -int(item.get("priority") or 0),
            ),
        )
        for source in ordered_sources:
            country_code = str(source.get("country_code") or "").upper()
            if country_code not in allowed_countries:
                continue
            competition_families = {
                _normalize(str(item)) for item in source.get("competition_families") or []
            }
            horizon = int(source.get("lookahead_days") or BROADCAST_GUIDE_HORIZON_DAYS)
            event_date = _event_datetime(target).astimezone(ROME).date()
            source_active, rights_temporally_valid = _source_temporal_scope(
                source, target
            )
            if (
                event_date > now.astimezone(ROME).date() + timedelta(days=horizon)
                or not source_active
                or (
                    competition_families
                    and _competition_family(str(target.get("competition") or ""))
                    not in competition_families
                )
            ):
                continue
            source_type = str(source.get("source_type") or "page")
            source_key = (country_code, _normalize(str(source["broadcaster"])))
            if (
                source.get("rights_confirmed")
                and rights_temporally_valid
                and source_type not in BROADCAST_GUIDE_SOURCE_TYPES
            ):
                evidence_by_broadcaster.setdefault(
                    source_key, {"source": deepcopy(source), "evidence": []}
                )
            if source_type in {"servus_epg", "tv8_api"}:
                try:
                    programmes, guide_url = _fetch_epg_rows(
                        requester, source, [target]
                    )
                except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"{source['broadcaster']}: {exc}")
                    failed_broadcasters.add(source_key)
                    continue
                starts = [
                    start
                    for programme in programmes
                    if (start := _programme_confirms_fixture(programme, target))
                ]
                if starts:
                    add_evidence(
                        source,
                        {
                            "source_type": "official_epg",
                            "source_url": guide_url,
                            "verified_at": checked_at,
                            "programme_start": min(starts).isoformat(),
                        },
                    )
                continue

            if source_type in BROADCAST_GUIDE_SOURCE_TYPES:
                try:
                    documents = _fetch_guide_documents(requester, source, target)
                except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"{source['broadcaster']} ({target.get('title')}): {exc}")
                    continue
                timezone_name = str(source.get("timezone") or "Europe/Rome")
                for html, document_url in documents:
                    if source.get("discover_broadcasters"):
                        for discovered in parse_structured_broadcast_guide(
                            html, target, source.get("team_aliases") or {}
                        ):
                            discovered_name = _normalize(discovered["broadcaster"])
                            known = next(
                                (
                                    item for item in broadcaster_sources
                                    if discovered_name
                                    in {
                                        _normalize(str(item.get("broadcaster") or "")),
                                        *(
                                            _normalize(str(term))
                                            for term in item.get("match_terms") or []
                                        ),
                                    }
                                ),
                                None,
                            )
                            broadcaster = known or {
                                "country": discovered.get("country") or "Da determinare",
                                "country_code": "ZZ",
                                "broadcaster": discovered["broadcaster"],
                                "access": "unknown",
                                "platforms": "TV/streaming da verificare",
                                "url": discovered.get("source_url") or document_url,
                                "priority": 0,
                                "market_tier": 3,
                                "discovered_dynamically": True,
                            }
                            add_evidence(
                                broadcaster,
                                {
                                    "source_type": "guide",
                                    "source_url": document_url,
                                    "broadcaster_page": discovered.get("source_url"),
                                    "verified_at": checked_at,
                                    "guide": str(source["broadcaster"]),
                                    "programme_start": discovered.get("programme_start"),
                                },
                            )
                    for broadcaster in _guide_broadcasters(
                        html,
                        target,
                        [
                            item for item in broadcaster_sources
                            if source.get("country_scope") == "*"
                            or str(item.get("country_code") or "").upper() == country_code
                        ],
                        timezone_name,
                        bool(source.get("require_kickoff_time")),
                        source.get("team_aliases") or {},
                    ):
                        add_evidence(
                            broadcaster,
                            {
                                "source_type": "guide",
                                "source_url": document_url,
                                "verified_at": checked_at,
                                "guide": str(source["broadcaster"]),
                            },
                        )
                continue

            url = _fixture_source_url(source, target)
            try:
                response = requester.get(url, timeout=10)
                response.raise_for_status()
            except requests.RequestException as exc:
                errors.append(f"{source['broadcaster']} ({target.get('title')}): {exc}")
                failed_broadcasters.add(source_key)
                continue

            timezone_name = str(source.get("timezone") or "Europe/Rome")
            if page_confirms_fixture(
                response.text,
                target,
                str(source["broadcaster"]),
                timezone_name,
                bool(source.get("require_kickoff_time")),
                source.get("team_aliases") or {},
            ):
                refined = _source_with_page_channels(source, response.text)
                add_evidence(
                    refined,
                    {
                        "source_type": "official_match",
                        "source_url": url,
                        "verified_at": checked_at,
                    },
                )
    finally:
        if optional_session is not None:
            optional_session.close()

    previous_target = next(
        (item for item in previous if _same_long_range_fixture(item, target)), None
    ) or next(
        (item for item in previous if _same_fixture(item, target, unordered=True)), None
    )
    candidates: list[dict[str, Any]] = []
    for bucket in evidence_by_broadcaster.values():
        source = bucket["source"]
        evidence = bucket["evidence"]
        _, rights_temporally_valid = _source_temporal_scope(source, target)
        rights_confirmed = bool(
            source.get("rights_confirmed") and rights_temporally_valid
        )
        match_confirmed = bool(evidence)
        confidence = score_broadcast_evidence(evidence, rights_confirmed)
        best = (
            max(
                evidence,
                key=lambda item: {
                    "official_match": 3, "official_epg": 2, "guide": 1
                }.get(str(item.get("source_type")), 0),
            )
            if evidence else {}
        )
        access = str(source["access"])
        status = classify_broadcast_candidate(
            access,
            confidence,
            rights_confirmed=rights_confirmed,
            match_confirmed=match_confirmed,
        )
        watch_url = _verified_official_url(source, "watch_url")
        broadcaster_home_url = _verified_official_url(
            source, "broadcaster_home_url"
        )
        source_schedule = str(best.get("source_url") or "") or None
        season_start_year, season_key = _broadcast_season(target)
        rights_cycle = str(source.get("rights_cycle") or "") or None
        if rights_cycle is None and source.get("rights_from_season") not in (None, ""):
            first = int(source["rights_from_season"])
            last = int(source.get("rights_through_season") or first)
            rights_cycle = (
                f"{first}/{str(first + 1)[-2:]}"
                if first == last
                else f"{first}/{str(first + 1)[-2:]}-{last}/{str(last + 1)[-2:]}"
            )
        rights_cache_key, fixture_cache_key = _broadcast_cache_keys(
            target, str(source["country_code"])
        )
        candidate = {
            "match": str(
                target.get("title")
                or f"{target.get('home_team', '')} - {target.get('away_team', '')}"
            ),
            "competition": str(target.get("competition") or ""),
            "date": _event_datetime(target).astimezone(ROME).date().isoformat(),
            "season": season_key,
            "season_start": season_start_year,
            "rights_cycle": rights_cycle,
            "rights_valid_from": source.get("rights_valid_from"),
            "rights_valid_to": source.get("rights_valid_to"),
            "rights_cache_key": rights_cache_key,
            "fixture_cache_key": fixture_cache_key,
            "country": str(source["country"]),
            "country_code": str(source["country_code"]).upper(),
            "broadcaster": str(source["broadcaster"]),
            "source_type": str(best.get("source_type") or "rights"),
            "source_url": str(
                source_schedule or source.get("rights_source_url") or source["url"]
            ),
            "source_rights": str(source.get("rights_source_url") or "") or None,
            "source_schedule": source_schedule,
            "evidence": evidence,
            "verified_at": checked_at,
            "confidence": confidence,
            "status": status,
            "rights_confirmed": rights_confirmed,
            "match_confirmed": match_confirmed,
            "broadcast_free": bool(source.get("broadcast_free", access == "free")),
            "web_stream_free": source.get("web_stream_free", "unknown"),
            "reason": (
                "exact fixture found in match-specific schedule"
                if match_confirmed
                else "competition rights found; no match-specific confirmation"
            ),
            "free_or_pay": access,
            "language": str(source.get("language") or ""),
            "platforms": str(source.get("platforms") or "streaming"),
            "requires_registration": bool(source.get("registration_required")),
            "registration": (
                "required" if source.get("registration_required") else "not_required"
            ),
            "watch_url": watch_url,
            "watch_url_type": (
                str(source.get("watch_url_type") or "unknown") if watch_url else None
            ),
            "broadcaster_home_url": broadcaster_home_url,
            "url": str(
                watch_url
                or broadcaster_home_url
                or source.get("public_url")
                or source_schedule
                or source["url"]
            ),
            "priority": int(source.get("priority") or 0),
            "market_tier": int(source.get("market_tier") or 3),
            "italian_primary": bool(source.get("italian_primary")),
            "discovered_dynamically": bool(source.get("discovered_dynamically")),
        }
        if best.get("programme_start"):
            start = datetime.fromisoformat(str(best["programme_start"]))
            candidate["broadcast_start"] = start.astimezone(ROME).isoformat()
            candidate["broadcast_start_rome"] = start.astimezone(ROME).strftime("%H:%M")
        candidates.append(candidate)

    def without_verification_time(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_verification_time(item)
                for key, item in value.items()
                if key != "verified_at"
            }
        if isinstance(value, list):
            return [without_verification_time(item) for item in value]
        return value

    if previous_target:
        previous_candidates = previous_target.get("broadcast_candidates") or []
        stable_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            old_candidate = next(
                (
                    item
                    for item in previous_candidates
                    if item.get("country_code") == candidate.get("country_code")
                    and item.get("broadcaster") == candidate.get("broadcaster")
                    and without_verification_time(item)
                    == without_verification_time(candidate)
                ),
                None,
            )
            stable_candidates.append(deepcopy(old_candidate or candidate))
        candidates = stable_candidates

        by_key = {
            (
                str(item.get("country_code") or ""),
                _normalize(str(item.get("broadcaster") or "")),
            ): item
            for item in candidates
        }
        for old_candidate in previous_candidates:
            key = (
                str(old_candidate.get("country_code") or ""),
                _normalize(str(old_candidate.get("broadcaster") or "")),
            )
            if key not in failed_broadcasters:
                continue
            current = by_key.get(key)
            if old_candidate.get("match_confirmed") and not (
                current and current.get("match_confirmed")
            ):
                if current in candidates:
                    candidates.remove(current)
                candidates.append(deepcopy(old_candidate))

    _, target_fixture_cache_key = _broadcast_cache_keys(target, "")

    def same_fixture_cache(item: dict[str, Any]) -> bool:
        cached = str(item.get("fixture_cache_key") or "")
        return not cached or cached == target_fixture_cache_key

    if not candidates and previous_target and errors:
        candidates = [
            deepcopy(item)
            for item in previous_target.get("broadcast_candidates") or []
            if same_fixture_cache(item)
        ]
    target["broadcast_candidates"] = candidates

    access_order = {"free": 0, "included": 1, "paid": 2}
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("status") in {"CONFIRMED_FREE", "CONFIRMED_PAY"}
        and int(candidate.get("confidence") or 0) >= BROADCAST_CONFIDENCE_THRESHOLD
    ]
    italian_candidates = sorted(
        (item for item in eligible if item["country_code"] == "IT"),
        key=lambda item: (
            0 if item.get("italian_primary") else 1,
            -int(item["confidence"]),
            -int(item["priority"]),
            access_order.get(str(item["free_or_pay"]), 9),
        ),
    )
    foreign_free_candidates = sorted(
        (
            item for item in eligible
            if item["country_code"] != "IT" and item["free_or_pay"] == "free"
        ),
        key=lambda item: (-int(item["confidence"]), -int(item["priority"])),
    )[:3]

    def candidate_option(candidate: dict[str, Any]) -> dict[str, Any]:
        option = {
            "country": candidate["country"],
            "country_code": candidate["country_code"],
            "broadcaster": candidate["broadcaster"],
            "access": candidate["free_or_pay"],
            "platforms": candidate["platforms"],
            "language": candidate["language"],
            "registration_required": candidate["requires_registration"],
            "url": candidate["url"],
            "source_url": candidate["source_url"],
            "source_type": candidate["source_type"],
            "source_schedule": candidate.get("source_schedule"),
            "source_rights": candidate.get("source_rights"),
            "confidence": candidate["confidence"],
            "candidate_status": candidate["status"],
            "rights_confirmed": candidate["rights_confirmed"],
            "match_confirmed": candidate["match_confirmed"],
            "broadcast_free": candidate["broadcast_free"],
            "web_stream_free": candidate["web_stream_free"],
            "watch_url": candidate.get("watch_url"),
            "watch_url_type": candidate.get("watch_url_type"),
            "broadcaster_home_url": candidate.get("broadcaster_home_url"),
            "status": "confirmed",
            "broadcast_type": "diretta",
            "verified_at": candidate["verified_at"],
            "priority": candidate["priority"],
            "season": candidate.get("season"),
            "rights_cycle": candidate.get("rights_cycle"),
            "rights_cache_key": candidate.get("rights_cache_key"),
            "fixture_cache_key": candidate.get("fixture_cache_key"),
        }
        for key in ("broadcast_start", "broadcast_start_rome"):
            if candidate.get(key):
                option[key] = candidate[key]
        return option

    selected_options = [
        candidate_option(item)
        for item in italian_candidates + foreign_free_candidates
    ]
    if not selected_options and previous_target and errors:
        selected_options = [
            deepcopy(item)
            for item in previous_target.get("broadcast_options") or []
            if same_fixture_cache(item)
        ]
    target["broadcast_options"] = selected_options
    target["primary_italian_broadcast"] = (
        candidate_option(italian_candidates[0]) if italian_candidates else None
    )
    target["free_italian_broadcasts"] = [
        candidate_option(item)
        for item in italian_candidates if item.get("free_or_pay") == "free"
    ]
    target["foreign_free_broadcasts"] = [
        candidate_option(item) for item in foreign_free_candidates
    ]
    target["foreign_possible_broadcasts"] = [
        deepcopy(item)
        for item in candidates
        if item.get("country_code") != "IT"
        and item.get("status") == "POSSIBLE_FREE"
    ]
    target["broadcast_italy_tbc"] = not any(
        option.get("country_code") == "IT" for option in selected_options
    )
    target["broadcast_international_tbc"] = not bool(foreign_free_candidates)
    return result, errors


def _event_datetime(event: dict[str, Any]) -> datetime:
    raw = str(event["start"])
    if event.get("all_day"):
        return datetime.combine(date.fromisoformat(raw[:10]), time.min, tzinfo=ROME)
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=ROME)


def _configured_broadcast_default(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = load_json(BROADCAST_CONFIG_PATH, {"italian_defaults": []})
    family = _competition_family(str(event.get("competition") or ""))
    matching = []
    for item in payload.get("italian_defaults") or []:
        if _normalize(str(item.get("competition_family") or "")) != family:
            continue
        active, _ = _source_temporal_scope(item, event)
        if active:
            matching.append(item)
    return max(matching, key=lambda item: int(item.get("priority") or 0), default=None)


def _same_fixture(
    left: dict[str, Any], right: dict[str, Any], *, unordered: bool = False
) -> bool:
    if any(str(event.get("event_kind") or "match") != "match" for event in (left, right)):
        return False
    left_teams = (
        _team_match_key(str(left.get("home_team") or "")),
        _team_match_key(str(left.get("away_team") or "")),
    )
    right_teams = (
        _team_match_key(str(right.get("home_team") or "")),
        _team_match_key(str(right.get("away_team") or "")),
    )
    if (sorted(left_teams) if unordered else left_teams) != (
        sorted(right_teams) if unordered else right_teams
    ):
        return False
    left_family = _competition_family(str(left.get("competition") or ""))
    right_family = _competition_family(str(right.get("competition") or ""))
    generic = {"partita", "altra-competizione"}
    if left_family != right_family and not ({left_family, right_family} & generic):
        return False
    return abs((_event_datetime(left) - _event_datetime(right)).total_seconds()) <= 60 * 60 * 72


def _same_long_range_fixture(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Recognize a uniquely identifiable postponement even across season boundaries."""
    if any(str(event.get("event_kind") or "match") != "match" for event in (left, right)):
        return False
    left_teams = tuple(
        _team_match_key(str(left.get(key) or "")) for key in ("home_team", "away_team")
    )
    right_teams = tuple(
        _team_match_key(str(right.get(key) or "")) for key in ("home_team", "away_team")
    )
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
    return abs((_event_datetime(left) - _event_datetime(right)).total_seconds()) <= 60 * 60 * 24 * 240


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _country_flag(country_code: str) -> str:
    code = country_code.upper()
    if len(code) != 2 or not code.isalpha():
        return "🌍"
    return "".join(chr(127397 + ord(character)) for character in code)


def _broadcast_description_lines(data: dict[str, Any]) -> list[str]:
    options = data.get("broadcast_options") or []
    italian = [option for option in options if option.get("country_code") == "IT"]
    foreign = [
        option
        for option in options
        if option.get("country_code") != "IT" and option.get("access") == "free"
    ][:3]
    lines = ["Dove vederla:"]
    if not italian:
        lines.append("🇮🇹 Italia — Da confermare")
    for option in italian:
        lines.append(f"🇮🇹 Italia — {option['broadcaster']}")
        access = {
            "free": "GRATIS",
            "included": "Incluso nell'abbonamento",
            "paid": "A pagamento",
        }.get(str(option.get("access") or ""), "Da verificare")
        attributes = [access, str(option.get("platforms") or "")]
        if option.get("language"):
            attributes.append(str(option["language"]))
        if option.get("registration_required"):
            attributes.append("registrazione richiesta")
        if option.get("broadcast_start_rome"):
            attributes.append(
                f"diretta dalle {option['broadcast_start_rome']} (ora di Roma)"
            )
        lines.append(" · ".join(item for item in attributes if item))
        if option.get("watch_url"):
            lines.append(f"Guarda: {option['watch_url']}")
        elif option.get("broadcaster_home_url"):
            lines.append(f"Sito ufficiale: {option['broadcaster_home_url']}")
        elif option.get("url"):
            lines.append(str(option["url"]))
    if foreign:
        lines.append("GRATIS / IN CHIARO ALL'ESTERO:")
        for option in foreign:
            lines.append(
                f"{_country_flag(str(option.get('country_code') or ''))} "
                f"{option['country']} — {option['broadcaster']}"
            )
            access = {
                "free": "GRATIS",
                "included": "Incluso nell'abbonamento",
                "paid": "A pagamento",
            }.get(str(option.get("access") or ""), "Da verificare")
            attributes = [access, str(option.get("platforms") or "")]
            if option.get("language"):
                attributes.append(str(option["language"]))
            if option.get("registration_required"):
                attributes.append("registrazione richiesta")
            if option.get("broadcast_start_rome"):
                attributes.append(
                    f"diretta dalle {option['broadcast_start_rome']} (ora di Roma)"
                )
            lines.append(" · ".join(item for item in attributes if item))
            if option.get("watch_url"):
                lines.append(f"Guarda sul sito ufficiale: {option['watch_url']}")
            elif option.get("broadcaster_home_url"):
                lines.append(f"Sito ufficiale: {option['broadcaster_home_url']}")
            elif option.get("url"):
                lines.append(str(option["url"]))
    elif data.get("broadcast_international_tbc"):
        lines.append("🌍 In chiaro all'estero — Nessuna emittente gratuita confermata al momento")
    return lines

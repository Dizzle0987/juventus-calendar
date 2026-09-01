from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests
from icalendar import Calendar

from juventus_calendar.generator import (
    ESPN_COMPETITIONS,
    FetchResult,
    UpdateError,
    _canonical_event,
    _competition_family,
    _uid_for,
    build_ical,
    fetch_remote_events,
    find_lega_calendar_articles,
    load_calendar_events,
    load_manual_events,
    merge_calendar_events,
    merge_manual_events,
    merge_remote_events,
    parse_espn_json,
    parse_espn_pending_recoveries_json,
    parse_espn_standings_json,
    parse_lega_calendar_article,
    parse_lega_standings_json,
    parse_official_json,
    parse_official_opta_json,
    parse_schedule_html,
    parse_thesportsdb_json,
    parse_uefa_draw_html,
    update_calendar,
)


def test_all_potential_competitions_are_explicitly_monitored() -> None:
    assert ESPN_COMPETITIONS["ita.2"] == "Serie B"
    assert ESPN_COMPETITIONS["uefa.super_cup"] == "Supercoppa UEFA"
    assert ESPN_COMPETITIONS["fifa.intercontinental_cup"] == "Coppa Intercontinentale FIFA"
    assert ESPN_COMPETITIONS["global.club_challenge"] == "UEFA–CONMEBOL Club Challenge"
    assert _competition_family("Serie B") == "serie-b"
    assert _competition_family("Supercoppa UEFA") == "supercoppa-uefa"
    assert _competition_family("Coppa Intercontinentale FIFA") == "coppa-intercontinentale-fifa"
    assert (
        _competition_family("UEFA–CONMEBOL Club Challenge")
        == "uefa-conmebol-club-challenge"
    )


def test_parse_uefa_draw_prefers_exact_localtime_timestamp() -> None:
    html = """
    <html><head><title>UEFA Champions League league phase draw | 2026/27</title></head>
    <body>
      <span data-options="{&quot;targetDate&quot;:&quot;2026-08-27T16:00:00+00:00&quot;}"></span>
      <script type="application/ld+json">
      {
        "@type": "SportsEvent", "@id": "https://www.uefa.com/draws/#draw-123",
        "name": "UEFA Champions League - League phase draw",
        "startDate": "2026-08-27T15:00:00+00:00",
        "location": [{"@type": "Place", "name": "Monaco", "address": "Monaco"}]
      }
      </script>
    </body></html>
    """

    events = parse_uefa_draw_html(
        html, "UEFA Champions League", "https://www.uefa.com/uefachampionsleague/draws/"
    )

    assert len(events) == 1
    assert events[0]["source_id"] == "draw-123"
    assert events[0]["start"] == "2026-08-27T18:00:00+02:00"
    assert events[0]["title"] == "Sorteggio fase campionato UEFA Champions League 2026/27"


def test_lega_news_discovery_and_explicit_calendar_datetime() -> None:
    listing = """
      <a href="/serie-a/news/una-notizia">Notizia</a>
      <a href="/serie-a/news/sorteggio-coppa-italia-2027-28">Sorteggio</a>
    """
    urls = find_lega_calendar_articles(listing)
    assert urls == [
        "https://www.legaseriea.it/serie-a/news/sorteggio-coppa-italia-2027-28"
    ]
    article = """
      <script type="application/ld+json">
      {"@type": "NewsArticle", "headline": "Sorteggio Coppa Italia 2027/28",
       "datePublished": "2027-06-03T10:00:00Z"}
      </script>
      <p>Il sorteggio si terrà venerdì 4 giugno alle ore 18.30.</p>
    """

    events = parse_lega_calendar_article(article, urls[0])

    assert events[0]["start"] == "2027-06-04T18:30:00+02:00"
    assert events[0]["title"] == "Sorteggio Coppa Italia 2027/28"


def test_calendar_event_merge_filter_uid_and_ical(tmp_path: Path) -> None:
    path = tmp_path / "calendar_events.json"
    path.write_text(json.dumps({"events": [
        {
            "id": "ucl-draw", "title": "Sorteggio Champions League",
            "competition": "UEFA Champions League",
            "start": "2026-08-27T18:00:00+02:00",
            "source_url": "https://www.uefa.com/uefachampionsleague/draws/",
            "participation_confirmed": True,
        },
        {
            "id": "uel-draw", "title": "Sorteggio Europa League",
            "competition": "UEFA Europa League",
            "start": "2026-08-28T13:00:00+02:00",
            "source_url": "https://www.uefa.com/uefaeuropaleague/draws/",
        },
    ]}), encoding="utf-8")
    configured = load_calendar_events(path, set())
    assert [event["source_id"] for event in configured] == ["ucl-draw"]

    automatic = {**configured[0], "source": "UEFA", "source_id": "draw-123"}
    merged = merge_calendar_events([automatic], configured, configured)
    assert len(merged) == 1
    canonical = _canonical_event(merged[0], [], "2026-08-20T08:00:00Z", set())
    parsed = next(
        component for component in Calendar.from_ical(build_ical([canonical])).walk()
        if component.name == "VEVENT"
    )
    alarm = next(component for component in parsed.subcomponents if component.name == "VALARM")
    assert parsed.decoded("summary").decode().startswith("🎲")
    assert "Tipo: Sorteggio" in parsed.decoded("description").decode()
    assert "Juventus:" not in parsed.decoded("description").decode()
    assert alarm.decoded("trigger").total_seconds() == -30 * 60


def test_parse_espn_standings_json_extracts_juventus_row():
    def entry(team_id: str, name: str, rank: int, points: int) -> dict:
        return {
            "team": {"id": team_id, "displayName": name},
            "stats": [
                {"name": "rank", "value": rank},
                {"name": "points", "value": points},
                {"name": "gamesPlayed", "value": 10},
                {"name": "wins", "value": 6},
                {"name": "ties", "value": 3},
                {"name": "losses", "value": 1},
                {"name": "pointDifferential", "value": 9},
            ],
        }

    payload = {
        "children": [{
            "standings": {
                "entries": [
                    entry("1", "Inter", 1, 24),
                    entry("2", "Milan", 2, 22),
                    entry("111", "Juventus", 3, 21),
                    entry("3", "Roma", 4, 19),
                    entry("4", "Napoli", 5, 18),
                ]
            }
        }]
    }

    assert parse_espn_standings_json(payload) == {
        "position": 3,
        "points": 21,
        "played": 10,
        "wins": 6,
        "draws": 3,
        "losses": 1,
        "goal_difference": 9,
        "context": [
            {"team": "Inter", "position": 1, "points": 24, "played": 10},
            {"team": "Milan", "position": 2, "points": 22, "played": 10},
            {"team": "Juventus", "position": 3, "points": 21, "played": 10},
            {"team": "Roma", "position": 4, "points": 19, "played": 10},
            {"team": "Napoli", "position": 5, "points": 18, "played": 10},
        ],
        "provisional": False,
        "source": "ESPN",
    }


def test_parse_official_lega_standings_uses_juventus_identifiers_and_state():
    def team(name: str, rank: int, points: int, played: int = 10, *, juventus=False) -> dict:
        return {
            "teamId": (
                "serie-a::Football_Team::0ae9210dce6f4f9b9d50aeeb19b0d371"
                if juventus else f"team-{rank}"
            ),
            "providerId": "opta:Team:bqbbqm98ud8obe45ds9ohgyrd" if juventus else "",
            "shortName": name,
            "officialName": name,
            "stats": [
                {"statsId": "rank", "statsValue": rank},
                {"statsId": "points", "statsValue": points},
                {"statsId": "matches-played", "statsValue": played},
                {"statsId": "win", "statsValue": 6},
                {"statsId": "draw", "statsValue": 2},
                {"statsId": "lose", "statsValue": 2},
                {"statsId": "goal-difference", "statsValue": 8},
            ],
        }

    teams = [
        team("Inter", 1, 25),
        team("JFC", 2, 24, juventus=True),
        team("Milan", 3, 22),
        team("Roma", 4, 20),
        team("Napoli", 5, 19, played=9),
        team("Atalanta", 6, 18),
    ]
    standing = parse_lega_standings_json({"standings": [{"teams": teams}]})

    assert standing is not None
    assert standing["position"] == 2
    assert standing["goal_difference"] == 8
    assert standing["provisional"] is True
    assert standing["source"] == "Lega Serie A"
    assert [row["team"] for row in standing["context"]] == [
        "Inter", "Juventus", "Milan", "Roma", "Napoli"
    ]

    for stat in teams[4]["stats"]:
        if stat["statsId"] == "matches-played":
            stat["statsValue"] = 10
    completed = parse_lega_standings_json({"standings": [{"teams": teams}]})
    assert completed is not None
    assert completed["provisional"] is False


def test_parse_espn_pending_recoveries_json_names_postponed_matches():
    def event(home: str, away: str, status: str) -> dict:
        return {
            "status": {"type": {"name": status}},
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": home}},
                    {"homeAway": "away", "team": {"displayName": away}},
                ]
            }],
        }

    recoveries = parse_espn_pending_recoveries_json({
        "events": [
            event("SS Lazio", "Juventus FC", "STATUS_POSTPONED"),
            event("Roma", "Inter", "STATUS_SCHEDULED"),
        ]
    })

    assert recoveries == ["SS Lazio–Juventus"]


def test_espn_standings_is_used_when_official_feed_fails():
    espn_payload = {
        "standings": {"entries": [
            {
                "team": {"id": "111", "displayName": "Juventus FC"},
                "stats": [
                    {"name": "rank", "value": 3},
                    {"name": "points", "value": 6},
                    {"name": "gamesPlayed", "value": 2},
                    {"name": "pointDifferential", "value": 3},
                ],
            }
        ]}
    }

    class Response:
        text = ""

        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def get(self, url, **kwargs):
            if "apis/v2/sports/soccer/ita.1/standings" in url:
                return Response(espn_payload)
            if "api-sdp.legaseriea.it" in url:
                raise requests.ConnectionError("feed ufficiale non disponibile")
            return Response()

    fetched = fetch_remote_events(Session(), date(2026, 8, 31))

    assert fetched.serie_a_standing is not None
    assert fetched.serie_a_standing["source"] == "ESPN"
    assert "ESPN classifica" in fetched.successful_sources
    assert any("Lega Serie A classifica" in error for error in fetched.errors)


def official_match(**overrides):
    item = {
        "MatchProviderId": "official-1",
        "CompetitionName": "Serie A",
        "KickOffDateTime": "2026-09-12T18:45:00Z",
        "TeamHome": "Juventus",
        "TeamAway": "Roma",
        "Venue": "Allianz Stadium",
        "Round": "3",
        "IsFinished": False,
    }
    item.update(overrides)
    return item


def base_event(**overrides):
    item = {
        "source": "Juventus",
        "source_url": "https://www.juventus.com/match",
        "home_team": "Juventus",
        "away_team": "Roma",
        "competition": "Serie A",
        "round": "3",
        "venue": "Allianz Stadium",
        "location": "Torino",
        "start": "2026-09-12T18:45:00+00:00",
        "all_day": False,
        "status": "scheduled",
        "time_source": "Juventus",
        "time_source_url": "https://www.juventus.com/match",
    }
    item.update(overrides)
    return item


def write_manual(root: Path, events=None):
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "manual_events.json").write_text(
        json.dumps({"events": events or []}), encoding="utf-8"
    )


def test_parse_official_structured_source_and_tbc():
    payload = [
        official_match(),
        official_match(
            MatchProviderId="official-2",
            KickOffDateTime="2026-09-20T00:00:00Z",
            IgnoreTime=True,
            TeamHome="Inter",
            TeamAway="Juventus FC",
        ),
    ]
    events = parse_official_json(payload)
    assert len(events) == 2
    assert events[0]["venue"] == "Allianz Stadium"
    assert events[1]["all_day"] is True
    assert events[1]["start"] == "2026-09-20"


def test_parse_official_opta_calendar_with_tbc_time():
    payload = {
        "match": [
            {
                "matchInfo": {
                    "id": "opta-1",
                    "date": "2027-05-30Z",
                    "time": "",
                    "week": "38",
                    "competition": {"name": "Serie A", "country": {"name": "Italy"}},
                    "stage": {"name": "Regular Season"},
                    "venue": {"longName": "Allianz Stadium", "neutral": "no"},
                    "contestant": [
                        {"position": "home", "name": "Juventus"},
                        {"position": "away", "name": "Frosinone"},
                    ],
                }
            }
        ]
    }
    event = parse_official_opta_json(payload)[0]
    assert event["start"] == "2027-05-30"
    assert event["all_day"] is True
    assert event["round"] == "Regular Season · Giornata 38"


def test_first_team_against_next_gen_is_included_but_other_squad_fixtures_are_not():
    payload = [
        official_match(TeamAway="Juventus Next Gen"),
        official_match(TeamHome="Juventus Next Gen", TeamAway="Pisa"),
        official_match(TeamHome="Juventus Women", TeamAway="Roma Women"),
        official_match(TeamHome="Juventus Primavera", TeamAway="Torino Primavera"),
        official_match(TeamAway="Napoli"),
    ]
    events = parse_official_json(payload)
    assert [(x["home_team"], x["away_team"]) for x in events] == [
        ("Juventus", "Juventus Next Gen"),
        ("Juventus", "Napoli"),
    ]


def test_parse_espn_fallback_api():
    payload = {
        "events": [
            {
                "id": "espn-1",
                "date": "2026-07-25T18:00:00Z",
                "league": {"name": "Club Friendly"},
                "links": [{"href": "https://www.espn.com/match/espn-1"}],
                "status": {"type": {"name": "STATUS_SCHEDULED", "detail": "Sat, July 25"}},
                "competitions": [
                    {
                        "round": 0,
                        "venue": {"fullName": "Stade de Sclessin", "address": {"city": "Liegi"}},
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "Standard Liege"}},
                            {"homeAway": "away", "team": {"displayName": "Juventus FC"}},
                        ],
                    }
                ],
            }
        ]
    }
    event = parse_espn_json(payload, "Amichevole")[0]
    assert event["away_team"] == "Juventus FC"
    assert event["venue"] == "Stade de Sclessin"
    assert event["location"] == "Liegi"


def test_parse_additional_structured_friendly_source():
    payload = {
        "events": [
            {
                "idEvent": "123",
                "strTimestamp": "2026-08-05T11:30:00Z",
                "strHomeTeam": "Chelsea FC",
                "strAwayTeam": "Juventus",
                "strLeague": "Club Friendlies",
                "strVenue": "Kai Tak Stadium",
                "strCity": "Hong Kong",
            }
        ]
    }
    event = parse_thesportsdb_json(payload)[0]
    assert event["source"] == "TheSportsDB"
    assert event["location"] == "Hong Kong"


def test_parse_structured_broadcaster_data():
    structured = {
        "@context": "https://schema.org",
        "text": "Sabato 12 settembre ore 21:00 - Juventus vs Roma.",
    }
    html = f'<script type="application/ld+json">{json.dumps(structured)}</script>'
    event = parse_schedule_html(html, "DAZN", "https://dazn.example", 2026, 80, "DAZN")[0]
    assert event["start"] == "2026-09-12T21:00:00+02:00"
    assert event["broadcast_it"] == "DAZN"
    assert event["_time_overlay"] is True


def test_broadcaster_time_overrides_club_tbc_without_replacing_metadata():
    club = base_event(start="2026-09-12", all_day=True, time_source="", time_source_url="")
    html = '<script type="application/ld+json">{"text":"12 settembre ore 21:00 - Juventus vs Roma."}</script>'
    overlay = parse_schedule_html(html, "DAZN", "https://dazn.example", 2026, 80, "DAZN")[0]
    merged = merge_remote_events([overlay, club])
    assert merged[0]["start"] == "2026-09-12T21:00:00+02:00"
    assert merged[0]["source"] == "Juventus"
    assert merged[0]["venue"] == "Allianz Stadium"
    assert merged[0]["time_source"] == "DAZN"


def test_editorial_fallback_is_used_but_broadcaster_has_final_priority():
    club = base_event(start="2026-09-12", all_day=True)
    editorial = parse_schedule_html(
        '<script type="application/ld+json">{"text":"12 settembre ore 20:45 - Juventus vs Roma."}</script>',
        "Gazzetta dello Sport", "https://gazzetta.example", 2026, 40,
    )[0]
    broadcaster = parse_schedule_html(
        '<script type="application/ld+json">{"text":"12 settembre ore 21:00 - Roma vs Juventus."}</script>',
        "Sky Sport/NOW", "https://sky.example", 2026, 80, "Sky Sport e NOW",
    )[0]
    merged = merge_remote_events([broadcaster, editorial, club])[0]
    assert merged["start"] == "2026-09-12T21:00:00+02:00"
    assert merged["time_source"] == "Sky Sport/NOW"


def test_dazn_sky_now_use_the_same_priority_order_as_milan_calendar():
    club = base_event(start="2026-09-12", all_day=True)
    sky = parse_schedule_html(
        '<script type="application/ld+json">{"text":"12 settembre ore 20:30 - Juventus vs Roma."}</script>',
        "Sky Sport", "https://sky.example", 2026, 40, "Sky Sport e NOW",
    )[0]
    dazn = parse_schedule_html(
        '<script type="application/ld+json">{"text":"12 settembre ore 21:00 - Juventus vs Roma."}</script>',
        "DAZN", "https://dazn.example", 2026, 60, "DAZN",
    )[0]
    now = parse_schedule_html(
        '<script type="application/ld+json">{"text":"12 settembre ore 20:45 - Juventus vs Roma."}</script>',
        "NOW", "https://now.example", 2026, 50, "Sky Sport e NOW",
    )[0]

    merged = merge_remote_events([dazn, now, club, sky])[0]

    assert merged["start"] == "2026-09-12T21:00:00+02:00"
    assert merged["time_source"] == "DAZN"
    assert merged["broadcast_it"] == "DAZN; Sky Sport e NOW"
    assert merged["broadcast_source_url"] == "https://dazn.example"


def test_deduplication_and_juventus_equivalent_names():
    official = base_event()
    espn = base_event(
        source="ESPN", home_team="Juventus FC", venue="", start="2026-09-12T19:00:00+00:00"
    )
    merged = merge_remote_events([espn, official])
    assert len(merged) == 1
    assert merged[0]["source"] == "Juventus"


def test_common_opponent_aliases_are_deduplicated():
    nice_official = base_event(away_team="OGC Nice", competition="Friendly")
    nice_fallback = base_event(source="ESPN", away_team="Nice", competition="Club Friendly")
    inter_official = base_event(away_team="Inter", competition="Friendly")
    inter_fallback = base_event(source="TheSportsDB", away_team="Inter Milan", competition="Club Friendlies")
    assert len(merge_remote_events([nice_fallback, nice_official])) == 1
    assert len(merge_remote_events([inter_fallback, inter_official])) == 1


def test_milan_and_next_gen_aliases_are_deduplicated():
    milan_official = base_event(away_team="Milan", competition="Serie A")
    milan_fallback = base_event(
        source="TheSportsDB", away_team="AC Milan", competition="Italian Serie A"
    )
    next_gen_official = base_event(
        away_team="Next Gen", competition="Friendly", start="2026-08-17T18:00:00+02:00"
    )
    next_gen_manual = base_event(
        source="Manuale",
        away_team="Juventus Next Gen",
        competition="Amichevole",
        start="2026-08-17T18:00:00+02:00",
    )

    assert len(merge_remote_events([milan_fallback, milan_official])) == 1
    assert len(merge_manual_events([next_gen_official], [next_gen_manual])) == 1


def test_time_overlay_cannot_create_a_next_day_fixture():
    europa = base_event(
        away_team="NEC",
        competition="UEFA Europa League",
        start="2026-09-17T21:00:00+02:00",
    )
    official = base_event(
        away_team="Atalanta",
        start="2026-09-20T18:00:00+02:00",
        time_source="Juventus",
    )
    misleading_now = parse_schedule_html(
        '<script type="application/ld+json">{"text":"18 settembre ore 18:00 - Juventus vs Atalanta."}</script>',
        "NOW",
        "https://now.example",
        2026,
        50,
        "Sky Sport e NOW",
    )[0]

    merged = merge_remote_events([europa, official, misleading_now])
    atalanta = next(event for event in merged if event["away_team"] == "Atalanta")

    assert atalanta["start"] == "2026-09-20T18:00:00+02:00"
    assert atalanta["time_source"] == "Juventus"
    assert "broadcast_it" not in atalanta
    assert atalanta["time_conflicts"] == [{
        "source": "NOW",
        "source_url": "https://now.example",
        "start": "2026-09-18T18:00:00+02:00",
        "rejected_reason": "meno di 48 ore da un'altra partita",
        "conflicting_fixture": "Juventus - NEC",
    }]


def test_uid_is_stable_after_date_time_venue_round_and_tv_changes():
    before = base_event()
    after = base_event(
        start="2026-09-14T21:00:00+02:00",
        venue="Stadio Olimpico",
        round="4",
        broadcast_it="DAZN",
    )
    assert _uid_for(before) == _uid_for(after)


def test_uid_survives_multi_month_postponement_and_changed_source_id(tmp_path, monkeypatch):
    write_manual(tmp_path)
    original = base_event(
        source_id="old-id",
        competition="Coppa Italia",
        round="Finale",
        start="2026-05-10T18:45:00+00:00",
    )
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([original], ["Juventus"], []),
    )
    first = update_calendar(tmp_path, session=object(), today=date(2026, 5, 1))[0]
    moved = dict(original, source_id="new-id", start="2026-08-20T18:45:00+00:00")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([moved], ["Juventus"], []),
    )
    second = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]
    assert second["uid"] == first["uid"]
    assert second["sequence"] == 1


def test_home_and_away_legs_have_unique_uids_and_legacy_collision_is_migrated(tmp_path, monkeypatch):
    write_manual(tmp_path)
    home_leg = base_event(source_id="home-leg")
    away_leg = base_event(
        source_id="away-leg",
        home_team="Roma",
        away_team="Juventus",
        round="24",
        start="2027-02-14T19:45:00+00:00",
    )
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([home_leg, away_leg], ["Juventus"], []),
    )
    first = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))
    assert len({event["uid"] for event in first}) == 2

    events_path = tmp_path / "data" / "events.json"
    payload = json.loads(events_path.read_text(encoding="utf-8"))
    payload["events"][1]["uid"] = payload["events"][0]["uid"]
    events_path.write_text(json.dumps(payload), encoding="utf-8")
    migrated = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))
    assert len({event["uid"] for event in migrated}) == 2


def test_reversed_teams_in_tv_schedule_and_tv_does_not_create_fixture():
    club = base_event(home_team="Roma", away_team="Juventus", start="2026-09-12", all_day=True)
    matching = parse_schedule_html(
        '<script type="application/ld+json">{"text":"12 settembre ore 20:45 - Juventus vs Roma."}</script>',
        "DAZN", "https://dazn.example", 2026, 80, "DAZN",
    )[0]
    unrelated = dict(matching, home_team="Juventus", away_team="Real Madrid")
    merged = merge_remote_events([matching, unrelated, club])
    assert len(merged) == 1
    assert merged[0]["home_team"] == "Roma"
    assert merged[0]["start"] == "2026-09-12T20:45:00+02:00"


def test_time_conflicts_are_recorded_and_highest_priority_wins():
    club = base_event()
    low = dict(
        club,
        source="Gazzetta dello Sport",
        source_url="https://gazzetta.example",
        start="2026-09-12T20:45:00+02:00",
        _time_overlay=True,
        _time_priority=40,
    )
    high = dict(
        club,
        source="DAZN",
        source_url="https://dazn.example",
        start="2026-09-12T21:00:00+02:00",
        _time_overlay=True,
        _time_priority=80,
    )
    merged = merge_remote_events([club, high, low])[0]
    assert merged["start"] == "2026-09-12T21:00:00+02:00"
    assert merged["time_source"] == "DAZN"
    assert {item["source"] for item in merged["time_conflicts"]} == {"Gazzetta dello Sport"}


def test_all_day_event_and_timed_transformation_keep_uid(tmp_path, monkeypatch):
    write_manual(tmp_path)
    all_day = base_event(start="2026-09-12", all_day=True, time_source="", time_source_url="")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([all_day], ["Juventus"], []),
    )
    first = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]
    timed = base_event(start="2026-09-12T21:00:00+02:00")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([timed], ["Juventus"], []),
    )
    second = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]
    assert first["uid"] == second["uid"]
    assert first["all_day"] is True and second["all_day"] is False
    assert second["sequence"] == first["sequence"] + 1


def test_postponed_match_is_annotated_then_rescheduled_with_same_uid(tmp_path, monkeypatch):
    write_manual(tmp_path)
    original = base_event(source_id="rain-delay")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([original], ["Juventus"], []),
    )
    scheduled = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]

    pending = dict(original, status="STATUS_POSTPONED")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([pending], ["Juventus"], []),
    )
    postponed = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]
    component = next(
        item for item in Calendar.from_ical((tmp_path / "calendar.ics").read_bytes()).walk()
        if item.name == "VEVENT"
    )
    assert postponed["uid"] == scheduled["uid"]
    assert postponed["all_day"] is True
    assert "RINVIATA — DATA DA DESTINARSI" in postponed["title"]
    assert component.decoded("status") == b"TENTATIVE"
    assert not any(item.name == "VALARM" for item in component.subcomponents)

    new_start = "2027-01-20T19:45:00+01:00"
    rescheduled_source = dict(original, start=new_start, status="Fixture")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([rescheduled_source], ["Juventus"], []),
    )
    rescheduled = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]
    assert rescheduled["uid"] == scheduled["uid"]
    assert rescheduled["postponed_to"] == new_start
    assert "RINVIATA AL 20/01/2027" in rescheduled["title"]


def test_ical_timezone_alarm_tv_and_validity():
    event = base_event(
        uid="stable@juventus-calendar",
        title="Juventus - Roma",
        home_away="Casa",
        broadcast_it="DAZN",
        broadcast_source_url="https://dazn.example",
        last_modified="2026-08-01T10:00:00Z",
        sequence=2,
        serie_a_standing={
            "position": 3,
            "points": 21,
            "played": 10,
            "goal_difference": 9,
            "provisional": True,
            "pending_recoveries": ["Lazio–Juventus"],
            "source": "Lega Serie A",
            "updated_at": "2026-08-24T10:00:00Z",
            "context": [
                {"team": "Inter", "position": 1, "points": 24, "played": 10},
                {"team": "Milan", "position": 2, "points": 22, "played": 10},
                {"team": "Juventus", "position": 3, "points": 21, "played": 10},
                {"team": "Roma", "position": 4, "points": 19, "played": 10},
                {"team": "Napoli", "position": 5, "points": 18, "played": 10},
            ],
        },
    )
    payload = build_ical([event])
    calendar = Calendar.from_ical(payload)
    component = next(x for x in calendar.walk() if x.name == "VEVENT")
    alarm = next(x for x in component.subcomponents if x.name == "VALARM")
    assert getattr(component.decoded("dtstart").tzinfo, "key", None) == "Europe/Rome"
    assert alarm.decoded("trigger").total_seconds() == -(2 * 60 + 30) * 60
    assert component.decoded("sequence") == 2
    description = component.decoded("description").decode()
    assert "Orario (Roma): 12/09/2026 20:45" in description
    assert "Dove vederla in Italia: DAZN" in description
    assert (
        "Classifica Serie A provvisoria — recupero Lazio–Juventus ancora da disputare:"
        in description
    )
    assert "  1. Inter — 24 pt" in description
    assert "▶ 3. Juventus — 21 pt — 10 PG — DR +9" in description
    assert "  5. Napoli — 18 pt" in description
    assert "Classifica aggiornata: 24/08/2026 12:00" in description
    assert "Fonte classifica: Lega Serie A" in description
    assert "https://" not in description
    assert b"BEGIN:VCALENDAR" in payload and b"END:VCALENDAR" in payload


def test_ical_all_day_uses_date_value():
    event = base_event(
        start="2026-09-12", all_day=True, uid="all-day@juventus-calendar",
        title="Juventus - Roma", home_away="Casa", last_modified="2026-08-01T10:00:00Z", sequence=0,
    )
    component = next(x for x in Calendar.from_ical(build_ical([event])).walk() if x.name == "VEVENT")
    assert component.decoded("dtstart") == date(2026, 9, 12)


def test_competition_assigns_italian_tv_coverage(tmp_path, monkeypatch):
    write_manual(tmp_path)
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([base_event(broadcast_it="")], ["Juventus"], []),
    )
    event = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]
    assert "DAZN" in event["broadcast_it"]


def test_update_attaches_standing_only_to_previous_and_next_serie_a_matches(
    tmp_path, monkeypatch
):
    write_manual(tmp_path)
    previous = base_event(
        source_id="previous",
        away_team="Torino",
        start="2026-08-10T18:45:00+00:00",
        status="finished",
    )
    just_played = base_event(
        source_id="just-played",
        away_team="Roma",
        start="2026-08-24T18:45:00+00:00",
        status="finished",
    )
    upcoming = base_event(
        source_id="upcoming",
        home_team="Inter",
        away_team="Juventus",
        start="2026-09-12T18:45:00+00:00",
    )
    later = base_event(
        source_id="later",
        away_team="Napoli",
        start="2026-09-20T18:45:00+00:00",
    )
    champions = base_event(
        source_id="champions-1",
        away_team="Paris Saint-Germain",
        competition="UEFA Champions League",
        start="2026-09-16T19:00:00+00:00",
    )
    standing = {
        "position": 3,
        "points": 21,
        "played": 10,
        "goal_difference": 9,
        "provisional": False,
        "source": "Lega Serie A",
    }
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult(
            [previous, just_played, upcoming, later, champions],
            ["Juventus", "Lega Serie A classifica"],
            [],
            standing,
        ),
    )

    events_today = update_calendar(tmp_path, session=object(), today=date(2026, 8, 24))
    assert [
        event["source_id"] for event in events_today if event.get("serie_a_standing")
    ] == ["just-played", "upcoming"]

    events = update_calendar(tmp_path, session=object(), today=date(2026, 8, 25))

    with_standing = [
        event["source_id"] for event in events if event.get("serie_a_standing")
    ]
    previous_meta = {
        event["source_id"]: (event["sequence"], event["last_modified"])
        for event in events_today
    }
    current_meta = {
        event["source_id"]: (event["sequence"], event["last_modified"])
        for event in events
    }
    europe = next(event for event in events if "Champions" in event["competition"])
    assert with_standing == ["just-played", "upcoming"]
    assert current_meta == previous_meta
    assert "serie_a_standing" not in europe


def test_manual_event_has_final_precedence(tmp_path, monkeypatch):
    write_manual(
        tmp_path,
        [
            {
                "id": "juve-roma-2026",
                "home_team": "Juventus FC",
                "away_team": "Roma",
                "competition": "Serie A",
                "start": "2026-09-12T21:00:00+02:00",
                "venue": "Stadio corretto manualmente",
                "broadcast_it": "Canale verificato",
                "time_source": "Fonte manuale",
                "locked": True,
            }
        ],
    )
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([base_event()], ["Juventus"], []),
    )
    event = update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))[0]
    assert event["source"] == "Manuale"
    assert event["venue"] == "Stadio corretto manualmente"
    assert event["broadcast_it"] == "Canale verificato"


def test_dazn_time_overrides_unlocked_manual_data():
    club = base_event(start="2026-09-12", all_day=True, time_source="", time_source_url="")
    dazn = parse_schedule_html(
        '<script type="application/ld+json">{"text":"12 settembre ore 21:00 - Juventus vs Roma."}</script>',
        "DAZN", "https://dazn.example", 2026, 60, "DAZN",
    )[0]
    remote = merge_remote_events([club, dazn])
    manual = [base_event(
        source="Manuale",
        start="2026-09-12T20:00:00+02:00",
        time_source="Fonte manuale precedente",
        broadcast_it="Canale precedente",
    )]

    event = merge_manual_events(remote, manual)[0]

    assert event["start"] == "2026-09-12T21:00:00+02:00"
    assert event["time_source"] == "DAZN"
    assert event["broadcast_it"] == "DAZN"


def test_manual_events_can_intentionally_include_other_squad(tmp_path):
    write_manual(
        tmp_path,
        [{"home_team": "Juventus Women", "away_team": "Roma Women", "competition": "Manuale", "start": "2026-09-12"}],
    )
    assert load_manual_events(tmp_path / "data" / "manual_events.json")[0]["home_team"] == "Juventus Women"


def test_disabled_manual_event_is_ignored(tmp_path):
    write_manual(
        tmp_path,
        [{"enabled": False, "home_team": "Juventus", "away_team": "X", "competition": "Test", "start": "2026-09-12"}],
    )
    assert load_manual_events(tmp_path / "data" / "manual_events.json") == []


def test_total_source_failure_preserves_previous_outputs(tmp_path, monkeypatch):
    write_manual(tmp_path)
    events_path = tmp_path / "data" / "events.json"
    calendar_path = tmp_path / "calendar.ics"
    events_path.write_text('{"events":[{"sentinel":true}]}\n', encoding="utf-8")
    calendar_path.write_text("LAST VALID CALENDAR\n", encoding="utf-8")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([], [], ["offline"]),
    )
    with pytest.raises(UpdateError):
        update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))
    assert "sentinel" in events_path.read_text(encoding="utf-8")
    assert calendar_path.read_text(encoding="utf-8") == "LAST VALID CALENDAR\n"


def test_tv_only_success_does_not_replace_outputs(tmp_path, monkeypatch):
    write_manual(tmp_path)
    (tmp_path / "calendar.ics").write_text("VALID\n", encoding="utf-8")
    overlay = dict(base_event(), _time_overlay=True, source="DAZN")
    monkeypatch.setattr(
        "juventus_calendar.generator.fetch_remote_events",
        lambda session, today: FetchResult([overlay], ["DAZN"], []),
    )
    with pytest.raises(UpdateError):
        update_calendar(tmp_path, session=object(), today=date(2026, 8, 1))
    assert (tmp_path / "calendar.ics").read_text(encoding="utf-8") == "VALID\n"


def test_subscription_page_links_and_instructions():
    html = (Path(__file__).parents[1] / "index.html").read_text(encoding="utf-8")
    assert "webcal://dizzle0987.github.io/juventus-calendar/calendar.ics" in html
    assert "https://dizzle0987.github.io/juventus-calendar/calendar.ics" in html
    assert "navigator.clipboard.writeText(calendarUrl)" in html
    assert "Google Calendar" in html
    assert "notifica push" in html

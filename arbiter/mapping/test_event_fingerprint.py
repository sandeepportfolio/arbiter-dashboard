from __future__ import annotations

from arbiter.mapping.event_fingerprint import (
    fingerprint_kalshi_market,
    fingerprint_polymarket_market,
    structural_match,
)
from arbiter.mapping.team_aliases import normalize_entity_code, split_compound_code


def test_sports_fingerprint_matches_aliases_and_outcome():
    kalshi = {
        "ticker": "KXLALIGAGAME-26MAY02OSABAR-BAR",
        "title": "Will Barcelona win?",
    }
    poly = {
        "slug": "atc-lal-osa-fcb-2026-05-02-fcb",
        "question": "Osasuna vs FC Barcelona",
        "category": "sports",
    }

    match = structural_match(kalshi, poly)

    assert match is not None
    assert match.category == "sports"
    assert match.event_key == "sports:lal:fcb-osa:2026-05-02:winner:moneyline"
    assert match.outcome == "fcb"
    assert match.polarity == "same"


def test_sports_fingerprint_rejects_same_event_wrong_outcome():
    kalshi = {
        "ticker": "KXLALIGAGAME-26MAY02OSABAR-BAR",
        "title": "Will Barcelona win?",
    }
    poly = {
        "slug": "atc-lal-osa-fcb-2026-05-02-osa",
        "question": "Will Osasuna win?",
        "category": "sports",
    }

    assert structural_match(kalshi, poly) is None


def test_politics_control_fingerprint_matches_party_and_chamber():
    kalshi = {
        "ticker": "CONTROLS-2026-D",
        "title": "Will Democrats win control of the Senate?",
        "category": "politics",
    }
    poly = {
        "slug": "paccc-usse-midterms-2026-11-03-dem",
        "question": "Will the Democratic Party win the Senate in the 2026 Midterms?",
        "category": "politics",
    }

    match = structural_match(kalshi, poly)

    assert match is not None
    assert match.event_key == "politics:us:senate:2026-11-03:party-control:majority"
    assert match.outcome == "dem"


def test_crypto_fingerprint_rejects_different_threshold_and_date():
    kalshi = {
        "ticker": "KXBTCD-26APR2207-T85799.99",
        "title": "Bitcoin price on Apr 22, 2026? $85,800 or above",
        "category": "crypto",
    }
    poly = {
        "slug": "will-bitcoin-reach-100000-by-december-31-2026",
        "question": "Will Bitcoin reach $100,000 by December 31, 2026?",
        "category": "crypto",
    }

    assert fingerprint_kalshi_market(kalshi) is not None
    assert fingerprint_polymarket_market(poly) is not None
    assert structural_match(kalshi, poly) is None


def test_economics_fed_decision_fingerprint_matches_exact_outcome_and_date():
    kalshi = {
        "ticker": "KXFEDDECISION-26JUN17-HOLD",
        "title": "Will the Fed hold rates at the June 17, 2026 FOMC meeting?",
    }
    poly = {
        "slug": "rdc-usfed-fomc-2026-06-17-maintains",
        "question": "Will the Federal Reserve maintain rates after the June 2026 FOMC meeting?",
    }

    match = structural_match(kalshi, poly)

    assert match is not None
    assert match.event_key == "economics:us:fomc-rate-decision:2026-06-17:fed-rate-decision:target-range"
    assert match.outcome == "maintains"
    assert match.resolution_source == "federal_reserve"


def test_economics_cpi_and_unemployment_require_same_period_threshold_and_source():
    cpi_kalshi = {
        "ticker": "KXCPIYOY-26MAY13-GTE3.0PCT",
        "title": "April 2026 CPI YoY at least 3.0%?",
    }
    cpi_poly = {
        "slug": "cpic-uscpi-apr2026yoy-2026-05-13-gte3pt0pct",
        "question": "Will US CPI YoY for April 2026 be at least 3.0%?",
    }
    unemployment_kalshi = {
        "ticker": "KXUNEMP-26MAY08-GTE4.0PCT",
        "title": "April 2026 unemployment rate at least 4.0%?",
    }
    unemployment_poly_wrong_threshold = {
        "slug": "uec-usunemployment-apr2026-2026-05-08-gte4pt1pct",
        "question": "Will US unemployment for April 2026 be at least 4.1%?",
    }

    assert structural_match(cpi_kalshi, cpi_poly) is not None
    assert structural_match(unemployment_kalshi, unemployment_poly_wrong_threshold) is None


def test_team_alias_split_distinguishes_montreal_and_inter_miami():
    assert normalize_entity_code("MTL") == "mtl"
    assert normalize_entity_code("MIM") == "mim"
    assert split_compound_code("ATXSTL") == ("aus", "stl")

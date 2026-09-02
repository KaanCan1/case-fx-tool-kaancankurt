"""Tests for the conversion endpoint. No network: every upstream call is faked.

FX_UPSTREAM_BASE is pointed at a host that does not resolve, and respx intercepts
before anything leaves the process. The one test that needs a real failure uses a
closed port on localhost, which fails immediately rather than waiting on a timeout.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app as application
import fx

FAKE = "http://upstream.test"
HOST = "upstream.test"

CURRENCIES = {"EUR": "Euro", "TRY": "Turkish Lira", "USD": "United States Dollar"}


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    """A faked frankfurter.dev. Yields the router so tests can add or count routes."""
    monkeypatch.setenv("FX_UPSTREAM_BASE", FAKE)
    fx.cache.clear()
    with respx.mock:
        respx.route(host=HOST, path="/v1/currencies").mock(
            return_value=httpx.Response(200, json=CURRENCIES)
        )
        yield respx


@pytest.fixture
def client():
    with TestClient(application.app) as test_client:
        yield test_client


def rates(on: str, rate: float, symbol: str = "TRY", base: str = "EUR") -> httpx.Response:
    return httpx.Response(
        200, json={"amount": 1.0, "base": base, "date": on, "rates": {symbol: rate}}
    )


def route(path: str):
    return respx.route(host=HOST, path=f"/v1/{path}")


def convert(client: TestClient, **params) -> httpx.Response:
    return client.get("/tools/convert", params=params)


# --------------------------------------------------------------------------
# The happy path


def test_converts_at_the_published_rate_without_rounding_it(client):
    route("2026-08-28").mock(return_value=rates("2026-08-28", 47.1234))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-28")

    assert response.status_code == 200
    assert response.json() == {
        "amount": 250.0,
        "from": "EUR",
        "to": "TRY",
        "rate": 47.1234,
        "result": 11780.85,
        "rate_date": "2026-08-28",
        "asked_date": "2026-08-28",
        "source": "ECB via frankfurter.dev",
    }


def test_without_a_date_it_uses_the_latest_publication(client):
    route("latest").mock(return_value=rates("2026-09-01", 47.1234))

    body = convert(client, amount=1, **{"from": "EUR"}, to="TRY").json()

    assert body["rate_date"] == "2026-09-01"
    assert body["asked_date"] == fx.today().isoformat()


def test_amount_with_ten_decimal_places_is_accepted(client):
    route("latest").mock(return_value=rates("2026-09-01", 47.1234))

    body = convert(client, amount="250.0000000001", **{"from": "EUR"}, to="TRY").json()

    assert body["result"] == 11780.85


# --------------------------------------------------------------------------
# The point of the exercise: a rate is never attributed to the wrong day


def test_a_day_with_no_publication_reports_the_date_the_rate_came_from(client):
    # Sunday. The upstream answers with Friday's rate and says so.
    route("2026-08-30").mock(return_value=rates("2026-08-28", 56.1718))

    body = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-30").json()

    assert body["rate_date"] == "2026-08-28"
    assert body["asked_date"] == "2026-08-30"
    assert body["rate"] == 56.1718


def test_a_date_with_no_rate_at_all_is_refused_not_back_filled(client):
    route("2026-08-28").mock(return_value=httpx.Response(404, json={"message": "not found"}))
    latest = route("latest").mock(return_value=rates("2026-09-01", 47.1234))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-28")

    assert response.status_code == 404
    assert response.json()["error"] == "no_rate_available"
    assert not latest.called, "a missing date must not be answered with the latest rate"


def test_a_rate_dated_after_the_day_asked_for_is_not_passed_on(client):
    route("2026-08-28").mock(return_value=rates("2026-09-01", 47.1234))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-28")

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


# --------------------------------------------------------------------------
# Refusals, and what they cost


@pytest.mark.parametrize(
    "params, code, status",
    [
        ({"amount": 250, "from": "EUR", "to": "TRY", "date": "2999-01-01"}, "date_in_future", 400),
        ({"amount": 250, "from": "EUR", "to": "TRY", "date": "1998-12-31"}, "date_out_of_range", 400),
        ({"amount": 250, "from": "EUR", "to": "TRY", "date": "not-a-date"}, "invalid_date", 400),
        ({"amount": 250, "from": "EUR", "to": "EUR"}, "same_currency", 400),
        ({"amount": 250, "from": "EUR", "to": "XXX"}, "unknown_currency", 400),
        ({"amount": 250, "from": "EU", "to": "TRY"}, "invalid_currency", 400),
        ({"amount": 250, "from": "EUR", "to": "12"}, "invalid_currency", 400),
        ({"amount": 0, "from": "EUR", "to": "TRY"}, "invalid_amount", 400),
        ({"amount": -250, "from": "EUR", "to": "TRY"}, "invalid_amount", 400),
        ({"amount": "1e400", "from": "EUR", "to": "TRY"}, "invalid_amount", 400),
        ({"amount": "abc", "from": "EUR", "to": "TRY"}, "invalid_amount", 400),
        ({"from": "EUR", "to": "TRY"}, "invalid_amount", 400),
        ({"amount": 250, "to": "TRY"}, "invalid_currency", 400),
    ],
)
def test_bad_requests_are_refused_with_a_code_and_a_sentence(client, params, code, status):
    response = convert(client, **params)

    assert response.status_code == status
    body = response.json()
    assert body["error"] == code
    assert body["message"].endswith("."), "the message should read as a sentence"
    assert "rate" not in body and "result" not in body


def test_tomorrow_is_refused_even_though_the_upstream_would_answer_it(client):
    # The upstream answers near-future dates with today's rate; we do not ask.
    tomorrow = (fx.today() + timedelta(days=1)).isoformat()
    asked = route(tomorrow).mock(return_value=rates("2026-09-02", 47.1234))

    response = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date=tomorrow)

    assert response.status_code == 400
    assert response.json()["error"] == "date_in_future"
    assert not asked.called


def test_a_refusal_never_carries_a_number(client):
    response = convert(client, amount=250, **{"from": "EUR"}, to="EUR")

    assert set(response.json()) == {"error", "message"}


# --------------------------------------------------------------------------
# When the upstream misbehaves


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="upstream on fire"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"amount": 1.0, "base": "EUR", "rates": {"TRY": 47.1}}),
        httpx.Response(200, json={"amount": 1.0, "base": "EUR", "date": "2026-08-28"}),
    ],
    ids=["http_500", "not_json", "no_date", "no_rate"],
)
def test_a_broken_upstream_never_becomes_a_number(client, response):
    route("2026-08-28").mock(return_value=response)

    answer = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-28")

    assert answer.status_code in (404, 502)
    assert "result" not in answer.json()


def test_an_unreachable_upstream_is_reported_not_absorbed(client, monkeypatch):
    # Port 9 (discard) is closed, so this fails at connect without a timeout wait.
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://127.0.0.1:9")
    fx.cache.clear()

    with respx.mock(assert_all_called=False):
        respx.route(host="127.0.0.1").pass_through()
        response = convert(client, amount=250, **{"from": "EUR"}, to="TRY")

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


# --------------------------------------------------------------------------
# Caching


def test_the_same_question_is_only_asked_once(client):
    asked = route("2026-08-28").mock(return_value=rates("2026-08-28", 47.1234))

    first = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-28").json()
    second = convert(client, amount=250, **{"from": "EUR"}, to="TRY", date="2026-08-28").json()

    assert first == second
    assert asked.call_count == 1


def test_two_dates_never_share_a_cache_entry(client):
    route("2026-08-28").mock(return_value=rates("2026-08-28", 56.1718))
    route("2026-09-01").mock(return_value=rates("2026-09-01", 47.1234))

    first = convert(client, amount=1, **{"from": "EUR"}, to="TRY", date="2026-08-28").json()
    second = convert(client, amount=1, **{"from": "EUR"}, to="TRY", date="2026-09-01").json()

    assert (first["rate"], first["rate_date"]) == (56.1718, "2026-08-28")
    assert (second["rate"], second["rate_date"]) == (47.1234, "2026-09-01")


def test_todays_rate_is_not_cached_for_good(client):
    """A settled past date may be cached forever; today may still change."""
    today = fx.today().isoformat()
    route(today).mock(return_value=rates(today, 47.1234))

    convert(client, amount=1, **{"from": "EUR"}, to="TRY", date=today)

    expires_at, _ = fx.cache._items[("EUR", "TRY", today)]
    assert expires_at is not None

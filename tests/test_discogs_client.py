"""Stage 2: rate limiting, retries and response parsing - with a fake clock (no real waits)."""

import json
import random

import pytest
import requests

from cd_screener.discogs_client import (
    DiscogsAuthError,
    DiscogsClient,
    DiscogsError,
    DiscogsNotFound,
    DiscogsRateLimited,
    RateLimiter,
    extract_condition_price,
)

NM = "Near Mint (NM or M-)"
SUGGESTIONS = {NM: {"currency": "USD", "value": 21.456}, "Very Good (VG)": {"currency": "USD", "value": 9.0}}


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedSession:
    """Returns (or raises) the scripted items in order and records each call."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status, payload=None, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(payload if payload is not None else {}).encode()
    resp.headers.update(headers or {})
    return resp


def make_client(session, clock, **kwargs):
    limiter = RateLimiter(60, 1.0, clock=clock.time, sleep=clock.sleep)
    kwargs.setdefault("backoff_base", 2.0)
    return DiscogsClient("secret-token", session=session, rate_limiter=limiter,
                         rng=random.Random(0), **kwargs)


def test_rate_limiter_spaces_requests_evenly():
    clock = FakeClock()
    limiter = RateLimiter(60, 0.9, clock=clock.time, sleep=clock.sleep)
    for _ in range(4):
        limiter.wait()
    assert clock.sleeps == pytest.approx([60 / 54] * 3)  # first call is immediate


def test_rate_limiter_backs_off_when_server_quota_is_low():
    clock = FakeClock()
    limiter = RateLimiter(60, 1.0, clock=clock.time, sleep=clock.sleep)
    limiter.wait()
    limiter.observe({"X-Discogs-Ratelimit-Remaining": "2"})
    limiter.wait()
    assert clock.sleeps[-1] == pytest.approx(30.0)  # 60 s window / 2 remaining
    limiter.observe({"X-Discogs-Ratelimit-Remaining": "40"})  # plenty left: no extra pause
    limiter.wait()
    assert clock.sleeps[-1] == pytest.approx(1.0)


def test_sends_auth_and_user_agent_and_parses_price():
    clock = FakeClock()
    session = ScriptedSession(response(200, SUGGESTIONS))
    client = make_client(session, clock)
    point = extract_condition_price(client.get_price_suggestions(249504))
    assert (point.condition, point.value, point.currency) == (NM, 21.46, "USD")
    call = session.calls[0]
    assert call["url"].endswith("/marketplace/price_suggestions/249504")
    assert call["headers"]["Authorization"] == "Discogs token=secret-token"
    assert "CDAppreciationScreener" in call["headers"]["User-Agent"]


def test_429_honours_retry_after():
    clock = FakeClock()
    session = ScriptedSession(response(429, headers={"Retry-After": "7"}), response(200, SUGGESTIONS))
    client = make_client(session, clock)
    assert client.get_price_suggestions(1) == SUGGESTIONS
    assert clock.sleeps == [pytest.approx(7.0)]
    assert client.retries == 1


def test_429_without_retry_after_backs_off_exponentially():
    clock = FakeClock()
    session = ScriptedSession(response(429), response(429), response(200, SUGGESTIONS))
    client = make_client(session, clock)
    client.get_price_suggestions(1)
    first, second = clock.sleeps
    assert 1.6 <= first <= 2.4  # 2 s +/- 20% jitter
    assert 3.2 <= second <= 4.8  # 4 s +/- 20% jitter


def test_429_after_all_retries_raises():
    clock = FakeClock()
    session = ScriptedSession(*[response(429) for _ in range(3)])
    client = make_client(session, clock, max_retries=2)
    with pytest.raises(DiscogsRateLimited):
        client.get_price_suggestions(1)
    assert len(session.calls) == 3


def test_server_and_network_errors_are_retried():
    clock = FakeClock()
    session = ScriptedSession(requests.ConnectionError("reset"), response(502), response(200, SUGGESTIONS))
    client = make_client(session, clock)
    assert client.get_price_suggestions(1) == SUGGESTIONS
    assert client.retries == 2


@pytest.mark.parametrize(("status", "error"), [(404, DiscogsNotFound), (401, DiscogsAuthError)])
def test_non_retryable_statuses_raise_immediately(status, error):
    clock = FakeClock()
    session = ScriptedSession(response(status, {"message": "nope"}))
    with pytest.raises(error):
        make_client(session, clock).get_price_suggestions(1)
    assert len(session.calls) == 1


def test_missing_seller_settings_is_fatal_not_a_missing_release():
    clock = FakeClock()
    session = ScriptedSession(response(404, {"message": "You must fill out your seller settings first."}))
    with pytest.raises(DiscogsAuthError, match="settings/seller"):
        make_client(session, clock).get_price_suggestions(1)


def test_403_surfaces_the_server_message():
    clock = FakeClock()
    session = ScriptedSession(response(403, {"message": "You must fill out your seller settings first."}))
    with pytest.raises(DiscogsError, match="seller settings") as info:
        make_client(session, clock).get_price_suggestions(1)
    assert not isinstance(info.value, DiscogsAuthError)  # per-release problem, not fatal


def test_cd_versions_parsing():
    from cd_screener.discogs_client import extract_cd_versions
    payload = {"pagination": {"items": 18},
               "versions": [{"id": 12468335, "released": "2008", "major_formats": ["CD"]}]}
    v = extract_cd_versions(payload)
    assert (v.count, v.latest_year, v.latest_release_id) == (18, 2008, 12468335)
    empty = extract_cd_versions({"pagination": {"items": 0}, "versions": []})
    assert (empty.count, empty.latest_year) == (0, None)


@pytest.mark.parametrize("payload", [
    {},  # Discogs' answer when it has no suggestion
    {"Very Good (VG)": {"currency": "USD", "value": 5}},
    {NM: {"currency": "USD", "value": 0}},
    {NM: {"currency": "USD", "value": "n/a"}},
    {NM: {"value": 12.0}},
    ["not", "a", "dict"],
])
def test_missing_or_invalid_price_data_returns_none(payload):
    assert extract_condition_price(payload) is None

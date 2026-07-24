import base64
import json

import httpx
import pytest

from toilet2.cms_auth import (
    CMSAuthenticator,
    CMSAuthStatus,
    CookieDecodeError,
    parse_cms_cookie,
)
from toilet2.settings import Settings


def _payload(username="alice"):
    return base64.b64encode(
        json.dumps([username, "SECRET-MUST-NOT-ESCAPE", 1234.5]).encode()
    ).decode()


def _field(value):
    encoded = value.encode()
    return f"{len(encoded)}:" + value


def _v1(username="alice"):
    return f"{_payload(username)}|1700000000|{'a' * 40}"


def _v2(name="contest_login", username="alice"):
    return "|".join(
        [
            "2",
            _field("0"),
            _field("1700000000"),
            _field(name),
            _field(_payload(username)),
            "b" * 64,
        ]
    )


def test_parse_tornado_v1_discards_secret():
    hint = parse_cms_cookie(_v1(), "contest_login")
    assert hint.username == "alice"
    assert hint.cms_timestamp == 1234.5
    assert hint.version == 1
    assert "SECRET" not in repr(hint)


def test_parse_tornado_v2_unicode_and_embedded_name():
    hint = parse_cms_cookie(_v2(username="Živilė"), "contest_login")
    assert hint.username == "Živilė"
    assert hint.version == 2


@pytest.mark.parametrize(
    "raw",
    [
        "2|x:0|",
        "2|1000001:x|",
        "2|1:0|10:1700000000|5:name|4:AAAA|short",
        "AAAA|bad-time|" + "a" * 40,
        "3|value|signature",
    ],
)
def test_parse_rejects_malformed_outer_cookie(raw):
    with pytest.raises(CookieDecodeError):
        parse_cms_cookie(raw, "contest_login")


def test_parse_rejects_wrong_embedded_cookie_name():
    with pytest.raises(CookieDecodeError, match="does not match"):
        parse_cms_cookie(_v2(name="other_login"), "contest_login")


@pytest.mark.asyncio
async def test_cms_probe_is_authoritative_client_aware_cached_and_filters_cookies():
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.url.path == "/api/toilet-auth"
        assert request.headers["x-forwarded-for"] in {"192.0.2.10", "192.0.2.11"}
        return httpx.Response(
            200,
            json={"username": "bob", "contest": "contest"},
            headers=[
                ("Set-Cookie", f"contest_login={_v2(name='contest_login', username='bob')}; Path=/"),
                ("Set-Cookie", "_xsrf=do-not-relay; Path=/"),
            ],
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://cms.test"
    )
    settings = Settings(
        cms_base_url="http://cms.test",
        cms_contests=("contest",),
    )
    auth = CMSAuthenticator(settings, client=client)

    result = await auth.authenticate({"contest_login": _v2()}, "192.0.2.10")
    assert result.status is CMSAuthStatus.AUTHENTICATED
    # The opaque browser cookie may contain Alice, but only CMS's response is trusted.
    assert result.username == "bob"
    assert len(result.set_cookie_headers) == 1
    assert result.set_cookie_headers[0].startswith("contest_login=")

    cached = await auth.authenticate({"contest_login": _v2()}, "192.0.2.10")
    assert cached.authenticated
    assert cached.set_cookie_headers == ()
    assert len(calls) == 1

    await auth.authenticate({"contest_login": _v2()}, "192.0.2.11")
    assert len(calls) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_no_cookie_is_unauthenticated_without_a_cms_probe():
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://cms.test"
    )
    auth = CMSAuthenticator(
        Settings(cms_contests=("contest",)), client=client
    )
    result = await auth.authenticate({}, "2001:db8::1")
    assert result.status is CMSAuthStatus.UNAUTHENTICATED
    assert calls == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_cms_response_cookie_never_leaks_into_a_later_browser_probe():
    seen_cookie = []

    async def handler(request):
        seen_cookie.append(request.headers.get("cookie"))
        if len(seen_cookie) == 1:
            return httpx.Response(
                200,
                json={"username": "alice", "contest": "contest"},
                headers={"Set-Cookie": f"contest_login={_v2()}; Path=/"},
            )
        return httpx.Response(302, headers={"Location": "/"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://cms.test"
    )
    auth = CMSAuthenticator(
        Settings(cms_contests=("contest",)), client=client
    )

    first_cookie = _v2(username="alice")
    second_cookie = _v2(username="bob")
    first = await auth.authenticate(
        {"contest_login": first_cookie}, "192.0.2.10"
    )
    second = await auth.authenticate(
        {"contest_login": second_cookie}, "192.0.2.10"
    )

    assert first.status is CMSAuthStatus.AUTHENTICATED
    assert second.status is CMSAuthStatus.UNAUTHENTICATED
    assert seen_cookie == [
        f"contest_login={first_cookie}",
        f"contest_login={second_cookie}",
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_multi_contest_different_users_fails_closed():
    async def handler(request):
        contest = request.url.path.split("/")[1]
        return httpx.Response(200, json={"username": contest, "contest": contest})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://cms.test"
    )
    auth = CMSAuthenticator(
        Settings(
            cms_multi_contest=True,
            cms_contests=("alice", "bob"),
        ),
        client=client,
    )
    result = await auth.authenticate(
        {
            "alice_login": _v2(name="alice_login", username="alice"),
            "bob_login": _v2(name="bob_login", username="bob"),
        },
        "192.0.2.1",
    )
    assert result.status is CMSAuthStatus.AMBIGUOUS
    await client.aclose()


@pytest.mark.asyncio
async def test_cms_transport_failure_is_unavailable_not_dev_fallback():
    async def handler(_request):
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://cms.test"
    )
    auth = CMSAuthenticator(
        Settings(cms_contests=("contest",)), client=client
    )
    result = await auth.authenticate(
        {"contest_login": _v2()}, "192.0.2.1"
    )
    assert result.status is CMSAuthStatus.UNAVAILABLE
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,status",
    [
        (httpx.Response(302, headers={"Location": "/"}), CMSAuthStatus.UNAUTHENTICATED),
        (httpx.Response(404), CMSAuthStatus.UNAVAILABLE),
        (
            httpx.Response(200, json={"username": "alice", "contest": "wrong"}),
            CMSAuthStatus.UNAVAILABLE,
        ),
    ],
)
async def test_cms_expected_logout_is_distinct_from_contract_failure(response, status):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response),
        base_url="http://cms.test",
    )
    auth = CMSAuthenticator(
        Settings(cms_contests=("contest",)), client=client
    )
    result = await auth.authenticate(
        {"contest_login": _v2()}, "192.0.2.44"
    )
    assert result.status is status
    await client.aclose()


@pytest.mark.asyncio
async def test_unavailable_is_briefly_cached_and_cache_and_flights_are_bounded():
    now = [100.0]
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://cms.test"
    )
    auth = CMSAuthenticator(
        Settings(
            cms_contests=("contest",),
            cms_cache_max_entries=2,
            cms_negative_cache_seconds=3,
        ),
        client=client,
        clock=lambda: now[0],
    )
    await auth.authenticate({"contest_login": _v1("one")}, "192.0.2.1")
    await auth.authenticate({"contest_login": _v1("one")}, "192.0.2.1")
    assert calls == 1
    for username in ("two", "three", "four"):
        await auth.authenticate({"contest_login": _v1(username)}, "192.0.2.1")
    assert len(auth._cache) <= 2
    assert auth._flights == {}
    now[0] += 4
    await auth.authenticate({"contest_login": _v1("five")}, "192.0.2.1")
    assert all(item.expires_at > now[0] for item in auth._cache.values())
    await client.aclose()


@pytest.mark.asyncio
async def test_cookie_size_and_per_ip_probe_limit_prevent_backend_amplification():
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "/"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://cms.test"
    )
    auth = CMSAuthenticator(
        Settings(
            cms_contests=("contest",),
            cms_probe_rate_limit_count=1,
        ),
        client=client,
    )
    huge = await auth.authenticate({"contest_login": "x" * 9000}, "192.0.2.9")
    assert huge.status is CMSAuthStatus.UNAUTHENTICATED
    assert calls == 0
    first = await auth.authenticate({"contest_login": _v1("one")}, "192.0.2.9")
    second = await auth.authenticate({"contest_login": _v1("two")}, "192.0.2.9")
    assert first.status is CMSAuthStatus.UNAUTHENTICATED
    assert second.status is CMSAuthStatus.UNAVAILABLE
    assert calls == 1
    await client.aclose()

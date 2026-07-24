"""CMS-compatible contestant authentication.

CMS login cookies are opaque credentials. The authoritative identity always
comes from the authenticated CWS ``/api/toilet-auth`` response, which preserves
CMS IP restrictions, hidden-user, expiry, and refresh rules. Without a CMS login
cookie the Toilet service returns unauthenticated without probing CWS; it never
attempts CMS IP autologin.
"""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import hashlib
from http.cookies import SimpleCookie
import ipaddress
import json
import string
import time
from typing import Callable, Mapping
from urllib.parse import quote

import httpx

from .settings import Settings


MAX_COOKIE_FIELD_BYTES = 1024 * 1024
_HEX = frozenset(string.hexdigits)


class CookieDecodeError(ValueError):
    """The unsigned outer cookie or its CMS payload is malformed."""


@dataclass(frozen=True, slots=True)
class CookieHint:
    username: str
    cms_timestamp: float
    version: int


def _decode_payload(encoded: bytes, version: int) -> CookieHint:
    try:
        raw = base64.b64decode(encoded, validate=True)
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CookieDecodeError("invalid CMS cookie payload") from exc
    if not isinstance(value, list) or len(value) != 3:
        raise CookieDecodeError("CMS cookie payload must be a three-item list")
    username, _secret_value, inner_timestamp = value
    if not isinstance(username, str) or not username:
        raise CookieDecodeError("CMS cookie username must be a non-empty string")
    if isinstance(inner_timestamp, bool) or not isinstance(inner_timestamp, (int, float)):
        raise CookieDecodeError("CMS cookie timestamp must be numeric")
    return CookieHint(username=username, cms_timestamp=float(inner_timestamp), version=version)


def _length_field(data: bytes, offset: int) -> tuple[bytes, int]:
    colon = data.find(b":", offset)
    if colon < 0:
        raise CookieDecodeError("truncated Tornado v2 length field")
    length_text = data[offset:colon]
    if not length_text or not length_text.isdigit():
        raise CookieDecodeError("invalid Tornado v2 field length")
    length = int(length_text)
    if length > MAX_COOKIE_FIELD_BYTES:
        raise CookieDecodeError("Tornado v2 field is too large")
    start = colon + 1
    end = start + length
    if end > len(data):
        raise CookieDecodeError("truncated Tornado v2 field")
    if end >= len(data) or data[end : end + 1] != b"|":
        raise CookieDecodeError("missing Tornado v2 field separator")
    return data[start:end], end + 1


def parse_cms_cookie(raw_cookie: str, expected_cookie_name: str) -> CookieHint:
    """Decode the non-secret CMS payload from a Tornado v1/v2 signed cookie.

    This does *not* verify the signature and must never be used as authentication.
    The password/hash element is deliberately discarded and never returned.
    """

    try:
        data = raw_cookie.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CookieDecodeError("cookie is not UTF-8") from exc

    if data.startswith(b"2|"):
        offset = 2
        key_version, offset = _length_field(data, offset)
        outer_timestamp, offset = _length_field(data, offset)
        embedded_name, offset = _length_field(data, offset)
        encoded_value, offset = _length_field(data, offset)
        signature = data[offset:]
        if not key_version.isdigit() or not outer_timestamp.isdigit():
            raise CookieDecodeError("invalid Tornado v2 metadata")
        try:
            cookie_name = embedded_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CookieDecodeError("invalid embedded cookie name") from exc
        if cookie_name != expected_cookie_name:
            raise CookieDecodeError("embedded cookie name does not match")
        if len(signature) != 64 or any(chr(byte) not in _HEX for byte in signature):
            raise CookieDecodeError("invalid Tornado v2 signature field")
        return _decode_payload(encoded_value, 2)

    parts = data.split(b"|")
    if len(parts) != 3:
        if parts and parts[0].isdigit():
            raise CookieDecodeError("unsupported Tornado cookie version")
        raise CookieDecodeError("invalid Tornado v1 cookie")
    encoded_value, outer_timestamp, signature = parts
    if not outer_timestamp.isdigit():
        raise CookieDecodeError("invalid Tornado v1 timestamp")
    if len(signature) != 40 or any(chr(byte) not in _HEX for byte in signature):
        raise CookieDecodeError("invalid Tornado v1 signature field")
    return _decode_payload(encoded_value, 1)


class CMSAuthStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class CMSAuthResult:
    status: CMSAuthStatus
    username: str | None = None
    contest: str | None = None
    set_cookie_headers: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.status is CMSAuthStatus.AUTHENTICATED


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    result: CMSAuthResult


@dataclass(slots=True)
class _Flight:
    lock: asyncio.Lock
    users: int = 0


MAX_CMS_COOKIE_BYTES = 8192


class CMSAuthenticator:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.cms_base_url,
            follow_redirects=False,
            timeout=httpx.Timeout(settings.cms_timeout_seconds),
            trust_env=False,
        )
        self._cache: OrderedDict[tuple[str, str, str], _CacheEntry] = OrderedDict()
        self._flights: dict[tuple[str, str, str], _Flight] = {}
        self._probe_windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def normalize_client_ip(client_ip: str) -> str:
        try:
            return ipaddress.ip_address(client_ip).compressed
        except ValueError as exc:
            raise ValueError("invalid client IP address") from exc

    @staticmethod
    def _cookie_digest(raw_cookie: str) -> str:
        return hashlib.sha256(raw_cookie.encode("utf-8")).hexdigest()

    @staticmethod
    def _target_set_cookies(response: httpx.Response, cookie_name: str) -> tuple[str, ...]:
        result = []
        for header in response.headers.get_list("set-cookie"):
            first = header.split(";", 1)[0]
            if "=" not in first:
                continue
            name = first.split("=", 1)[0].strip()
            if name == cookie_name:
                result.append(header)
        return tuple(result)

    @staticmethod
    def _set_cookie_value(headers: tuple[str, ...], cookie_name: str) -> str | None:
        for header in headers:
            cookie = SimpleCookie()
            try:
                cookie.load(header)
            except Exception:
                continue
            if cookie_name in cookie and cookie[cookie_name].value:
                return cookie[cookie_name].value
        return None

    def _probe_path(self, contest: str) -> str:
        if self.settings.cms_multi_contest:
            return f"/{quote(contest, safe='')}/api/toilet-auth"
        return "/api/toilet-auth"

    def _allow_probe(self, client_ip: str) -> bool:
        now = self._clock()
        start, count = self._probe_windows.get(client_ip, (now, 0))
        if now - start >= self.settings.cms_probe_rate_limit_window_seconds:
            start, count = now, 0
        allowed = count < self.settings.cms_probe_rate_limit_count
        self._probe_windows[client_ip] = (start, count + 1 if allowed else count)
        self._probe_windows.move_to_end(client_ip)
        while len(self._probe_windows) > self.settings.cms_cache_max_entries:
            self._probe_windows.popitem(last=False)
        return allowed

    def _cache_get(self, key: tuple[str, str, str]) -> CMSAuthResult | None:
        item = self._cache.get(key)
        if item is None:
            return None
        if item.expires_at <= self._clock():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        # Set-Cookie is intentionally never replayed from cache.
        return CMSAuthResult(
            status=item.result.status,
            username=item.result.username,
            contest=item.result.contest,
            detail=item.result.detail,
        )

    def _cache_put(
        self,
        key: tuple[str, str, str],
        result: CMSAuthResult,
        refreshed_cookie: str | None = None,
    ) -> None:
        ttl = (
            self.settings.cms_positive_cache_seconds
            if result.authenticated
            else self.settings.cms_negative_cache_seconds
        )
        if ttl <= 0:
            return
        cached = CMSAuthResult(
            status=result.status,
            username=result.username,
            contest=result.contest,
            detail=result.detail,
        )
        expires = self._clock() + ttl
        now = self._clock()
        for expired_key in [
            existing_key
            for existing_key, item in self._cache.items()
            if item.expires_at <= now
        ]:
            self._cache.pop(expired_key, None)

        def put(cache_key: tuple[str, str, str]) -> None:
            self._cache[cache_key] = _CacheEntry(expires, cached)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self.settings.cms_cache_max_entries:
                self._cache.popitem(last=False)

        put(key)
        if refreshed_cookie:
            contest, _old_digest, ip = key
            refreshed_key = (contest, self._cookie_digest(refreshed_cookie), ip)
            put(refreshed_key)

    async def _probe(
        self,
        contest: str,
        raw_cookie: str,
        client_ip: str,
    ) -> CMSAuthResult:
        cookie_name = f"{contest}_login"
        if len(raw_cookie.encode("utf-8")) > MAX_CMS_COOKIE_BYTES:
            return CMSAuthResult(
                CMSAuthStatus.UNAUTHENTICATED, detail="CMS login cookie is too large"
            )
        # Cookie decoding is only a routing hint. Keep the exact raw-cookie
        # digest in the authority cache so a future CMS cookie format cannot
        # accidentally share another cookie's verdict.
        key = (contest, self._cookie_digest(raw_cookie), client_ip)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        flight = self._flights.get(key)
        if flight is None:
            flight = _Flight(asyncio.Lock())
            self._flights[key] = flight
        flight.users += 1
        try:
            async with flight.lock:
                cached = self._cache_get(key)
                if cached is not None:
                    return cached
                if not self._allow_probe(client_ip):
                    result = CMSAuthResult(
                        CMSAuthStatus.UNAVAILABLE,
                        detail="CMS authentication probe rate limit exceeded",
                    )
                    self._cache_put(key, result)
                    return result
                headers = {"X-Forwarded-For": client_ip, "Accept": "application/json"}
                # AsyncClient keeps a cookie jar by default. CMS responses refresh
                # the browser cookie, but that credential belongs to the caller and
                # must never become ambient state on this shared backend client.
                # Build the request for the client's transport/timeout settings, then
                # replace its Cookie header with exactly this browser's credential.
                request = self._client.build_request(
                    "GET", self._probe_path(contest), headers=headers
                )
                request.headers["Cookie"] = f"{cookie_name}={raw_cookie}"
                try:
                    response = await self._client.send(
                        request, follow_redirects=False
                    )
                except httpx.HTTPError as exc:
                    result = CMSAuthResult(
                        CMSAuthStatus.UNAVAILABLE, detail=type(exc).__name__
                    )
                    self._cache_put(key, result)
                    return result
                set_cookies = self._target_set_cookies(response, cookie_name)
                if response.status_code >= 500:
                    result = CMSAuthResult(
                        CMSAuthStatus.UNAVAILABLE,
                        set_cookie_headers=set_cookies,
                        detail=f"CMS returned {response.status_code}",
                    )
                    self._cache_put(key, result)
                    return result
                if response.status_code != 200:
                    expected_logout = response.status_code == 401 or (
                        response.status_code in {301, 302, 303, 307, 308}
                        and bool(response.headers.get("location"))
                    )
                    result = CMSAuthResult(
                        (
                            CMSAuthStatus.UNAUTHENTICATED
                            if expected_logout
                            else CMSAuthStatus.UNAVAILABLE
                        ),
                        set_cookie_headers=set_cookies,
                        detail=f"CMS returned {response.status_code}",
                    )
                    self._cache_put(key, result)
                    return result
                try:
                    payload = response.json()
                    username = payload["username"]
                    returned_contest = payload["contest"]
                    if not isinstance(username, str) or not username:
                        raise ValueError("invalid username")
                    if returned_contest != contest:
                        raise ValueError("contest mismatch")
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    result = CMSAuthResult(
                        CMSAuthStatus.UNAVAILABLE,
                        set_cookie_headers=set_cookies,
                        detail="malformed CMS identity response",
                    )
                    self._cache_put(key, result)
                    return result
                result = CMSAuthResult(
                    CMSAuthStatus.AUTHENTICATED,
                    username=username,
                    contest=contest,
                    set_cookie_headers=set_cookies,
                )
                refreshed = self._set_cookie_value(set_cookies, cookie_name)
                self._cache_put(key, result, refreshed)
                return result
        finally:
            flight.users -= 1
            if flight.users == 0 and self._flights.get(key) is flight:
                self._flights.pop(key, None)

    async def authenticate(
        self,
        cookies: Mapping[str, str],
        client_ip: str,
    ) -> CMSAuthResult:
        credentialed_contests = [
            (contest, cookies.get(f"{contest}_login"))
            for contest in self.settings.cms_contests
            if cookies.get(f"{contest}_login")
        ]
        if not credentialed_contests:
            return CMSAuthResult(CMSAuthStatus.UNAUTHENTICATED)

        ip = self.normalize_client_ip(client_ip)
        results = []
        for contest, raw_cookie in sorted(credentialed_contests):
            assert raw_cookie is not None
            result = await self._probe(contest, raw_cookie, ip)
            results.append(result)

        unavailable = [r for r in results if r.status is CMSAuthStatus.UNAVAILABLE]
        authenticated = [r for r in results if r.authenticated]
        relays = tuple(
            header for result in results for header in result.set_cookie_headers
        )
        if unavailable:
            return CMSAuthResult(
                CMSAuthStatus.UNAVAILABLE,
                set_cookie_headers=relays,
                detail="one or more configured CMS contests are unavailable",
            )
        identities = {(r.username, r.contest) for r in authenticated}
        usernames = {r.username for r in authenticated}
        if len(usernames) > 1:
            return CMSAuthResult(
                CMSAuthStatus.AMBIGUOUS,
                set_cookie_headers=relays,
                detail="configured contests authenticated different users",
            )
        if authenticated:
            selected = authenticated[0]
            return CMSAuthResult(
                CMSAuthStatus.AUTHENTICATED,
                username=selected.username,
                contest=selected.contest,
                set_cookie_headers=relays,
                detail=("authenticated in multiple contests" if len(identities) > 1 else None),
            )
        return CMSAuthResult(
            CMSAuthStatus.UNAUTHENTICATED, set_cookie_headers=relays
        )

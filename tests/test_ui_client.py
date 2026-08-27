from __future__ import annotations

import errno
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from te_agent_migrate.errors import AuthenticationError, UiError
from te_agent_migrate.http import RetryPolicy
from te_agent_migrate.ui_client import TevaUiClient


def sequence_transport(responses: Iterator[tuple[int, dict[str, str], Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        status, headers, body = next(responses)
        return httpx.Response(status, headers=headers, json=body, request=request)

    return httpx.MockTransport(handler)


def test_authentication_reuses_rotated_cookie_and_csrf() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.scheme == "https"
        # HTTPX omits a scheme's default port when normalizing the URL;
        # implicit HTTPS still connects on TCP 443.
        assert request.url.port in {None, 443}
        if request.url.path == "/login":
            return httpx.Response(
                200,
                headers={
                    "X-NewCSRFToken": "csrf-one",
                    "Set-Cookie": "__Secure-tevasid=session-one; Secure; HttpOnly; Path=/",
                },
                request=request,
            )
        if request.url.path == "/api/session":
            return httpx.Response(401, headers={"X-NewCSRFToken": "csrf-two"}, request=request)
        if request.url.path == "/api/login":
            assert request.headers["x-csrftoken"] == "csrf-two"
            assert "session-one" in request.headers["cookie"]
            return httpx.Response(
                200,
                headers={"Set-Cookie": "__Secure-tevasid=session-two; Secure; HttpOnly; Path=/"},
                json={"success": True},
                request=request,
            )
        if request.url.path == "/api/app-config":
            assert "session-two" in request.headers["cookie"]
            return httpx.Response(
                200, headers={"X-NewCSRFToken": "csrf-three"}, json={}, request=request
            )
        raise AssertionError(request.url.path)

    client = TevaUiClient(
        "192.0.2.10",
        "admin",
        "password",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )
    client.authenticate()

    assert client.authenticated
    assert [request.url.path for request in requests] == [
        "/login",
        "/api/session",
        "/api/login",
        "/api/app-config",
    ]


def test_codex_sandbox_network_failure_has_actionable_https_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("No route to host", request=request)

    client = TevaUiClient(
        "192.0.2.10",
        "admin",
        "password",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )

    with pytest.raises(UiError, match=r"HTTPS/443.*Codex network sandbox"):
        client.authenticate()


def test_no_route_error_identifies_direct_transport_and_macos_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        os_error = OSError(errno.EHOSTUNREACH, "No route to host")
        raise httpx.ConnectError(str(os_error), request=request) from os_error

    client = TevaUiClient(
        "192.0.2.10",
        "admin",
        "password",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )

    with pytest.raises(
        UiError,
        match=r"HTTPS/443.*bypasses environment proxies.*Privacy & Security > Local Network",
    ):
        client.authenticate()


def test_http_200_login_with_success_false_is_rejected() -> None:
    responses = iter(
        [
            (200, {"X-NewCSRFToken": "csrf"}, {}),
            (401, {"X-NewCSRFToken": "csrf"}, {}),
            (200, {}, {"success": False}),
        ]
    )
    client = TevaUiClient(
        "192.0.2.10",
        "admin",
        "wrong",
        transport=sequence_transport(responses),
        retry_policy=RetryPolicy(attempts=1),
    )

    with pytest.raises(AuthenticationError, match="failed"):
        client.authenticate()


def test_csrf_403_discards_session_and_requires_fresh_login() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, headers={"X-NewCSRFToken": "csrf"}, request=request)
        if calls == 2:
            return httpx.Response(401, headers={"X-NewCSRFToken": "csrf"}, request=request)
        if calls == 3:
            return httpx.Response(200, json={"success": True}, request=request)
        if calls == 4:
            return httpx.Response(200, headers={"X-NewCSRFToken": "csrf"}, json={}, request=request)
        return httpx.Response(403, text="Forbidden", request=request)

    client = TevaUiClient(
        "192.0.2.10",
        "admin",
        "password",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )
    client.authenticate()

    with pytest.raises(AuthenticationError, match="fresh login"):
        client.refresh_csrf()
    assert not client.authenticated


def test_warning_token_response_is_not_treated_as_success_or_echoed() -> None:
    calls = 0
    secret = "A" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, headers={"X-NewCSRFToken": "csrf"}, request=request)
        if calls == 2:
            return httpx.Response(401, headers={"X-NewCSRFToken": "csrf"}, request=request)
        if calls == 3:
            return httpx.Response(200, json={"success": True}, request=request)
        if calls in {4, 5}:
            return httpx.Response(200, headers={"X-NewCSRFToken": "csrf"}, json={}, request=request)
        return httpx.Response(
            200,
            json={"message": {"category": "warn"}, "currentToken": secret},
            request=request,
        )

    client = TevaUiClient(
        "192.0.2.10",
        "admin",
        "password",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(attempts=1),
    )
    client.authenticate()

    with pytest.raises(UiError) as caught:
        client.set_account_group_token(secret, browserbot=True, crash_reports=True)
    assert secret not in str(caught.value)

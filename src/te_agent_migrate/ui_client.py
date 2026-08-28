"""HTTPS-only client for the Enterprise Agent Flask UI."""

from __future__ import annotations

import errno
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any

import httpx

from te_agent_migrate.errors import AuthenticationError, UiError
from te_agent_migrate.http import TRANSIENT_STATUS_CODES, RetryPolicy
from te_agent_migrate.models import LocalSnapshot

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{32}$")


def _is_no_route_to_host(exc: BaseException) -> bool:
    """Recognize EHOSTUNREACH through HTTPX/HTTPCore exception chaining."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, OSError) and current.errno == errno.EHOSTUNREACH:
            return True
        current = current.__cause__
    return "[Errno 65]" in str(exc) or "No route to host" in str(exc)


class TevaUiClient:
    """One isolated cookie/CSRF session for exactly one appliance."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 15.0,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.host = host
        self.base_url = f"https://{host}"
        self._username = username
        self._password = password
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._csrf: str | None = None
        self._authenticated = False
        self._client = httpx.Client(
            base_url=self.base_url,
            verify=False,
            trust_env=False,
            follow_redirects=False,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json, text/plain, */*"},
        )

    def __enter__(self) -> TevaUiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def _update_session_state(self, response: httpx.Response) -> None:
        token = response.headers.get("x-newcsrftoken") or response.headers.get("x-csrftoken")
        if token:
            self._csrf = token
        # Some appliances have a clock that makes a freshly issued cookie look expired.
        # Preserve only the UI session cookie value; never log it.
        for raw_cookie in response.headers.get_list("set-cookie"):
            cookie = SimpleCookie()
            cookie.load(raw_cookie)
            for name, morsel in cookie.items():
                if name == "__Secure-tevasid":
                    self._client.cookies.set(name, morsel.value, domain=self.host, path="/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        safe_to_retry: bool,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        attempts = self._retry.attempts if safe_to_retry else 1
        response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(method, path, headers=headers, json=json_body)
                self._update_session_state(response)
            except httpx.RequestError as exc:
                if attempt == attempts:
                    if (
                        os.getenv("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
                        and os.getenv("TE_ALLOW_CODEX_SANDBOX_NETWORK") != "1"
                    ):
                        raise UiError(
                            f"{method} {path} could not reach {self.host} over HTTPS/443 because "
                            "the Codex network sandbox blocks direct LAN connections; run from "
                            "macOS Terminal outside Codex"
                        ) from exc
                    if _is_no_route_to_host(exc):
                        raise UiError(
                            f"{method} {path} could not reach {self.host} over HTTPS/443: "
                            "no route to host. The UI client already bypasses environment "
                            "proxies. Check the active LAN/VPN route and local firewall; on "
                            "macOS, allow your terminal app in System Settings > Privacy & "
                            "Security > Local Network"
                        ) from exc
                    raise UiError(f"{method} {path} could not reach {self.host}: {exc}") from exc
                self._sleep(self._retry.delay(attempt))
                continue
            if response.status_code in expected:
                return response
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < attempts:
                retry_after = response.headers.get("retry-after")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else self._retry.delay(attempt)
                )
                self._sleep(delay)
                continue
            break
        assert response is not None
        if response.status_code == 403:
            self._authenticated = False
            self._csrf = None
            self._client.cookies.clear()
            raise AuthenticationError(
                f"{self.host} rejected the CSRF/session state; a fresh login is required"
            )
        raise UiError(f"{method} {path} returned HTTP {response.status_code} on {self.host}")

    def authenticate(self) -> None:
        """Bootstrap cookies and CSRF once, then retain them for later operations."""
        self._authenticated = False
        self._csrf = None
        self._client.cookies.clear()
        self._request("GET", "/login", expected={200}, safe_to_retry=True)
        self._request("GET", "/api/session", expected={200, 401}, safe_to_retry=True)
        if not self._csrf:
            raise AuthenticationError(f"No CSRF token was returned by {self.host}")
        response = self._request(
            "POST",
            "/api/login",
            expected={200},
            safe_to_retry=True,
            headers={"X-CSRFToken": self._csrf, "Referer": f"{self.base_url}/login"},
            json_body={"username": self._username, "password": self._password},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthenticationError(f"Login response from {self.host} was not JSON") from exc
        if payload.get("success") is not True:
            raise AuthenticationError(f"UI authentication failed for {self.host}")
        self._authenticated = True
        # The browser obtains the current modifying-call token here after login.
        try:
            self._request("GET", "/api/app-config", expected={200}, safe_to_retry=True)
        except UiError:
            self._request("GET", "/api/session", expected={200}, safe_to_retry=True)

    def _auth_headers(self, referer_path: str = "/") -> dict[str, str]:
        if not self._authenticated or not self._csrf:
            raise AuthenticationError(f"No authenticated UI session exists for {self.host}")
        return {"X-CSRFToken": self._csrf, "Referer": f"{self.base_url}{referer_path}"}

    def refresh_csrf(self) -> None:
        """Refresh the current token without replacing the authenticated cookie jar."""
        if not self._authenticated:
            raise AuthenticationError(f"No authenticated UI session exists for {self.host}")
        try:
            self._request("GET", "/api/app-config", expected={200}, safe_to_retry=True)
        except UiError:
            self._request("GET", "/api/session", expected={200}, safe_to_retry=True)
        if not self._csrf:
            raise AuthenticationError(f"CSRF refresh returned no token for {self.host}")

    def get_json(self, path: str, *, referer_path: str = "/") -> dict[str, Any]:
        response = self._request(
            "GET",
            path,
            expected={200},
            safe_to_retry=True,
            headers=self._auth_headers(referer_path),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UiError(f"GET {path} returned invalid JSON on {self.host}") from exc
        if not isinstance(payload, dict):
            raise UiError(f"GET {path} returned an unexpected JSON shape on {self.host}")
        return payload

    def snapshot_state(self) -> LocalSnapshot:
        state: dict[str, Any] = {}
        endpoint_map = {
            "config_data": ("/api/config-data", "/settings"),
            "agent_setup_status": ("/api/agent_setup_status", "/agent"),
            "app_config": ("/api/app-config", "/"),
            "certificates": ("/api/access/ssl/crt", "/ssl"),
        }
        for key, (path, referer) in endpoint_map.items():
            try:
                state[key] = self.get_json(path, referer_path=referer)
            except UiError as exc:
                state[key] = {"capture_error": str(exc)}
        return LocalSnapshot(
            agent=self.host,
            captured_at=datetime.now(UTC).isoformat(),
            state=state,
        )

    def is_reachable(self) -> bool:
        try:
            self._request("GET", "/login", expected={200}, safe_to_retry=True)
        except UiError:
            return False
        return True

    def reset_agent(self) -> None:
        self.refresh_csrf()
        response = self._request(
            "POST",
            "/api/resetstate",
            expected={200},
            safe_to_retry=False,
            headers=self._auth_headers("/advanced"),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UiError(f"Reset response from {self.host} was not JSON") from exc
        message = payload.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else str(message)
        if "reset" not in str(content).lower():
            raise UiError(f"Reset response from {self.host} did not confirm reset")

    def set_account_group_token(
        self,
        account_token: str,
        *,
        browserbot: bool,
        crash_reports: bool,
    ) -> None:
        if not TOKEN_PATTERN.fullmatch(account_token):
            raise UiError("Target account-group token must be exactly 32 alphanumeric characters")
        if not self._authenticated:
            self.authenticate()
        self.refresh_csrf()
        response = self._request(
            "POST",
            "/api/agent",
            expected={200},
            safe_to_retry=False,
            headers=self._auth_headers("/agent"),
            json_body={
                "account_token": account_token,
                "browserbot": "yes" if browserbot else "no",
                "crash_reports": "yes" if crash_reports else "no",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UiError(f"Token response from {self.host} was not JSON") from exc
        message = payload.get("message", {})
        category = message.get("category") if isinstance(message, dict) else None
        if category != "success":
            # A warning can contain the current secret token; never include the body.
            raise UiError(
                f"Token configuration was not accepted by {self.host} (category={category})"
            )


def boolean_from_snapshot(snapshot: LocalSnapshot, key: str, *, default: bool) -> bool:
    """Find a boolean-like appliance setting without depending on one TEVA version's shape."""
    wanted = key.casefold()

    def walk(value: Any) -> bool | None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key.casefold() == wanted:
                    if isinstance(child, bool):
                        return child
                    if isinstance(child, str) and child.casefold() in {
                        "yes",
                        "true",
                        "enabled",
                        "on",
                    }:
                        return True
                    if isinstance(child, str) and child.casefold() in {
                        "no",
                        "false",
                        "disabled",
                        "off",
                    }:
                        return False
                result = walk(child)
                if result is not None:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = walk(child)
                if result is not None:
                    return result
        return None

    result = walk(snapshot.state)
    return default if result is None else result

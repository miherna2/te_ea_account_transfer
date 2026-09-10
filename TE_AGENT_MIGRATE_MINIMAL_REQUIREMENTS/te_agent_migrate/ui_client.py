"""HTTPS-only Enterprise Agent UI client using one persistent Requests session."""

import errno
import os
import re
import ssl
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import urljoin

import requests
import urllib3

from te_agent_migrate.errors import AuthenticationError, UiError
from te_agent_migrate.http import RetryPolicy, TRANSIENT_STATUS_CODES
from te_agent_migrate.models import LocalSnapshot


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{32}$")


def _exception_chain(exc):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)


def _is_no_route_to_host(exc):
    for current in _exception_chain(exc):
        if isinstance(current, OSError) and current.errno in (
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
        ):
            return True
    text = str(exc).lower()
    return (
        "no route to host" in text
        or "network is unreachable" in text
        or "[errno 65]" in text
        or "[errno 51]" in text
    )


def _tls_runtime_description():
    """Return TLS capability details available on Python 3.6 and newer."""
    return "%s; TLS 1.3 support=%s" % (
        getattr(ssl, "OPENSSL_VERSION", "unknown SSL backend"),
        "yes" if getattr(ssl, "HAS_TLSv1_3", False) else "no",
    )


def _tls_error_message(method, path, host, exc):
    message = (
        "%s %s could not negotiate TLS with %s over HTTPS/443. "
        "Python SSL runtime: %s. The appliance rejected the offered TLS "
        "protocol or cipher before HTTP authentication; certificate verification "
        "is already disabled for the self-signed appliance certificate. "
        "Use a Python interpreter linked to a TLS 1.3-capable SSL library "
        "(OpenSSL 1.1.1 or newer), then recreate the virtual environment"
        % (method, path, host, _tls_runtime_description())
    )
    if exc:
        message += ". Original TLS error: %s" % exc
    return message


def _set_cookie_headers(response):
    """Return individual Set-Cookie values without splitting Expires dates."""
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    if raw_headers is not None:
        for method_name in ("getlist", "get_all"):
            method = getattr(raw_headers, method_name, None)
            if method is not None:
                values = method("Set-Cookie")
                if values:
                    return list(values)
    value = response.headers.get("Set-Cookie")
    return [value] if value else []


class TevaUiClient(object):
    """One isolated cookie and CSRF session for exactly one appliance."""

    def __init__(
        self,
        host,
        username,
        password,
        timeout_seconds=15.0,
        retry_policy=None,
        session=None,
        sleep=time.sleep,
    ):
        self.host = host
        self.base_url = "https://%s" % host
        self._username = username
        self._password = password
        self._timeout_seconds = timeout_seconds
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._csrf = None
        self._authenticated = False
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.headers.update({"Accept": "application/json, text/plain, */*"})

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        self._session.close()

    @property
    def authenticated(self):
        return self._authenticated

    def _update_session_state(self, response):
        token = response.headers.get("x-newcsrftoken") or response.headers.get("x-csrftoken")
        if token:
            self._csrf = token
        # A misconfigured appliance clock can make a new session cookie appear
        # expired. Preserve only the TEVA session cookie in this isolated jar.
        for raw_cookie in _set_cookie_headers(response):
            cookie = SimpleCookie()
            try:
                cookie.load(raw_cookie)
            except Exception:
                continue
            morsel = cookie.get("__Secure-tevasid")
            if morsel is not None:
                self._session.cookies.set(
                    "__Secure-tevasid",
                    morsel.value,
                    domain=self.host,
                    path="/",
                    secure=True,
                )

    def _request(
        self,
        method,
        path,
        expected,
        safe_to_retry,
        headers=None,
        json_body=None,
    ):
        attempts = self._retry.attempts if safe_to_retry else 1
        response = None
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        for attempt in range(1, attempts + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=self._timeout_seconds,
                    verify=False,
                    allow_redirects=False,
                )
                self._update_session_state(response)
            except requests.RequestException as exc:
                if attempt == attempts:
                    if isinstance(exc, requests.exceptions.SSLError):
                        raise UiError(_tls_error_message(method, path, self.host, exc))
                    if (
                        os.getenv("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
                        and os.getenv("TE_ALLOW_CODEX_SANDBOX_NETWORK") != "1"
                    ):
                        raise UiError(
                            "%s %s could not reach %s over HTTPS/443 because the Codex "
                            "network sandbox blocks direct LAN connections; run from a local "
                            "terminal outside Codex" % (method, path, self.host)
                        )
                    if _is_no_route_to_host(exc):
                        raise UiError(
                            "%s %s could not reach %s over HTTPS/443: no route to host. "
                            "The UI client already bypasses environment proxies. Check the "
                            "active LAN/VPN route and local firewall; on macOS, allow your "
                            "terminal app in System Settings > Privacy & Security > Local Network"
                            % (method, path, self.host)
                        )
                    raise UiError("%s %s could not reach %s: %s" % (method, path, self.host, exc))
                self._sleep(self._retry.delay(attempt))
                continue
            if response.status_code in expected:
                return response
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < attempts:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else self._retry.delay(attempt)
                self._sleep(delay)
                continue
            break
        if response.status_code == 403:
            self._authenticated = False
            self._csrf = None
            self._session.cookies.clear()
            raise AuthenticationError(
                "%s rejected the CSRF/session state; a fresh login is required" % self.host
            )
        raise UiError(
            "%s %s returned HTTP %s on %s"
            % (method, path, response.status_code, self.host)
        )

    def authenticate(self):
        """Bootstrap once and retain the same cookie jar for later operations."""
        self._authenticated = False
        self._csrf = None
        self._session.cookies.clear()
        self._request("GET", "/login", set((200,)), True)
        self._request("GET", "/api/session", set((200, 401)), True)
        if not self._csrf:
            raise AuthenticationError("No CSRF token was returned by %s" % self.host)
        response = self._request(
            "POST",
            "/api/login",
            set((200,)),
            True,
            headers={
                "X-CSRFToken": self._csrf,
                "Referer": "%s/login" % self.base_url,
            },
            json_body={"username": self._username, "password": self._password},
        )
        try:
            payload = response.json()
        except ValueError:
            raise AuthenticationError("Login response from %s was not JSON" % self.host)
        if payload.get("success") is not True:
            raise AuthenticationError("UI authentication failed for %s" % self.host)
        self._authenticated = True
        try:
            self._request("GET", "/api/app-config", set((200,)), True)
        except UiError:
            self._request("GET", "/api/session", set((200,)), True)

    def _auth_headers(self, referer_path="/"):
        if not self._authenticated or not self._csrf:
            raise AuthenticationError("No authenticated UI session exists for %s" % self.host)
        return {
            "X-CSRFToken": self._csrf,
            "Referer": "%s%s" % (self.base_url, referer_path),
        }

    def refresh_csrf(self):
        """Refresh the token while retaining the authenticated cookie jar."""
        if not self._authenticated:
            raise AuthenticationError("No authenticated UI session exists for %s" % self.host)
        try:
            self._request("GET", "/api/app-config", set((200,)), True)
        except UiError:
            self._request("GET", "/api/session", set((200,)), True)
        if not self._csrf:
            raise AuthenticationError("CSRF refresh returned no token for %s" % self.host)

    def get_json(self, path, referer_path="/"):
        response = self._request(
            "GET",
            path,
            set((200,)),
            True,
            headers=self._auth_headers(referer_path),
        )
        try:
            payload = response.json()
        except ValueError:
            raise UiError("GET %s returned invalid JSON on %s" % (path, self.host))
        if not isinstance(payload, dict):
            raise UiError("GET %s returned an unexpected JSON shape on %s" % (path, self.host))
        return payload

    def snapshot_state(self):
        state = {}
        endpoint_map = {
            "config_data": ("/api/config-data", "/settings"),
            "agent_setup_status": ("/api/agent_setup_status", "/agent"),
            "app_config": ("/api/app-config", "/"),
            "certificates": ("/api/access/ssl/crt", "/ssl"),
        }
        for key, values in endpoint_map.items():
            path, referer = values
            try:
                state[key] = self.get_json(path, referer_path=referer)
            except UiError as exc:
                state[key] = {"capture_error": str(exc)}
        return LocalSnapshot(
            agent=self.host,
            captured_at=datetime.now(timezone.utc).isoformat(),
            state=state,
        )

    def is_reachable(self):
        try:
            self._request("GET", "/login", set((200,)), True)
        except UiError:
            return False
        return True

    def reset_agent(self):
        self.refresh_csrf()
        response = self._request(
            "POST",
            "/api/resetstate",
            set((200,)),
            False,
            headers=self._auth_headers("/advanced"),
        )
        try:
            payload = response.json()
        except ValueError:
            raise UiError("Reset response from %s was not JSON" % self.host)
        message = payload.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else str(message)
        if "reset" not in str(content).lower():
            raise UiError("Reset response from %s did not confirm reset" % self.host)

    def set_account_group_token(self, account_token, browserbot, crash_reports):
        if not TOKEN_PATTERN.fullmatch(account_token):
            raise UiError("Target account-group token must be exactly 32 alphanumeric characters")
        if not self._authenticated:
            self.authenticate()
        self.refresh_csrf()
        response = self._request(
            "POST",
            "/api/agent",
            set((200,)),
            False,
            headers=self._auth_headers("/agent"),
            json_body={
                "account_token": account_token,
                "browserbot": "yes" if browserbot else "no",
                "crash_reports": "yes" if crash_reports else "no",
            },
        )
        try:
            payload = response.json()
        except ValueError:
            raise UiError("Token response from %s was not JSON" % self.host)
        message = payload.get("message", {})
        category = message.get("category") if isinstance(message, dict) else None
        if category != "success":
            raise UiError(
                "Token configuration was not accepted by %s (category=%s)"
                % (self.host, category)
            )


def boolean_from_snapshot(snapshot, key, default):
    wanted = key.casefold()

    def walk(value):
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key.casefold() == wanted:
                    if isinstance(child, bool):
                        return child
                    if isinstance(child, str) and child.casefold() in (
                        "yes",
                        "true",
                        "enabled",
                        "on",
                    ):
                        return True
                    if isinstance(child, str) and child.casefold() in (
                        "no",
                        "false",
                        "disabled",
                        "off",
                    ):
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

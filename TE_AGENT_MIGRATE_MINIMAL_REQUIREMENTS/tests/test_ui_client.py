import unittest

import requests

from te_agent_migrate.errors import UiError
from te_agent_migrate.ui_client import TevaUiClient


class FakeRawHeaders(object):
    def __init__(self, cookies=None):
        self.cookies = cookies or []

    def getlist(self, name):
        return self.cookies if name.lower() == "set-cookie" else []


class FakeRaw(object):
    def __init__(self, cookies=None):
        self.headers = FakeRawHeaders(cookies)


class FakeResponse(object):
    def __init__(self, status=200, payload=None, headers=None, cookies=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.content = b"{}"
        self.raw = FakeRaw(cookies)

    def json(self):
        return self._payload


class FakeSession(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


class FailingTlsSession(FakeSession):
    def __init__(self):
        FakeSession.__init__(self, [])

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        raise requests.exceptions.SSLError("tlsv1 alert protocol version")


class UiClientTests(unittest.TestCase):
    def test_authentication_reuses_session_and_rotates_csrf(self):
        session = FakeSession(
            [
                FakeResponse(
                    headers={"x-newcsrftoken": "csrf-login"},
                    cookies=["__Secure-tevasid=session-value; Path=/; Secure"],
                ),
                FakeResponse(headers={"x-newcsrftoken": "csrf-session"}),
                FakeResponse(payload={"success": True}, headers={"x-newcsrftoken": "csrf-auth"}),
                FakeResponse(headers={"x-newcsrftoken": "csrf-current"}),
            ]
        )
        client = TevaUiClient("192.0.2.10", "admin", "password", session=session)
        client.authenticate()
        self.assertTrue(client.authenticated)
        self.assertFalse(session.trust_env)
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "POST", "GET"])
        self.assertEqual(session.calls[2][2]["headers"]["X-CSRFToken"], "csrf-session")
        self.assertEqual(client._csrf, "csrf-current")
        self.assertEqual(session.cookies.get("__Secure-tevasid"), "session-value")
        self.assertTrue(all(call[1].startswith("https://192.0.2.10/") for call in session.calls))
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))

    def test_modifying_call_refreshes_csrf_in_same_session(self):
        session = FakeSession(
            [
                FakeResponse(headers={"x-newcsrftoken": "csrf-new"}),
                FakeResponse(payload={"message": {"category": "success"}}),
            ]
        )
        client = TevaUiClient("192.0.2.10", "admin", "password", session=session)
        client._authenticated = True
        client._csrf = "csrf-old"
        client.set_account_group_token("a" * 32, browserbot=True, crash_reports=False)
        self.assertEqual(session.calls[0][1], "https://192.0.2.10/api/app-config")
        self.assertEqual(session.calls[1][2]["headers"]["X-CSRFToken"], "csrf-new")

    def test_tls_protocol_failure_reports_runtime_and_remediation(self):
        client = TevaUiClient(
            "192.0.2.10",
            "admin",
            "password",
            session=FailingTlsSession(),
            retry_policy=type(
                "OneAttemptPolicy",
                (object,),
                {"attempts": 1, "delay": staticmethod(lambda unused: 0)},
            )(),
        )
        with self.assertRaises(UiError) as caught:
            client.authenticate()
        message = str(caught.exception)
        self.assertIn("could not negotiate TLS", message)
        self.assertIn("Python SSL runtime", message)
        self.assertIn("TLS 1.3-capable", message)
        self.assertIn("recreate the virtual environment", message)


if __name__ == "__main__":
    unittest.main()

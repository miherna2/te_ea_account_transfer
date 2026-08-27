from te_agent_migrate.redaction import REDACTED, redact_headers, redact_value


def test_redacts_secret_keys_and_nested_values():
    payload = {
        "account_token": "A" * 32,
        "nested": {"Authorization": "Bearer abc.def.ghi", "safe": "ok"},
        "list": ["prefix AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA suffix"],
    }
    redacted = redact_value(payload)
    assert redacted["account_token"] == REDACTED
    assert redacted["nested"]["Authorization"] == REDACTED
    assert redacted["nested"]["safe"] == "ok"
    assert REDACTED in redacted["list"][0]


def test_redacts_headers_case_insensitively():
    assert redact_headers({"X-CSRFToken": "abc", "Accept": "json"}) == {
        "X-CSRFToken": REDACTED,
        "Accept": "json",
    }

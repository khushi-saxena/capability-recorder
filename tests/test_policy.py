from src.policy.policy import Allow, Deny, Policy, RequireConfirmation


policy = Policy()


def test_off_origin_navigate_denied():
    decision = policy.check_navigate("https://example.com/console")

    assert isinstance(decision, Deny)


def test_path_outside_allowlist_denied():
    decision = policy.check_navigate("http://127.0.0.1:8800/admin")

    assert isinstance(decision, Deny)


def test_subaccount_confirm_requires_confirmation():
    decision = policy.check_action(
        "activate", "http://127.0.0.1:8800/subaccount/confirm", "safe"
    )

    assert isinstance(decision, RequireConfirmation)


def test_ssn_shaped_string_is_redacted():
    assert policy.redact("SSN: 123-45-6789") == "[REDACTED]: [REDACTED]"

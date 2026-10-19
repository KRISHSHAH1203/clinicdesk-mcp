import pytest

from clinicdesk.auth import AuthError, issue_token, verify_token

S = "test-secret"


def test_round_trip():
    c = verify_token(S, issue_token(S, "u1", "clinic-a", "front_desk"))
    assert (c.subject, c.tenant, c.role) == ("u1", "clinic-a", "front_desk")


def test_tampered_token_rejected():
    tok = issue_token(S, "u1", "clinic-a", "clinician")
    body, sig = tok.split(".")
    forged = issue_token("other-secret", "u1", "clinic-a", "front_desk").split(".")[0]
    with pytest.raises(AuthError):
        verify_token(S, f"{forged}.{sig}")


def test_expired_token_rejected():
    tok = issue_token(S, "u1", "clinic-a", "agent", ttl_seconds=10, now=1000)
    with pytest.raises(AuthError, match="expired"):
        verify_token(S, tok, now=2000)


def test_scope_check():
    c = verify_token(S, issue_token(S, "u1", "clinic-a", "clinician"))
    c.require("patients:read")
    with pytest.raises(AuthError):
        c.require("appointments:write")

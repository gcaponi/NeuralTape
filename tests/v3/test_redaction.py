"""Test per lex/v3/redaction.py (D0.1)."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure nt_v3 package is registered by run_all.py loader.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from redaction import Redactor  # type: ignore[import-not-found]


def test_aws_access_key():
    r = Redactor()
    out, ev = r.redact("connect using AKIAIOSFODNN7EXAMPLE as the key")
    assert "[REDACTED:aws-access-key-id]" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert any(e.kind == "aws-access-key-id" for e in ev)


def test_jwt_token():
    r = Redactor()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out, ev = r.redact(f"Authorization: Bearer {jwt}")
    assert jwt not in out
    assert any(e.kind in ("jwt", "bearer-token") for e in ev)


def test_github_token():
    r = Redactor()
    tok = "ghp_" + "a" * 36
    out, ev = r.redact(f"git remote add origin https://{tok}@github.com/x/y.git")
    assert tok not in out
    assert any(e.kind == "github-token" for e in ev)


def test_private_key_pem():
    r = Redactor()
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAxxxxxx\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, ev = r.redact(f"here is the key:\n{pem}\ndone")
    assert "MIIEpAIBAA" not in out
    assert any(e.kind == "private-key-pem" for e in ev)


def test_url_credentials():
    r = Redactor()
    out, ev = r.redact("postgres://user:secretpass123@db.example.com:5432/mydb")
    assert "secretpass123" not in out
    assert any(e.kind == "url-credentials" for e in ev)


def test_generic_assignment_env_block():
    r = Redactor()
    text = "API_KEY=ak_live_1234567890abcdefghijklm\nPASSWORD=thisismypassword12345"
    out, ev = r.redact(text)
    assert "ak_live_1234567890abcdefghijklm" not in out
    assert "thisismypassword12345" not in out
    # At least 2 generic-assignment or other redactions occurred.
    assert len(ev) >= 2


def test_no_false_positive_function_def():
    """Critical: legitimate code must not be mangled."""
    r = Redactor()
    code = (
        "def api_key_handler(request):\n"
        "    return JsonResponse({'token_count': 42})\n"
        "class SecretManager:\n"
        "    pass\n"
    )
    out, ev = r.redact(code)
    # Function defs and class names that don't contain an actual secret assignment
    # must NOT be redacted. The number regex in generic-assignment requires '=' or ':'
    # followed by a 16+ char opaque value, which the above lacks.
    assert "api_key_handler" in out
    assert "SecretManager" in out
    # Only the integer 42 could be matched? No — too short. Expect zero events.
    assert ev == [], f"unexpected redactions: {ev}"


def test_extra_pattern_custom():
    """User-supplied extra_patterns must work.

    Note: we use a string WITHOUT a generic-assignment form (no 'token=' / 'secret:'
    prefix) so the default patterns don't shadow the custom one. The custom pattern
    matches MYCUSTOM_<20 digits> anywhere via word boundaries.
    """
    r = Redactor(extra_patterns=[(r"\bMYCUSTOM_\d{20}\b", "custom")])
    out, ev = r.redact("value MYCUSTOM_12345678901234567890 end")
    assert "MYCUSTOM_12345678901234567890" not in out
    assert any(e.kind == "custom" for e in ev), f"got kinds={[e.kind for e in ev]}"


def test_summary_zero():
    r = Redactor()
    s = r.summary([])
    assert "clean" in s.lower()


def test_summary_with_redactions():
    # AWS pattern requires EXACTLY 16 uppercase alphanumerics after 'AKIA' (20 total).
    # AKIAIOSFODNN7EXAMPLE         → 20 chars ✓
    # AKIAEXAMPLE123456780         → 20 chars ✓
    r = Redactor()
    _, ev = r.redact("key AKIAIOSFODNN7EXAMPLE plus AKIAEXAMPLE123456780 done")
    s = r.summary(ev)
    assert "aws-access-key-id" in s, f"summary was: {s!r}"
    assert "2" in s, f"expected 2 redactions, summary={s!r}"

"""Unit tests for T0.3 secret scrubbing (redact.py).

Covers every declared kind, false-positive avoidance for ordinary prose,
and that a redacted value never survives in the scrubbed text.
"""

from __future__ import annotations

from amplifier_module_tool_memory.redact import redact


class TestEachKindIsRedacted:
    def test_anthropic_key(self) -> None:
        text = "key is sk-ant-" + "a" * 30 + " end"
        scrubbed, counts = redact(text)
        assert "sk-ant-" not in scrubbed
        assert counts == {"anthropic_key": 1}
        assert "[REDACTED:anthropic_key]" in scrubbed

    def test_openai_key(self) -> None:
        text = "OPENAI_API_KEY value sk-" + "b" * 30 + " done"
        scrubbed, counts = redact(text)
        assert "sk-" + "b" * 30 not in scrubbed
        assert counts.get("openai_key") == 1

    def test_openai_key_proj_variant(self) -> None:
        text = "token sk-proj-" + "c" * 30
        scrubbed, counts = redact(text)
        assert counts.get("openai_key") == 1
        assert "sk-proj-" not in scrubbed

    def test_anthropic_not_double_counted_as_openai(self) -> None:
        text = "sk-ant-" + "a" * 30
        _scrubbed, counts = redact(text)
        assert counts == {"anthropic_key": 1}
        assert "openai_key" not in counts

    def test_github_token_ghp(self) -> None:
        text = "auth ghp_" + "d" * 36 + " ok"
        scrubbed, counts = redact(text)
        assert counts == {"github_token": 1}
        assert "ghp_" not in scrubbed

    def test_github_token_pat(self) -> None:
        text = "github_pat_" + "e" * 22
        scrubbed, counts = redact(text)
        assert counts == {"github_token": 1}
        assert "github_pat_" not in scrubbed

    def test_aws_access_key(self) -> None:
        text = "AKIA" + "1234567890ABCDEF"
        scrubbed, counts = redact(text)
        assert counts == {"aws_access_key": 1}
        assert "AKIA" not in scrubbed

    def test_aws_access_key_asia(self) -> None:
        text = "temp creds ASIA1234567890ABCDEF"
        scrubbed, counts = redact(text)
        assert counts == {"aws_access_key": 1}
        assert "ASIA" not in scrubbed

    def test_slack_token(self) -> None:
        text = "xoxb-1234567890-abcdefghij"
        scrubbed, counts = redact(text)
        assert counts == {"slack_token": 1}
        assert "xoxb-" not in scrubbed

    def test_google_api_key(self) -> None:
        text = "AIza" + "A" * 35
        scrubbed, counts = redact(text)
        assert counts == {"google_api_key": 1}
        assert "AIza" not in scrubbed

    def test_jwt(self) -> None:
        text = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMe"
        )
        scrubbed, counts = redact(text)
        assert counts == {"jwt": 1}
        assert "eyJ" not in scrubbed

    def test_private_key_block(self) -> None:
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBOgIBAAJBAK...\nmore lines here\n"
            "-----END RSA PRIVATE KEY-----"
        )
        scrubbed, counts = redact(text)
        assert counts == {"private_key": 1}
        assert "MIIBOgIBAAJBAK" not in scrubbed
        assert "BEGIN" not in scrubbed

    def test_bearer_token(self) -> None:
        text = "Authorization: Bearer " + "z" * 40
        scrubbed, counts = redact(text)
        assert counts == {"bearer": 1}
        assert "z" * 40 not in scrubbed

    def test_env_secret_equals(self) -> None:
        text = "API_KEY=abcdef1234567890"
        scrubbed, counts = redact(text)
        assert counts == {"env_secret": 1}
        assert "abcdef1234567890" not in scrubbed
        assert "API_KEY" in scrubbed  # name preserved

    def test_env_secret_colon(self) -> None:
        text = "DB_PASSWORD: superlongsecretvalue123"
        scrubbed, counts = redact(text)
        assert counts == {"env_secret": 1}
        assert "superlongsecretvalue123" not in scrubbed
        assert "DB_PASSWORD" in scrubbed

    def test_env_secret_quoted_json(self) -> None:
        text = '"MY_TOKEN": "reallylongsecretabc123"'
        scrubbed, counts = redact(text)
        assert counts == {"env_secret": 1}
        assert "reallylongsecretabc123" not in scrubbed
        assert '"MY_TOKEN"' in scrubbed


class TestFalsePositivesNotAltered:
    def test_prose_mentioning_token_untouched(self) -> None:
        text = "Remember to rotate the token every 90 days for security."
        scrubbed, counts = redact(text)
        assert scrubbed == text
        assert counts == {}

    def test_prose_mentioning_password_untouched(self) -> None:
        text = "The password field must never be logged in plaintext."
        scrubbed, counts = redact(text)
        assert scrubbed == text
        assert counts == {}

    def test_short_env_like_value_untouched(self) -> None:
        text = "API_KEY=abc"  # value < 8 chars
        scrubbed, counts = redact(text)
        assert scrubbed == text
        assert counts == {}

    def test_placeholder_value_untouched(self) -> None:
        text = "API_KEY=[REDACTED:env_secret]"
        scrubbed, counts = redact(text)
        assert scrubbed == text
        assert counts == {}

    def test_env_var_style_placeholder_untouched(self) -> None:
        text = "SECRET=${SOME_SECRET_ENV_VAR}"
        scrubbed, counts = redact(text)
        assert scrubbed == text
        assert counts == {}

    def test_angle_bracket_placeholder_untouched(self) -> None:
        text = "TOKEN=<your-token-here>"
        scrubbed, counts = redact(text)
        assert scrubbed == text
        assert counts == {}

    def test_ordinary_code_with_word_key_untouched(self) -> None:
        text = "def get_key(name: str) -> str:\n    return cache[name]\n"
        scrubbed, counts = redact(text)
        assert scrubbed == text
        assert counts == {}

    def test_empty_string(self) -> None:
        scrubbed, counts = redact("")
        assert scrubbed == ""
        assert counts == {}


class TestSecretNeverSurvivesInScrubbedText:
    def test_multiple_kinds_in_one_blob_all_redacted(self) -> None:
        text = (
            "config dump:\n"
            "ANTHROPIC_API_KEY=sk-ant-" + "a" * 25 + "\n"
            "AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF\n"
            "Authorization: Bearer " + "q" * 25 + "\n"
        )
        scrubbed, counts = redact(text)
        assert "sk-ant-" not in scrubbed
        assert "AKIA1234567890ABCDEF" not in scrubbed
        assert "q" * 25 not in scrubbed
        assert counts.get("anthropic_key") == 1
        assert counts.get("bearer") == 1
        # AKIA... is caught either as env_secret (AWS_ACCESS_KEY_ID=...) or
        # aws_access_key depending on which pattern claims it first; either
        # way it must be gone from the output (already asserted above).
        assert sum(counts.values()) >= 3

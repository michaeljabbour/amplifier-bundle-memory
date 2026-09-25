"""Tests for amplifier_module_tool_memory.automation_gate (perf/incremental-fold, part B)."""

from __future__ import annotations

import pytest

from amplifier_module_tool_memory.automation_gate import (
    CAPTURE_ENV_VAR,
    automation_opt_out,
)


class TestEnvVarOptOut:
    @pytest.mark.parametrize("value", ["off", "OFF", " Off ", "0", "false", "FALSE", "no", "No"])
    def test_recognized_off_values_opt_out(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(CAPTURE_ENV_VAR, value)
        assert automation_opt_out() is True

    @pytest.mark.parametrize("value", ["on", "1", "true", "yes", ""])
    def test_other_values_do_not_opt_out(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(CAPTURE_ENV_VAR, value)
        assert automation_opt_out() is False

    def test_unset_env_var_does_not_opt_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CAPTURE_ENV_VAR, raising=False)
        assert automation_opt_out() is False


class TestExcludedWorkingDirs:
    def test_matching_glob_opts_out(self) -> None:
        assert (
            automation_opt_out(
                excluded_working_dirs=["/home/*/dev/afast-ev/*/experiments/*"],
                cwd="/home/user/dev/afast-ev/planner/experiments/run-1",
            )
            is True
        )

    def test_non_matching_glob_does_not_opt_out(self) -> None:
        assert (
            automation_opt_out(
                excluded_working_dirs=["/home/*/dev/afast-ev/*/experiments/*"],
                cwd="/home/user/dev/my-real-project",
            )
            is False
        )

    def test_empty_list_does_not_opt_out(self) -> None:
        assert automation_opt_out(excluded_working_dirs=[], cwd="/anywhere") is False

    def test_none_does_not_opt_out(self) -> None:
        assert automation_opt_out(excluded_working_dirs=None, cwd="/anywhere") is False

    def test_multiple_patterns_first_match_wins(self) -> None:
        assert (
            automation_opt_out(
                excluded_working_dirs=["/no/match/*", "/yes/match/*"],
                cwd="/yes/match/here",
            )
            is True
        )


class TestDefaultBehaviorUnchanged:
    def test_normal_interactive_session_never_opts_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CAPTURE_ENV_VAR, raising=False)
        assert automation_opt_out(excluded_working_dirs=[], cwd="/home/user/my-project") is False


class TestNeverRaises:
    def test_survives_a_broken_pattern(self) -> None:
        # fnmatch treats malformed patterns leniently (never raises), but
        # this pins the "never crash a hook" contract regardless.
        assert automation_opt_out(excluded_working_dirs=["[", "*"], cwd="/x") in (True, False)

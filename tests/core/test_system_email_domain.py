from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.autobots.core.settings import SettingsProvider


def _with_domain(monkeypatch: pytest.MonkeyPatch, domain: str | None) -> None:
    monkeypatch.setattr(
        SettingsProvider,
        "_settings",
        SimpleNamespace(SYSTEM_EMAIL_DOMAIN=domain),
    )


@pytest.mark.parametrize(
    "email,expected",
    [
        ("user@runkai.ai", True),
        ("USER@RUNKAI.AI", True),
        ("  user@runkai.ai \n", True),
        (None, False),
        ("", False),
        ("   ", False),
        ("noatsign", False),
        ("user@notrunkai.ai", False),
        ("user@sub.runkai.ai", False),
        ("user@runkai.ai.evil.com", False),
    ],
)
def test_is_system_email_default_domain(
    monkeypatch: pytest.MonkeyPatch, email: str | None, expected: bool
) -> None:
    _with_domain(monkeypatch, "runkai.ai")
    assert SettingsProvider.is_system_email(email) is expected


def test_is_system_email_configured_domain_with_leading_at(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_domain(monkeypatch, "@runkai.ai")
    assert SettingsProvider.get_system_email_domain() == "runkai.ai"
    assert SettingsProvider.is_system_email("user@runkai.ai") is True


def test_is_system_email_case_and_whitespace_in_configured_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_domain(monkeypatch, "  RunKai.AI  ")
    assert SettingsProvider.get_system_email_domain() == "runkai.ai"
    assert SettingsProvider.is_system_email("user@runkai.ai") is True


@pytest.mark.parametrize("domain", [None, ""])
def test_is_system_email_fails_closed_with_no_configured_domain(
    monkeypatch: pytest.MonkeyPatch, domain: str | None
) -> None:
    _with_domain(monkeypatch, domain)
    assert SettingsProvider.is_system_email("user@runkai.ai") is False

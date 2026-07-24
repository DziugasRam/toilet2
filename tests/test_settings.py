import pytest

from toilet2.settings import Settings


def cms_settings(**changes):
    values = {
        "database_url": "sqlite:///test.db",
        "public_origin": "http://testserver",
        "cms_contests": ("contest",),
        "control_auth_key": "test-key",
    }
    values.update(changes)
    return Settings(**values)


def test_local_cms_test_origin_is_explicitly_allowed():
    assert cms_settings().validate().public_origin == "http://testserver"


def test_defaults_fail_closed_without_database_origin_contest_or_control_key():
    with pytest.raises(ValueError, match="TOILET_DATABASE_URL"):
        Settings().validate()


def test_retired_dev_auth_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("TOILET_AUTH_MODE", "dev")
    with pytest.raises(ValueError, match="no longer supports"):
        Settings.from_env()


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"public_origin": "http://contest.example.org"}, "HTTPS public origin"),
        ({"public_origin": "http://testserver/"}, r"http\(s\) origin"),
        (
            {"public_origin": "https://contest.example.org", "cookie_secure": False},
            "COOKIE_SECURE",
        ),
        ({"public_origin": "https://contest.example.org/toilet", "cookie_secure": True}, "origin"),
        (
            {
                "public_origin": "https://contest.example.org",
                "cookie_secure": True,
                "control_base_url": "http://control.example.org",
            },
            "Olimp-control",
        ),
        ({"cms_base_url": "http://cms.example.org"}, "non-local CMS"),
        ({"cms_base_url": "https://cms.example.org/path"}, "CMS_BASE_URL"),
        ({"cms_contests": ("one", "two")}, "exactly one"),
        ({"student_rate_limit_window_seconds": 0}, "STUDENT_RATE_LIMIT_WINDOW"),
        ({"operator_login_rate_limit_count": 0}, "OPERATOR_LOGIN_RATE_LIMIT_COUNT"),
        ({"operator_login_rate_limit_window_seconds": 0}, "OPERATOR_LOGIN_RATE_LIMIT_WINDOW"),
        ({"cms_socket_max_age_seconds": 0}, "SOCKET_MAX_AGE"),
        ({"cms_timeout_seconds": 0}, "CMS_TIMEOUT"),
        ({"control_timeout_seconds": 0}, "CONTROL_TIMEOUT"),
    ],
)
def test_unsafe_or_nonpositive_production_settings_fail_at_startup(changes, match):
    with pytest.raises(ValueError, match=match):
        cms_settings(**changes).validate()


def test_https_production_origins_require_and_accept_secure_cookie():
    settings = cms_settings(
        public_origin="https://contest.example.org",
        cookie_secure=True,
        control_base_url="https://control.example.org",
        control_auth_key="t" * 32,
    )
    assert settings.validate() is settings


def test_nonlocal_origin_requires_a_strong_control_service_key():
    with pytest.raises(ValueError, match="at least 32"):
        cms_settings(
            public_origin="https://contest.example.org",
            cookie_secure=True,
            control_base_url="https://control.example.org",
            control_auth_key="short-key",
        ).validate()


def test_general_request_types_are_disabled_until_explicitly_enabled(monkeypatch):
    assert cms_settings().validate().general_request_types == ()
    assert cms_settings(
        general_request_types=("paper",)
    ).validate().general_request_types == ("paper",)

    monkeypatch.setenv("TOILET_DATABASE_URL", "sqlite:///test.db")
    monkeypatch.setenv("TOILET_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.setenv("TOILET_CMS_CONTESTS", "contest")
    monkeypatch.setenv("TOILET_CONTROL_AUTH_KEY", "test-key")
    monkeypatch.delenv("TOILET_GENERAL_REQUEST_TYPES", raising=False)
    assert Settings.from_env().general_request_types == ()
    for disabled in ("", " ", ","):
        monkeypatch.setenv("TOILET_GENERAL_REQUEST_TYPES", disabled)
        assert Settings.from_env().general_request_types == ()
    monkeypatch.setenv("TOILET_GENERAL_REQUEST_TYPES", "paper")
    assert Settings.from_env().general_request_types == ("paper",)


def test_contestant_ui_is_disabled_until_explicitly_enabled(monkeypatch):
    assert cms_settings().validate().student_ui_enabled is False

    monkeypatch.setenv("TOILET_DATABASE_URL", "sqlite:///test.db")
    monkeypatch.setenv("TOILET_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.setenv("TOILET_CMS_CONTESTS", "contest")
    monkeypatch.setenv("TOILET_CONTROL_AUTH_KEY", "test-key")
    monkeypatch.delenv("TOILET_STUDENT_UI_ENABLED", raising=False)
    assert Settings.from_env().student_ui_enabled is False
    for disabled in ("", " ", "false", "no", "0"):
        monkeypatch.setenv("TOILET_STUDENT_UI_ENABLED", disabled)
        assert Settings.from_env().student_ui_enabled is False
    for enabled in ("true", "yes", "1", "on"):
        monkeypatch.setenv("TOILET_STUDENT_UI_ENABLED", enabled)
        assert Settings.from_env().student_ui_enabled is True


def test_unknown_or_duplicate_general_request_types_are_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        cms_settings(general_request_types=("water",)).validate()
    with pytest.raises(ValueError, match="duplicates"):
        cms_settings(general_request_types=("paper", "paper")).validate()

"""Fail-closed environment-backed configuration for the toilet application."""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit


SUPPORTED_GENERAL_REQUEST_TYPES = ("paper",)
INSECURE_CONTROL_AUTH_KEYS = {
    "dev-toilet-control-key",
    "toilet-dev-key",
    "replace-with-dedicated-random-key",
    "a_different_very_secret_toilet_service_key",
}


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _root_path(value: str | None) -> str:
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = ""
    admin_username: str = ""
    admin_password: str = ""
    app_root_path: str = ""
    public_origin: str = ""
    cookie_secure: bool = False
    session_ttl_seconds: int = 8 * 60 * 60

    cms_base_url: str = "http://127.0.0.1:8888"
    cms_contests: tuple[str, ...] = ()
    cms_multi_contest: bool = False
    cms_timeout_seconds: float = 5.0
    cms_positive_cache_seconds: float = 30.0
    cms_negative_cache_seconds: float = 3.0
    cms_cache_max_entries: int = 4096
    cms_probe_rate_limit_count: int = 60
    cms_probe_rate_limit_window_seconds: float = 60.0
    cms_socket_max_age_seconds: int = 5 * 60

    control_base_url: str = "http://127.0.0.1:8001"
    control_auth_key: str = ""
    control_timeout_seconds: float = 5.0

    # Optional contestant support requests are disabled unless a deployment
    # explicitly opts in. Toilet requests are the only always-available flow.
    general_request_types: tuple[str, ...] = ()

    # The contestant-facing pages are disabled until CMS student login and every
    # contestant translation have been verified for a contest. Staff request
    # toilet breaks on a contestant's behalf from the class layout instead. The
    # contestant REST/WebSocket handlers stay in place so re-enabling the surface
    # is a single configuration change.
    student_ui_enabled: bool = False

    student_rate_limit_count: int = 10
    student_rate_limit_window_seconds: int = 60
    operator_login_rate_limit_count: int = 10
    operator_login_rate_limit_window_seconds: int = 60

    @property
    def cookie_path(self) -> str:
        return self.app_root_path or "/"

    def validate(self) -> "Settings":
        if not self.database_url:
            raise ValueError("TOILET_DATABASE_URL is required")
        if bool(self.admin_username) != bool(self.admin_password):
            raise ValueError(
                "TOILET_ADMIN_USERNAME and TOILET_ADMIN_PASSWORD must be set together"
            )
        if not self.cms_contests:
            raise ValueError("TOILET_CMS_CONTESTS is required")
        if not self.cms_multi_contest and len(self.cms_contests) != 1:
            raise ValueError(
                "single-contest CMS mode requires exactly one TOILET_CMS_CONTESTS entry"
            )
        if not self.public_origin:
            raise ValueError("TOILET_PUBLIC_ORIGIN is required")
        parsed_origin = urlsplit(self.public_origin)
        local_hosts = {"127.0.0.1", "::1", "localhost", "testserver"}
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ValueError("TOILET_PUBLIC_ORIGIN must be an http(s) origin")
        if parsed_origin.scheme != "https" and parsed_origin.hostname not in local_hosts:
            raise ValueError("CMS mode requires an HTTPS public origin outside localhost")
        if parsed_origin.scheme == "https" and not self.cookie_secure:
            raise ValueError("CMS mode with HTTPS requires TOILET_COOKIE_SECURE=true")
        parsed_cms = urlsplit(self.cms_base_url)
        if (
            parsed_cms.scheme not in {"http", "https"}
            or not parsed_cms.hostname
            or parsed_cms.username is not None
            or parsed_cms.password is not None
            or parsed_cms.path not in {"", "/"}
            or parsed_cms.query
            or parsed_cms.fragment
        ):
            raise ValueError("TOILET_CMS_BASE_URL must be an http(s) origin")
        if parsed_cms.scheme != "https" and parsed_cms.hostname not in local_hosts:
            raise ValueError("CMS mode requires HTTPS for a non-local CMS service")
        parsed_control = urlsplit(self.control_base_url)
        if (
            parsed_control.scheme not in {"http", "https"}
            or not parsed_control.hostname
            or parsed_control.username is not None
            or parsed_control.password is not None
            or parsed_control.path not in {"", "/"}
            or parsed_control.query
            or parsed_control.fragment
        ):
            raise ValueError("TOILET_CONTROL_BASE_URL must be an http(s) origin")
        if parsed_control.scheme != "https" and parsed_control.hostname not in local_hosts:
            raise ValueError(
                "CMS mode requires HTTPS for a non-local Olimp-control service"
            )
        if not self.control_auth_key:
            raise ValueError("TOILET_CONTROL_AUTH_KEY must not be empty")
        if self.control_auth_key in INSECURE_CONTROL_AUTH_KEYS:
            raise ValueError(
                "TOILET_CONTROL_AUTH_KEY must not use a sample or development key"
            )
        if (
            parsed_origin.hostname not in local_hosts
            and len(self.control_auth_key.encode("utf-8")) < 32
        ):
            raise ValueError(
                "TOILET_CONTROL_AUTH_KEY must contain at least 32 UTF-8 bytes"
            )
        if self.session_ttl_seconds <= 0:
            raise ValueError("TOILET_SESSION_TTL_SECONDS must be positive")
        if self.student_rate_limit_count <= 0:
            raise ValueError("TOILET_STUDENT_RATE_LIMIT_COUNT must be positive")
        if self.student_rate_limit_window_seconds <= 0:
            raise ValueError("TOILET_STUDENT_RATE_LIMIT_WINDOW_SECONDS must be positive")
        if self.operator_login_rate_limit_count <= 0:
            raise ValueError("TOILET_OPERATOR_LOGIN_RATE_LIMIT_COUNT must be positive")
        if self.operator_login_rate_limit_window_seconds <= 0:
            raise ValueError("TOILET_OPERATOR_LOGIN_RATE_LIMIT_WINDOW_SECONDS must be positive")
        if self.cms_cache_max_entries <= 0:
            raise ValueError("TOILET_CMS_CACHE_MAX_ENTRIES must be positive")
        if self.cms_probe_rate_limit_count <= 0:
            raise ValueError("TOILET_CMS_PROBE_RATE_LIMIT_COUNT must be positive")
        if self.cms_probe_rate_limit_window_seconds <= 0:
            raise ValueError("TOILET_CMS_PROBE_RATE_LIMIT_WINDOW_SECONDS must be positive")
        if self.cms_socket_max_age_seconds <= 0:
            raise ValueError("TOILET_CMS_SOCKET_MAX_AGE_SECONDS must be positive")
        if self.cms_timeout_seconds <= 0:
            raise ValueError("TOILET_CMS_TIMEOUT_SECONDS must be positive")
        if self.cms_positive_cache_seconds < 0 or self.cms_negative_cache_seconds < 0:
            raise ValueError("CMS cache lifetimes must not be negative")
        if self.control_timeout_seconds <= 0:
            raise ValueError("TOILET_CONTROL_TIMEOUT_SECONDS must be positive")
        if len(self.general_request_types) != len(set(self.general_request_types)):
            raise ValueError("TOILET_GENERAL_REQUEST_TYPES must not contain duplicates")
        unknown_request_types = set(self.general_request_types) - set(
            SUPPORTED_GENERAL_REQUEST_TYPES
        )
        if unknown_request_types:
            raise ValueError(
                "TOILET_GENERAL_REQUEST_TYPES contains unsupported values: "
                + ", ".join(sorted(unknown_request_types))
            )
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        legacy_auth_mode = os.environ.get("TOILET_AUTH_MODE", "").strip().lower()
        if legacy_auth_mode and legacy_auth_mode != "cms":
            raise ValueError("TOILET_AUTH_MODE no longer supports development authentication")
        defaults = cls()
        settings = cls(
            database_url=os.environ.get("TOILET_DATABASE_URL", defaults.database_url),
            admin_username=os.environ.get(
                "TOILET_ADMIN_USERNAME", defaults.admin_username
            ).strip(),
            admin_password=os.environ.get(
                "TOILET_ADMIN_PASSWORD", defaults.admin_password
            ),
            app_root_path=_root_path(os.environ.get("TOILET_ROOT_PATH")),
            public_origin=os.environ.get("TOILET_PUBLIC_ORIGIN", "").rstrip("/"),
            cookie_secure=_bool(os.environ.get("TOILET_COOKIE_SECURE")),
            session_ttl_seconds=int(
                os.environ.get("TOILET_SESSION_TTL_SECONDS", defaults.session_ttl_seconds)
            ),
            cms_base_url=os.environ.get("TOILET_CMS_BASE_URL", defaults.cms_base_url).rstrip("/"),
            cms_contests=_csv(os.environ.get("TOILET_CMS_CONTESTS")),
            cms_multi_contest=_bool(os.environ.get("TOILET_CMS_MULTI_CONTEST")),
            cms_timeout_seconds=float(
                os.environ.get("TOILET_CMS_TIMEOUT_SECONDS", defaults.cms_timeout_seconds)
            ),
            cms_positive_cache_seconds=float(
                os.environ.get(
                    "TOILET_CMS_POSITIVE_CACHE_SECONDS",
                    defaults.cms_positive_cache_seconds,
                )
            ),
            cms_negative_cache_seconds=float(
                os.environ.get(
                    "TOILET_CMS_NEGATIVE_CACHE_SECONDS",
                    defaults.cms_negative_cache_seconds,
                )
            ),
            cms_cache_max_entries=int(
                os.environ.get(
                    "TOILET_CMS_CACHE_MAX_ENTRIES", defaults.cms_cache_max_entries
                )
            ),
            cms_probe_rate_limit_count=int(
                os.environ.get(
                    "TOILET_CMS_PROBE_RATE_LIMIT_COUNT",
                    defaults.cms_probe_rate_limit_count,
                )
            ),
            cms_probe_rate_limit_window_seconds=float(
                os.environ.get(
                    "TOILET_CMS_PROBE_RATE_LIMIT_WINDOW_SECONDS",
                    defaults.cms_probe_rate_limit_window_seconds,
                )
            ),
            cms_socket_max_age_seconds=int(
                os.environ.get(
                    "TOILET_CMS_SOCKET_MAX_AGE_SECONDS",
                    defaults.cms_socket_max_age_seconds,
                )
            ),
            control_base_url=os.environ.get(
                "TOILET_CONTROL_BASE_URL", defaults.control_base_url
            ).rstrip("/"),
            control_auth_key=os.environ.get(
                "TOILET_CONTROL_AUTH_KEY", defaults.control_auth_key
            ),
            control_timeout_seconds=float(
                os.environ.get(
                    "TOILET_CONTROL_TIMEOUT_SECONDS", defaults.control_timeout_seconds
                )
            ),
            # Unset, empty, and whitespace-only values all mean "disabled".
            general_request_types=_csv(
                os.environ.get("TOILET_GENERAL_REQUEST_TYPES")
            ),
            student_ui_enabled=_bool(os.environ.get("TOILET_STUDENT_UI_ENABLED")),
            student_rate_limit_count=int(
                os.environ.get(
                    "TOILET_STUDENT_RATE_LIMIT_COUNT", defaults.student_rate_limit_count
                )
            ),
            student_rate_limit_window_seconds=int(
                os.environ.get(
                    "TOILET_STUDENT_RATE_LIMIT_WINDOW_SECONDS",
                    defaults.student_rate_limit_window_seconds,
                )
            ),
            operator_login_rate_limit_count=int(
                os.environ.get(
                    "TOILET_OPERATOR_LOGIN_RATE_LIMIT_COUNT",
                    defaults.operator_login_rate_limit_count,
                )
            ),
            operator_login_rate_limit_window_seconds=int(
                os.environ.get(
                    "TOILET_OPERATOR_LOGIN_RATE_LIMIT_WINDOW_SECONDS",
                    defaults.operator_login_rate_limit_window_seconds,
                )
            ),
        )
        return settings.validate()

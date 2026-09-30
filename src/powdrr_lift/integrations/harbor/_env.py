"""Environment parsing helpers that do not depend on Harbor or Pier."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from urllib.parse import urlsplit

PROVIDER_ENV_KEYS = (
    "DEEPINFRA_API_KEY",
    "DEEPINFRA_API_TOKEN",
    "DEEPINFRA_BASE_URL",
    "TYPESAFEAI_API_KEY",
    "TYPESAFE_API_KEY",
    "SYSTEM_ONE_API_KEY",
    "TAVILY_API_KEY",
    "SYSTEM_ONE_BASE_URL",
)


def provider_environment(get_env: Callable[[str], str | None]) -> dict[str, str]:
    """Forward configured provider credentials to the in-container Powdrr run."""
    return {
        key: value for key in PROVIDER_ENV_KEYS if (value := get_env(key)) is not None
    }


def jev_allowed_domains(base_url: str | None = None) -> list[str]:
    """Return the default JEV host and any configured custom endpoint host."""
    domains = ["api.typesafe.ai"]
    if base_url:
        hostname = urlsplit(base_url).hostname
        if hostname and hostname not in domains:
            domains.append(hostname)
    return domains


def pip_install_command(package: str, *options: str) -> str:
    """Install into an active venv, or use the user site for system Python."""
    quoted_package = shlex.quote(package)
    option_text = " ".join(options)
    install_args = f"{option_text} {quoted_package}" if option_text else quoted_package
    pip = "python3 -m pip install"
    system_python = (
        "python3 -c 'import sys; raise SystemExit(sys.prefix != sys.base_prefix)'"
    )
    return (
        f"if {system_python}; then {pip} --user {install_args}; "
        f"else {pip} {install_args}; fi"
    )


def env_flag_is_enabled(value: str | None) -> bool:
    """Return whether an environment value uses a supported true spelling."""
    return (value or "").casefold() in {"1", "true", "yes"}

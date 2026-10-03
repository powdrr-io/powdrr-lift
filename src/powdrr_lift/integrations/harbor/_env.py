"""Environment parsing helpers that do not depend on Harbor or Pier."""

from __future__ import annotations

import shlex
from urllib.parse import urlsplit

PROVIDER_ENVIRONMENT_KEYS = (
    "DEEPINFRA_API_KEY",
    "DEEPINFRA_API_TOKEN",
    "DEEPINFRA_BASE_URL",
    "SYSTEM_ONE_API_KEY",
    "SYSTEM_ONE_BASE_URL",
    "TYPESAFEAI_API_KEY",
    "TYPESAFE_API_KEY",
    "TAVILY_API_KEY",
)


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


def network_allowlist_domains(
    system_one_base_url: str | None = None,
) -> tuple[str, ...]:
    """Return hosts required by package installation and configured providers."""
    domains = {
        "api.deepinfra.com",
        "api.typesafe.ai",
        "files.pythonhosted.org",
        "github.com",
        "pypi.org",
        "registry.npmjs.org",
    }
    if system_one_base_url:
        host = urlsplit(system_one_base_url).hostname
        if host:
            domains.add(host.casefold())
    return tuple(sorted(domains))

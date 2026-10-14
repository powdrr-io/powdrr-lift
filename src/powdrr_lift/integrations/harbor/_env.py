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
    "SUBQ_API_KEY",
    "SUBQ_BASE_URL",
    "SUBQ_MODEL",
    "TYPESAFEAI_API_KEY",
    "TYPESAFE_API_KEY",
    "TAVILY_API_KEY",
)

CODE_AGENT_ENVIRONMENT_KEYS = (
    *PROVIDER_ENVIRONMENT_KEYS,
    "LITELLM_LOCAL_MODEL_COST_MAP",
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
    subq_base_url: str | None = None,
) -> tuple[str, ...]:
    """Return hosts required by package installation and configured providers."""
    domains = {
        "api.deepinfra.com",
        "api.typesafe.ai",
        "api.subq.ai",
        "files.pythonhosted.org",
        "github.com",
        "pypi.org",
        "registry.npmjs.org",
    }
    for base_url in (system_one_base_url, subq_base_url):
        if not base_url:
            continue
        host = urlsplit(base_url).hostname
        if host:
            domains.add(host.casefold())
    return tuple(sorted(domains))

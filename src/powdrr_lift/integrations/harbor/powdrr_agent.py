"""Harbor adapter for the Powdrr implement-feature agent."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import Any

try:
    from pier.agents.installed.base import (  # type: ignore[import-not-found]
        BaseInstalledAgent,
    )
    from pier.environments.base import BaseEnvironment  # type: ignore[import-not-found]
    from pier.models.agent.context import AgentContext  # type: ignore[import-not-found]
except ImportError:
    from harbor.agents.installed.base import (  # type: ignore[import-not-found]
        BaseInstalledAgent,
    )
    from harbor.environments.base import (  # type: ignore[import-not-found]
        BaseEnvironment,
    )
    from harbor.models.agent.context import (  # type: ignore[import-not-found]
        AgentContext,
    )

POWDRR_VERSION = "0.1.0"
OPENCODE_VERSION = "1.18.31"


class PowdrrAgent(BaseInstalledAgent):
    """Run Powdrr's Procedrr implement-feature flow in a Harbor checkout."""

    @staticmethod
    def name() -> str:
        return "powdrr"

    def version(self) -> str | None:
        return self._get_env("POWDRR_VERSION") or POWDRR_VERSION

    def install_spec(self) -> Any:
        """Provide Pier's build contract while retaining Harbor installation."""
        try:
            from pier.models.agent.install import (  # type: ignore[import-not-found]
                AgentInstallSpec,
                InstallStep,
            )
        except ImportError:
            return None

        package = self._get_env("POWDRR_INSTALL_SPEC") or (
            f"powdrr-lift=={POWDRR_VERSION}"
        )
        return AgentInstallSpec(
            agent_name=self.name(),
            version=self.version(),
            steps=[
                InstallStep(
                    run=(
                        "python3 -m pip install --user --upgrade --force-reinstall "
                        "--no-cache-dir --disable-pip-version-check "
                        f"{shlex.quote(package)}"
                    )
                ),
                InstallStep(
                    run=f"npm install --global opencode-ai@{OPENCODE_VERSION}",
                    user="root",
                ),
            ],
            verification_command="powdrr-lift --help >/dev/null && opencode --version",
        )

    def network_allowlist(self) -> Any:
        """Allow package installation and the configured DeepInfra provider."""
        try:
            from pier.models.agent.network import (  # type: ignore[import-not-found]
                NetworkAllowlist,
            )
        except ImportError:
            return None

        return NetworkAllowlist(
            domains=[
                "api.deepinfra.com",
                "files.pythonhosted.org",
                "github.com",
                "registry.npmjs.org",
                "pypi.org",
            ]
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Powdrr writes its own telemetry bundle; no extra context is needed."""
        del context

    @property
    def remote_session_logs_dir(self) -> PurePosixPath:
        return PurePosixPath(".powdrr/feature-runs")

    async def install(self, environment: BaseEnvironment) -> None:
        """Install pinned runtimes when the image does not preinstall them."""
        await self.ensure_system_dependencies(
            environment,
            ("git", "python3", "python_pip", "nodejs", "npm"),
        )

        package = self._get_env("POWDRR_INSTALL_SPEC") or (
            f"powdrr-lift=={POWDRR_VERSION}"
        )
        await self.exec_as_agent(
            environment,
            command=(
                "python3 -m pip install --user --upgrade --force-reinstall "
                "--no-cache-dir --disable-pip-version-check "
                f"{shlex.quote(package)}"
            ),
        )

        # OpenCode documents opencode-ai as its npm installation package. Keep
        # this conditional so a prebuilt Harbor image does not reinstall it.
        await self.exec_as_root(
            environment,
            command=(
                "if ! command -v opencode >/dev/null 2>&1; then "
                f"npm install --global opencode-ai@{OPENCODE_VERSION}; "
                "fi"
            ),
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        cwd_result = await self.exec_as_agent(environment, command="pwd")
        repo_root = (cwd_result.stdout or "").strip()
        if not repo_root:
            raise RuntimeError("Harbor did not provide a repository working directory")

        task_name = (
            self._get_env("HARBOR_TASK_ID")
            or self._get_env("HARBOR_TASK_NAME")
            or "harbor-task"
        )
        command: list[str] = [
            "powdrr-lift",
            "harbor-feature",
            "--feature-description",
            instruction,
            "--work-item-name",
            task_name,
            "--repo-root",
            repo_root,
            "--planning-provider",
            "deepinfra",
            "--opencode-model",
            "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
        ]
        for path in _split_paths(self._get_env("POWDRR_ALLOWED_PATHS") or "."):
            command.extend(("--allowed-path", path))

        planning_model = self._get_env("POWDRR_PLANNING_MODEL")
        if planning_model:
            command.extend(("--planning-model", planning_model))
        output_root = self._get_env("POWDRR_OUTPUT_ROOT")
        if output_root:
            command.extend(("--output-root", output_root))

        await self.exec_as_agent(
            environment,
            command=shlex.join(command),
            cwd=repo_root,
        )


def _split_paths(value: str) -> tuple[str, ...]:
    paths = tuple(item.strip() for item in value.split(",") if item.strip())
    return paths or (".",)


__all__ = ["PowdrrAgent"]

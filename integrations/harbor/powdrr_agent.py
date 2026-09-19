"""Harbor adapter for the Powdrr implement-feature agent.

This module is imported by Harbor, which supplies the ``harbor`` dependency.
Harbor is intentionally not a Powdrr runtime dependency: normal Powdrr users
can use the feature endpoint without installing the benchmark harness.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class PowdrrAgent(BaseInstalledAgent):
    """Run Powdrr's Procedrr implement-feature flow in a Harbor checkout."""

    @staticmethod
    def name() -> str:
        return "powdrr"

    def version(self) -> str | None:
        return self._get_env("POWDRR_VERSION") or "0.1.0"

    @property
    def remote_session_logs_dir(self) -> PurePosixPath:
        return PurePosixPath(".powdrr/feature-runs")

    async def install(self, environment: BaseEnvironment) -> None:
        """Install the two runtimes Powdrr uses inside the task container."""
        await self.ensure_system_dependencies(
            environment,
            ("git", "python3", "python_pip", "nodejs", "npm"),
        )

        package = self._get_env("POWDRR_INSTALL_SPEC") or "powdrr-lift"
        await self.exec_as_agent(
            environment,
            command=(
                "python3 -m pip install --user --disable-pip-version-check "
                f"{shlex.quote(package)}"
            ),
        )

        # OpenCode documents opencode-ai as its npm installation package. Keep
        # this conditional so a prebuilt Harbor image does not reinstall it.
        await self.exec_as_root(
            environment,
            command=(
                "if ! command -v opencode >/dev/null 2>&1; then "
                "npm install --global opencode-ai; "
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
        ]
        for path in _split_paths(self._get_env("POWDRR_ALLOWED_PATHS") or "."):
            command.extend(("--allowed-path", path))

        for option, variable in (
            ("--validation-command", "POWDRR_VALIDATION_COMMAND"),
            ("--opencode-model", "OPENCODE_MODEL"),
            ("--planning-provider", "POWDRR_PLANNING_PROVIDER"),
            ("--planning-model", "POWDRR_PLANNING_MODEL"),
        ):
            value = self._get_env(variable)
            if value:
                command.extend((option, value))

        await self.exec_as_agent(
            environment,
            command=shlex.join(command),
            cwd=repo_root,
        )


def _split_paths(value: str) -> tuple[str, ...]:
    paths = tuple(item.strip() for item in value.split(",") if item.strip())
    return paths or (".",)


__all__ = ["PowdrrAgent"]

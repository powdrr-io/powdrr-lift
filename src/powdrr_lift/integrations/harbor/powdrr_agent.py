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
MINISWEAGENT_VERSION = "2.4.6"
MINISWEAGENT_MODEL = "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731"


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
        code_agent = self._code_agent()
        if code_agent == "minisweagent":
            agent_install = InstallStep(
                run=(
                    "python3 -m pip install --user --upgrade --no-cache-dir "
                    "--disable-pip-version-check "
                    f"mini-swe-agent=={MINISWEAGENT_VERSION}"
                )
            )
            verification = "powdrr-lift --help >/dev/null && mini --help >/dev/null"
        else:
            agent_install = InstallStep(
                run=f"npm install --global opencode-ai@{OPENCODE_VERSION}",
                user="root",
            )
            verification = "powdrr-lift --help >/dev/null && opencode --version"
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
                agent_install,
            ],
            verification_command=verification,
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

        if self._code_agent() == "minisweagent":
            await self.exec_as_agent(
                environment,
                command=(
                    "if ! command -v mini >/dev/null 2>&1; then "
                    "python3 -m pip install --user --upgrade --no-cache-dir "
                    "--disable-pip-version-check "
                    f"mini-swe-agent=={MINISWEAGENT_VERSION}; "
                    "fi"
                ),
            )
        else:
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
        code_agent = self._code_agent()
        command: list[str] = [
            "powdrr-lift",
            "harbor-feature",
            "--feature-description",
            instruction,
            "--work-item-name",
            task_name,
            "--task-id",
            task_name,
            "--repo-root",
            repo_root,
            "--planning-provider",
            "deepinfra",
            "--code-agent",
            code_agent,
        ]
        if code_agent == "minisweagent":
            command.extend(("--minisweagent-model", MINISWEAGENT_MODEL))
        else:
            command.extend(
                (
                    "--opencode-model",
                    "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
                )
            )
        minisweagent_executable = self._get_env("POWDRR_MINISWEAGENT_EXECUTABLE")
        if minisweagent_executable:
            command.extend(("--minisweagent-executable", minisweagent_executable))
        minisweagent_model = self._get_env("POWDRR_MINISWEAGENT_MODEL")
        if minisweagent_model:
            command.extend(("--minisweagent-model", minisweagent_model))
        prompt_prefix = self._get_env("POWDRR_CODE_AGENT_PROMPT_PREFIX")
        if prompt_prefix:
            command.extend(("--code-agent-prompt-prefix", prompt_prefix))
        prompt_suffix = self._get_env("POWDRR_CODE_AGENT_PROMPT_SUFFIX")
        if prompt_suffix:
            command.extend(("--code-agent-prompt-suffix", prompt_suffix))
        for path in _split_paths(self._get_env("POWDRR_ALLOWED_PATHS") or "."):
            command.extend(("--allowed-path", path))

        planning_model = self._get_env("POWDRR_PLANNING_MODEL")
        if planning_model:
            command.extend(("--planning-model", planning_model))
        validation_command = self._get_env("POWDRR_VALIDATION_COMMAND")
        if validation_command:
            command.extend(("--validation-command", validation_command))
        output_root = self._get_env("POWDRR_OUTPUT_ROOT")
        if output_root:
            command.extend(("--output-root", output_root))
        if self._get_env("POWDRR_DESIGN_ONLY", "").casefold() in {"1", "true", "yes"}:
            command.append("--design-only")

        provider_env = {
            key: value
            for key in (
                "DEEPINFRA_API_KEY",
                "DEEPINFRA_API_TOKEN",
                "DEEPINFRA_BASE_URL",
            )
            if (value := self._get_env(key)) is not None
        }
        for key in ("POWDRR_INSTALL_SPEC", "POWDRR_VERSION", "POWDRR_REVISION"):
            if (value := self._get_env(key)) is not None:
                provider_env[key] = value
        provider_env.update(
            {
                "GIT_AUTHOR_NAME": self._get_env("GIT_AUTHOR_NAME") or "Powdrr Agent",
                "GIT_AUTHOR_EMAIL": self._get_env("GIT_AUTHOR_EMAIL")
                or "powdrr@localhost",
                "GIT_COMMITTER_NAME": self._get_env("GIT_COMMITTER_NAME")
                or "Powdrr Agent",
                "GIT_COMMITTER_EMAIL": self._get_env("GIT_COMMITTER_EMAIL")
                or "powdrr@localhost",
            }
        )
        command_text = shlex.join(command)
        command_log = self._get_env("POWDRR_COMMAND_LOG")
        if command_log:
            quoted_log = shlex.quote(command_log)
            command_text = (
                f"{command_text} > {quoted_log} 2>&1; "
                f"status=$?; tail -300 {quoted_log}; exit $status"
            )
        await self.exec_as_agent(
            environment,
            command=command_text,
            env=provider_env,
            cwd=repo_root,
        )

    def _code_agent(self) -> str:
        code_agent = self._get_env("POWDRR_CODE_AGENT") or "minisweagent"
        if code_agent not in {"opencode", "minisweagent"}:
            raise ValueError("POWDRR_CODE_AGENT must be 'opencode' or 'minisweagent'")
        return code_agent


def _split_paths(value: str) -> tuple[str, ...]:
    paths = tuple(item.strip() for item in value.split(",") if item.strip())
    return paths or (".",)


__all__ = ["PowdrrAgent"]

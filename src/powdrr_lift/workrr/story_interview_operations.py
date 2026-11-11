"""Local persistence and user-channel transport for service-owned story design."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from powdrr_service.structrr.bootstrap import bootstrap_structrr
from powdrr_service.structrr.story_interview import (
    StoryInterview,
    StoryUserResponse,
    parse_story_interview_edit,
)
from powdrr_service.structrr.story_semantic_review import StorySemanticReviewer
from powdrr_service.structrr.user_stories import (
    STORY_SECTIONS,
    _decode,
)

from powdrr_lift.workrr.execution_state import _WorkflowExecutionState


class StoryInterviewOperations:
    def __init__(
        self,
        state: _WorkflowExecutionState,
        execution_id: str,
        original_request: str,
        *,
        snapshot_loader: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.state = state
        self.execution_id = execution_id
        self.original_request = original_request
        self.root = state.error_log_root / ".powdrr" / "procedrr-runs"
        self.path = self.root / f"{execution_id}.story-interview.json"
        self.answers_path = self.root / f"{execution_id}.story-inputs.json"
        self.snapshot_loader = snapshot_loader or self._load_snapshot
        self.reviewer: StorySemanticReviewer | None = None

    def _load_snapshot(self) -> Mapping[str, Any]:
        from powdrr_service.resources import bundled_process_directory

        taxonomy = self.state.worktree_root / "software_development_entity_taxonomy.md"
        if not taxonomy.is_file():
            taxonomy = (
                bundled_process_directory().parent
                / "software_development_entity_taxonomy.md"
            )
        result = bootstrap_structrr(
            self.state.worktree_root,
            taxonomy_path=taxonomy,
            output_path=self.root / f"{self.execution_id}.story-baseline.yaml",
        )
        if not result.validation.successful:
            raise ValueError(
                "story design requires a valid repository snapshot: "
                + "; ".join(issue.message for issue in result.validation.issues)
            )
        return result.document

    def _write(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        temporary.write_text(
            json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def load(self) -> StoryInterview:
        if not self.path.is_file():
            raise ValueError("start the story interview before editing or confirming")
        session = StoryInterview.from_data(
            json.loads(self.path.read_text(encoding="utf-8"))
        )
        if session.to_data()["session_id"] != self.execution_id:
            raise ValueError("story checkpoint belongs to another execution")
        return session

    def save(self, session: StoryInterview) -> None:
        self._write(self.path, session.to_data())

    def execute(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        action = parameters.get("action")
        allowed = {
            "start": {"work_item_name", "story_mode", "existing_story_ref", "options"},
            "select": {"story_id"},
            "status": set(),
            "context": {"keywords", "module_ids"},
            "edit": {"edit", "checkpoint_key"},
            "ask": {"dimension", "text", "proposed_assumption"},
            "review": {"dimension"},
            "present": set(),
            "publish": set(),
        }
        if (
            not isinstance(action, str)
            or action not in allowed
            or set(parameters) - {"command", "action"} - allowed[action]
        ):
            raise ValueError(
                "unsupported story operation or model-authored authority fields"
            )
        if action == "start":
            name = parameters.get("work_item_name")
            if not isinstance(name, str):
                raise ValueError("story start requires work_item_name")
            options = parameters.get("options", {})
            if not isinstance(options, Mapping) or set(options) - {
                "story_mode",
                "existing_story_ref",
            }:
                raise ValueError(
                    "story options require typed mode and existing catalog reference"
                )
            mode = options.get("story_mode", parameters.get("story_mode", "create"))
            if mode not in {"create", "amend"}:
                raise ValueError("story_mode must be create or amend")
            existing_ref = options.get(
                "existing_story_ref", parameters.get("existing_story_ref")
            )
            if existing_ref is not None and not isinstance(existing_ref, str):
                raise ValueError("existing_story_ref must be a catalog ID")
            if self.path.exists():
                session = self.load()
                if (
                    session.to_data()["work_item_name"] != name
                    or session.to_data()["instruction_ledger"]["source"]["text"]
                    != self.original_request
                    or session.to_data()["start_options"]
                    != {"story_mode": mode, "existing_story_ref": existing_ref}
                ):
                    raise ValueError(
                        "story checkpoint inputs differ from the current request"
                    )
                return session.status()
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.state.worktree_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            session = StoryInterview.start(
                self.execution_id,
                name,
                self.original_request,
                self.snapshot_loader(),
                source_revision=revision,
                story_mode=mode,
                existing_story_ref=existing_ref,
            )
            if self.answers_path.exists():
                for key, answer in json.loads(
                    self.answers_path.read_text(encoding="utf-8")
                ).items():
                    session.record_prior_answer(
                        answer["question"], answer["answer"], key
                    )
            self.save(session)
            return session.status()
        session = self.load()
        if action == "select":
            result = session.select_story(
                _decode(str, parameters.get("story_id"), "story_id")
            )
        elif action == "status":
            result = session.status()
        elif action == "context":
            keywords: tuple[str, ...] = _decode(
                tuple[str, ...], parameters.get("keywords", []), "keywords"
            )
            module_ids: tuple[str, ...] = _decode(
                tuple[str, ...], parameters.get("module_ids", []), "module_ids"
            )
            result = session.retrieve_context(keywords, module_ids)
        elif action == "edit":
            edit = parse_story_interview_edit(parameters.get("edit"))
            result = session.edit(
                edit, _decode(str, parameters.get("checkpoint_key"), "checkpoint_key")
            )
        elif action == "ask":
            result = session.ask(
                _decode(str, parameters.get("dimension"), "dimension"),
                _decode(str, parameters.get("text"), "text"),
                proposed_assumption=_decode(
                    str | None,
                    parameters.get("proposed_assumption"),
                    "proposed_assumption",
                ),
            )
        elif action == "review":
            if self.reviewer is None:
                raise ValueError("story semantic reviewer is unavailable")
            result = session.review(
                _decode(str, parameters.get("dimension"), "dimension"), self.reviewer
            )
        elif action == "present":
            self._check_dependencies(session)
            result = session.present()
        else:
            result = self._publish(session)
        self.save(session)
        return result

    def validate_question(self, parameters: Mapping[str, Any]) -> None:
        self.load().pending_question(
            _decode(str, parameters.get("story_question_id"), "story_question_id"),
            _question_text(parameters),
            allow_answered=True,
        )

    def record_user_answer(
        self, parameters: Mapping[str, Any], answer: str
    ) -> dict[str, Any]:
        question_id = parameters.get("story_question_id")
        text = _question_text(parameters)
        if question_id is not None:
            if not isinstance(question_id, str):
                raise ValueError("story question identity must be a string")
            session = self.load()
            result = session.accept_user_response(
                StoryUserResponse(question_id, text, answer)
            )
            self.save(session)
            return {"answer": answer, "story_answer": result}
        # Real prior interview answers remain available to the story service,
        # even when the authoring sub-process starts later or the chat resumes.
        answers = (
            json.loads(self.answers_path.read_text(encoding="utf-8"))
            if self.answers_path.exists()
            else {}
        )
        from powdrr_service.decision_obligation import content_fingerprint

        key = content_fingerprint({"question": text, "answer": answer})
        answers[key] = {"question": text, "answer": answer}
        self._write(self.answers_path, answers)
        if self.path.exists():
            session = self.load()
            if not session.status()["ready"]:
                session.record_prior_answer(text, answer, key)
                self.save(session)
        return {"answer": answer}

    def _check_dependencies(self, session: StoryInterview) -> None:
        try:
            session.require_current_dependencies(self.snapshot_loader())
        finally:
            self.save(session)

    def _publish(self, session: StoryInterview) -> dict[str, Any]:
        self._check_dependencies(session)
        proposal = session.compile_proposal()
        name = session.to_data()["work_item_name"]
        relative = Path("docs") / "proposals" / name
        root = self.state.worktree_root
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("story publication path escapes its worktree")
        sections: dict[str, list[dict[str, Any]]] = {
            section: [] for section in STORY_SECTIONS
        }
        for operation in proposal.operations:
            sections[operation.section].append(
                {
                    "id": operation.subject_id,
                    "action": operation.action,
                    "content": dict(operation.content),
                    **(
                        {"expected_before": operation.expected_before}
                        if operation.expected_before
                        else {}
                    ),
                }
            )
        artifact = session.evidence_artifact()
        seed = {
            "work_item_name": name,
            "feature_description": self.original_request,
            **{
                f"{section}_edits": {"added": changes, "deleted": []}
                for section, changes in sections.items()
            },
            "story_design": {
                "status": "ready",
                "story_id": session.story_id,
                "contract_digest": session.status()["contract_digest"],
                "evidence_fingerprint": artifact["fingerprint"],
            },
        }
        artifacts = {
            "story-proposal-revision.json": proposal.to_data(),
            "story-interview-evidence.json": artifact,
            "design-interview-input.json": seed,
        }
        for filename, value in artifacts.items():
            self._write(target / filename, value)
        declaration = {
            "schema": "https://powdrr.io/schemas/specification-v1",
            "id": name + "-story-design",
            "title": session.catalog.stories_by_id[session.story_id].title,
            **{section: records for section, records in sections.items() if records},
        }
        target.mkdir(parents=True, exist_ok=True)
        (target / "story-design.yaml").write_text(
            yaml.safe_dump(declaration, sort_keys=False), encoding="utf-8"
        )
        paths = tuple(
            str(relative / filename) for filename in [*artifacts, "story-design.yaml"]
        )
        if self.state.file_added_callback:
            self.state.file_added_callback(paths)
        return {
            "ready": True,
            "story_id": session.story_id,
            "contract_digest": session.status()["contract_digest"],
            "input_path": str(relative / "design-interview-input.json"),
            "proposal_path": str(relative / "story-proposal-revision.json"),
            "paths": list(paths),
        }

    def require_publish_readiness(
        self,
        files_to_publish: Sequence[str] | None = None,
        *,
        require_committed: bool = False,
    ) -> None:
        session = self.load()
        expected = session.compile_proposal().to_data()
        path = (
            self.state.worktree_root
            / "docs"
            / "proposals"
            / session.to_data()["work_item_name"]
            / "story-proposal-revision.json"
        )
        if (
            not path.is_file()
            or json.loads(path.read_text(encoding="utf-8")) != expected
        ):
            raise ValueError(
                "publish requires the exact confirmed story proposal artifact"
            )
        evidence_path = path.with_name("story-interview-evidence.json")
        if (
            not evidence_path.is_file()
            or json.loads(evidence_path.read_text(encoding="utf-8"))
            != session.evidence_artifact()
        ):
            raise ValueError(
                "story publication evidence differs from the recorded user responses"
            )
        required_paths = [
            path.with_name(filename)
            for filename in (
                "story-proposal-revision.json",
                "story-interview-evidence.json",
                "design-interview-input.json",
                "story-design.yaml",
            )
        ]
        if files_to_publish is not None:
            if isinstance(files_to_publish, (str, bytes)) or not all(
                isinstance(item, str) for item in files_to_publish
            ):
                raise ValueError(
                    "story publication files must be an explicit path list"
                )
            required = {
                item.relative_to(self.state.worktree_root).as_posix()
                for item in required_paths
            }
            if not required <= set(files_to_publish):
                raise ValueError("publication omits required confirmed story artifacts")
        if require_committed:
            for artifact in required_paths:
                relative = artifact.relative_to(self.state.worktree_root).as_posix()
                committed = subprocess.run(
                    ["git", "show", "HEAD:" + relative],
                    cwd=self.state.worktree_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if committed.returncode != 0 or committed.stdout != artifact.read_text(
                    encoding="utf-8"
                ):
                    raise ValueError(
                        "commit the exact confirmed story artifacts "
                        "before publishing this head"
                    )
        from powdrr_service.structrr.user_stories import parse_story_changes

        expected_changes: dict[str, list[dict[str, Any]]] = {
            section: [] for section in STORY_SECTIONS
        }
        for operation in expected["operations"]:
            expected_changes[operation["section"]].append(
                {
                    "id": operation["subject_id"],
                    "action": operation["action"],
                    "content": operation["content"],
                    **(
                        {"expected_before": operation["expected_before"]}
                        if operation.get("expected_before")
                        else {}
                    ),
                }
            )
        seed = json.loads(
            path.with_name("design-interview-input.json").read_text(encoding="utf-8")
        )
        seed_sections: dict[str, Any] = {
            section: seed.get(section + "_edits", {}).get("added", [])
            for section in STORY_SECTIONS
        }
        expected_seed_design = {
            "status": "ready",
            "story_id": session.story_id,
            "contract_digest": session.status()["contract_digest"],
            "evidence_fingerprint": session.evidence_artifact()["fingerprint"],
        }
        if seed.get("story_design") != expected_seed_design or parse_story_changes(
            seed_sections
        ) != parse_story_changes(expected_changes):
            raise ValueError("story interview seed differs from confirmed evidence")
        for filename in ("story-design.yaml", "feature-pr-specification.yaml"):
            declaration = path.with_name(filename)
            if not declaration.is_file():
                if filename == "story-design.yaml":
                    raise ValueError("confirmed story declaration is missing")
                continue
            actual = yaml.safe_load(declaration.read_text(encoding="utf-8"))
            if not isinstance(actual, Mapping) or parse_story_changes(
                actual
            ) != parse_story_changes(expected_changes):
                raise ValueError(
                    "published story declaration differs from the confirmed proposal"
                )

        self._check_dependencies(session)


def _question_text(parameters: Mapping[str, Any]) -> str:
    text = parameters.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("user question requires its exact display text")
    return text

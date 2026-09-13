"""Model response transport, repair, and provider fallback logic.

This module turns provider responses into validated workflow payloads. It does
not know how a selected workflow mutates state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO, cast

import laga

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workflow_chat_io import (
    _prompt_user,
    _verbose_json,
    _verbose_print,
)
from powdrr_lift.workflow_chat_selection import (
    SkillChatConfig,
    WorkflowChatConfig,
)
from powdrr_lift.workflow_execution_loop import _print_waiting_for_model
from powdrr_lift.workflow_llm import (
    WorkflowLLMClient,
    build_clean_room_repair_prompt,
    prompt_size_breakdown,
)
from powdrr_lift.workflow_llm import (
    complete_json as _request_json,
)
from powdrr_lift.workflow_provider_runtime import model_limits_for
from powdrr_lift.workrr.provider_config import LLMModelMapping
from powdrr_lift.workrr.providers import (
    LocalModelRuntimeError,
    _EmptyProviderResponseError,
    _estimate_message_tokens,
    _ModelUnavailableError,
    _SemanticRepairExhaustedError,
    backup_model_for,
    long_context_backup_for,
)

_MAX_EMPTY_QUESTION_REPROMPTS = 3
_MAX_REPEATED_REPAIR_ATTEMPTS = 5
_CONTEXT_SAFETY_MARGIN_TOKENS = 1024


def _chat_support(name: str) -> Any:
    from powdrr_lift import workflow_chat_agent

    return getattr(workflow_chat_agent, name)


def _support_call(name: str, *args: Any, **kwargs: Any) -> Any:
    return _chat_support(name)(*args, **kwargs)


def _complete_json_with_model_fallback(
    *,
    client_for: Callable[[str, str], WorkflowLLMClient],
    messages: list[dict[str, str]],
    context: str,
    model: str,
    parser: Callable[[dict[str, Any]], Any],
    repair_instructions: str,
    config: SkillChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    model_mappings: Sequence[tuple[str, LLMModelMapping]],
    provider: str,
    empty_response_fallback_payload: dict[str, Any] | None = None,
    error_recorder: Callable[[RuntimeError, dict[str, Any] | None], None] | None = None,
    response_schema: Mapping[str, Any] | None = None,
) -> tuple[Any | None, str, str]:
    active_model = model
    active_provider = provider
    attempted_models = {model.casefold()}
    while True:
        long_context_backup = long_context_backup_for(
            active_model,
            model_mappings,
        )
        estimated_input_tokens = _estimate_message_tokens(messages)
        _verbose_json(
            stderr,
            config.verbose,
            "Prompt size breakdown",
            prompt_size_breakdown(messages),
        )
        active_limits = model_limits_for(active_provider, active_model)
        if (
            long_context_backup is not None
            and estimated_input_tokens + _CONTEXT_SAFETY_MARGIN_TOKENS
            >= active_limits.context_window
            and long_context_backup.model.casefold() not in attempted_models
        ):
            print(
                f"{context} estimated context is too large for model "
                f"{active_model!r} ({estimated_input_tokens} input tokens; "
                f"limit {active_limits.context_window}). Switching to long-"
                f"context backup model {long_context_backup.model!r}.",
                file=stderr,
            )
            attempted_models.add(long_context_backup.model.casefold())
            active_model = long_context_backup.model
            active_provider = long_context_backup.provider
            continue
        try:
            result = _complete_json_with_repair(
                client_for(active_model, active_provider),
                messages,
                context=context,
                model=active_model,
                parser=parser,
                repair_instructions=repair_instructions,
                config=config,
                input_func=input_func,
                stdout=stdout,
                stderr=stderr,
                fallback_on_transient_exhaustion=(
                    backup_model_for(active_model, model_mappings) is not None
                ),
                empty_response_fallback_payload=empty_response_fallback_payload,
                error_recorder=error_recorder,
                response_schema=response_schema,
            )
            return result, active_model, active_provider
        except _ModelUnavailableError as exc:
            backup_model = backup_model_for(active_model, model_mappings)
            if (
                backup_model is None
                or backup_model.model.casefold() in attempted_models
            ):
                print(
                    f"{context} model {active_model!r} is unavailable and no "
                    "unused backup model is configured.",
                    file=stderr,
                )
                return None, active_model, active_provider
                if isinstance(exc, _SemanticRepairExhaustedError):
                    print(
                        f"{context} semantic repair was exhausted for "
                        f"{active_model!r}: {exc}. Switching to backup model "
                        f"{backup_model.model!r}.",
                        file=stderr,
                    )
                else:
                    print(
                        f"{context} model {active_model!r} is unavailable: {exc}. "
                        f"Switching to backup model {backup_model.model!r}.",
                        file=stderr,
                    )
            attempted_models.add(backup_model.model.casefold())
            active_model = backup_model.model
            active_provider = backup_model.provider


def _complete_json_with_repair(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    *,
    context: str,
    model: str,
    parser: Callable[[dict[str, Any]], Any],
    repair_instructions: str,
    config: WorkflowChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    fallback_on_transient_exhaustion: bool = False,
    empty_response_fallback_payload: dict[str, Any] | None = None,
    error_recorder: Callable[[RuntimeError, dict[str, Any] | None], None] | None = None,
    response_schema: Mapping[str, Any] | None = None,
) -> Any | None:
    empty_question_reprompts = 0
    empty_response_reprompts = 0
    last_repair_fingerprint: tuple[str, str] | None = None
    repeated_repair_count = 0
    while True:
        _verbose_json(
            stderr,
            config.verbose,
            f"{context} LLM input (model={model})",
            messages,
        )
        _print_waiting_for_model(stderr, model)
        try:
            payload = _request_json(client, messages, response_schema=response_schema)
            _verbose_json(
                stderr,
                config.verbose,
                f"{context} LLM output (model={model})",
                payload,
            )
        except RuntimeError as exc:
            if error_recorder is not None:
                error_recorder(exc, None)
            if isinstance(exc, _ModelUnavailableError):
                raise
            if isinstance(exc, LocalModelRuntimeError):
                raise
            if _is_model_unavailable_error(exc):
                raise _ModelUnavailableError(
                    f"provider reported that model {model!r} is unavailable"
                ) from exc
            if _is_transient_provider_error(exc):
                retry_attempts = max(1, config.provider_retry_attempts)
                for retry_attempt in range(1, retry_attempts + 1):
                    delay_seconds = max(0.0, config.provider_retry_delay_seconds)
                    print(
                        f"{context} failed for model {model!r}: {exc}. "
                        f"Waiting {delay_seconds:g} seconds before automatic "
                        f"retry {retry_attempt}/{retry_attempts}.",
                        file=stderr,
                    )
                    _chat_support("time").sleep(delay_seconds)
                    try:
                        _print_waiting_for_model(stderr, model)
                        payload = _request_json(
                            client, messages, response_schema=response_schema
                        )
                        _verbose_json(
                            stderr,
                            config.verbose,
                            f"{context} LLM retry output (model={model})",
                            payload,
                        )
                        break
                    except RuntimeError as retry_exc:
                        if error_recorder is not None:
                            error_recorder(retry_exc, None)
                        if _is_model_unavailable_error(retry_exc):
                            raise _ModelUnavailableError(
                                f"provider reported that model {model!r} is unavailable"
                            ) from retry_exc
                        exc = retry_exc
                else:
                    payload = None

                if payload is not None:
                    try:
                        return parser(payload)
                    except RuntimeError as retry_parse_exc:
                        if error_recorder is not None:
                            error_recorder(retry_parse_exc, payload)
                        print(
                            f"{context} retry response needs repair: {retry_parse_exc}",
                            file=stderr,
                        )
                else:
                    print(
                        f"{context} automatic retries exhausted for model {model!r}.",
                        file=stderr,
                    )
                    if fallback_on_transient_exhaustion:
                        raise _ModelUnavailableError(
                            f"provider retries were exhausted for model {model!r}"
                        ) from exc
            elif _is_json_repairable_error(exc):
                _verbose_print(
                    stderr,
                    config.verbose,
                    f"Attempting automatic repair for {context} after provider failure",
                )
                repair_error_message = str(exc)
                if _is_empty_response_error(exc):
                    repair_error_message += (
                        " The response cannot be empty. Return a complete "
                        "corrected JSON object."
                    )
                try:
                    repaired_payload = _attempt_json_repair(
                        client,
                        messages,
                        context=context,
                        model=model,
                        error_message=repair_error_message,
                        repair_instructions=repair_instructions,
                        stderr=stderr,
                        verbose=config.verbose,
                        error_recorder=error_recorder,
                        response_schema=response_schema,
                    )
                except _EmptyProviderResponseError as empty_exc:
                    empty_response_reprompts += 1
                    _print_empty_response_exchange(
                        context=context,
                        model=model,
                        attempts=empty_response_reprompts,
                        provider_error=str(empty_exc),
                        messages=empty_exc.messages or messages,
                        stderr=stderr,
                    )
                    if empty_response_reprompts > 1:
                        if empty_response_fallback_payload is not None:
                            print(
                                f"{context} corrective response was empty; "
                                "interpreting it as next_step.",
                                file=stderr,
                            )
                            return parser(empty_response_fallback_payload)
                        if not _ask_to_retry_empty_response(
                            context=context,
                            model=model,
                            attempts=empty_response_reprompts,
                            provider_error=str(empty_exc),
                            messages=empty_exc.messages or messages,
                            input_func=input_func,
                            stdout=stdout,
                            stderr=stderr,
                        ):
                            return None
                        empty_response_reprompts = 0
                        continue
                    print(
                        f"{context} returned an empty response; requesting a "
                        "corrected response.",
                        file=stderr,
                    )
                    messages = _build_json_repair_messages(
                        messages,
                        context=context,
                        error_message=(
                            f"{empty_exc} The response cannot be empty. Return a "
                            "complete corrected JSON object."
                        ),
                        repair_instructions=repair_instructions,
                        previous_payload=None,
                    )
                    continue
                if repaired_payload is not None:
                    repair_fingerprint = _repair_response_fingerprint(
                        messages,
                        repaired_payload,
                    )
                    if repair_fingerprint == last_repair_fingerprint:
                        repeated_repair_count += 1
                    else:
                        repeated_repair_count = 0
                    if repeated_repair_count >= _MAX_REPEATED_REPAIR_ATTEMPTS:
                        print(
                            f"{context} made no progress during response repair; "
                            "switching to the configured fallback model.",
                            file=stderr,
                        )
                        raise _SemanticRepairExhaustedError(
                            f"{context} repeated the same invalid response "
                            f"{_MAX_REPEATED_REPAIR_ATTEMPTS} times"
                        ) from None
                    last_repair_fingerprint = repair_fingerprint
                    try:
                        return parser(repaired_payload)
                    except RuntimeError as repair_exc:
                        if error_recorder is not None:
                            error_recorder(repair_exc, repaired_payload)
                        print(
                            "Repaired "
                            f"{context} response was still invalid: {repair_exc}",
                            file=stderr,
                        )
                        if _is_invalid_user_question_error(repair_exc):
                            empty_question_reprompts += 1
                            if empty_question_reprompts > _MAX_EMPTY_QUESTION_REPROMPTS:
                                raise PowdrrExecutionError(
                                    f"{context} LLM repeatedly returned an invalid "
                                    "user question."
                                ) from repair_exc
                            print(
                                f"{context} received an invalid user question. "
                                "Requesting a properly formed English question "
                                "from the LLM "
                                f"(attempt {empty_question_reprompts}/"
                                f"{_MAX_EMPTY_QUESTION_REPROMPTS}).",
                                file=stderr,
                            )
                            messages = _build_json_repair_messages(
                                messages,
                                context=context,
                                error_message=str(repair_exc),
                                repair_instructions=(
                                    repair_instructions
                                    + " Return a concise, specific, properly formed "
                                    "English question ending with a question mark in "
                                    "the user-question field."
                                ),
                                previous_payload=repaired_payload,
                            )
                            continue
                print(
                    f"{context} repair request failed; requesting the original "
                    "response again with an updated correction instruction.",
                    file=stderr,
                )
                messages = _build_json_repair_messages(
                    messages,
                    context=context,
                    error_message=(
                        f"{exc} The repair request itself failed. Correct the "
                        "original response directly."
                    ),
                    repair_instructions=repair_instructions,
                    previous_payload=None,
                )
                continue
            else:
                print(f"{context} failed: {exc}", file=stderr)
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
                status_stream=stderr,
            )
            _verbose_print(
                stderr,
                config.verbose,
                f"User chose {retry!r} after {context} failure",
            )
            if retry.strip().lower() == "retry":
                continue
            print(f"Stopping after {context} failure.", file=stderr)
            return None
        assert payload is not None
        try:
            return parser(payload)
        except RuntimeError as exc:
            if error_recorder is not None:
                error_recorder(exc, payload)
            print(f"{context} response needs repair: {exc}", file=stderr)
            _verbose_print(
                stderr,
                config.verbose,
                f"Attempting automatic repair for {context} after validation failure",
            )
            repair_error_message = str(exc)
            if _is_empty_response_error(exc):
                repair_error_message += (
                    " The response cannot be empty. Return a complete corrected "
                    "JSON object."
                )
            try:
                repaired_payload = _attempt_json_repair(
                    client,
                    messages,
                    context=context,
                    model=model,
                    error_message=repair_error_message,
                    repair_instructions=repair_instructions,
                    previous_payload=payload,
                    stderr=stderr,
                    verbose=config.verbose,
                    error_recorder=error_recorder,
                    response_schema=response_schema,
                )
            except _EmptyProviderResponseError as empty_exc:
                empty_response_reprompts += 1
                _print_empty_response_exchange(
                    context=context,
                    model=model,
                    attempts=empty_response_reprompts,
                    provider_error=str(empty_exc),
                    messages=empty_exc.messages or messages,
                    stderr=stderr,
                )
                if empty_response_reprompts > 1:
                    if not _ask_to_retry_empty_response(
                        context=context,
                        model=model,
                        attempts=empty_response_reprompts,
                        provider_error=str(empty_exc),
                        messages=empty_exc.messages or messages,
                        input_func=input_func,
                        stdout=stdout,
                        stderr=stderr,
                    ):
                        return None
                    empty_response_reprompts = 0
                    continue
                print(
                    f"{context} returned an empty response; requesting a corrected "
                    "response.",
                    file=stderr,
                )
                messages = _build_json_repair_messages(
                    messages,
                    context=context,
                    error_message=(
                        f"{empty_exc} The response cannot be empty. Return a "
                        "complete corrected JSON object."
                    ),
                    repair_instructions=repair_instructions,
                    previous_payload=payload,
                )
                continue
            if repaired_payload is not None:
                repair_fingerprint = _repair_response_fingerprint(
                    messages,
                    repaired_payload,
                )
                if repair_fingerprint == last_repair_fingerprint:
                    repeated_repair_count += 1
                else:
                    repeated_repair_count = 0
                if repeated_repair_count >= _MAX_REPEATED_REPAIR_ATTEMPTS:
                    print(
                        f"{context} made no progress during response repair; "
                        "switching to the configured fallback model.",
                        file=stderr,
                    )
                    raise _SemanticRepairExhaustedError(
                        f"{context} repeated the same invalid response "
                        f"{_MAX_REPEATED_REPAIR_ATTEMPTS} times"
                    ) from None
                last_repair_fingerprint = repair_fingerprint
                try:
                    return parser(repaired_payload)
                except RuntimeError as repair_exc:
                    if error_recorder is not None:
                        error_recorder(repair_exc, repaired_payload)
                    print(
                        f"{context} repaired response was still invalid: {repair_exc}",
                        file=stderr,
                    )
                    if _is_invalid_user_question_error(repair_exc):
                        empty_question_reprompts += 1
                        if empty_question_reprompts > _MAX_EMPTY_QUESTION_REPROMPTS:
                            raise PowdrrExecutionError(
                                f"{context} LLM repeatedly returned an invalid user "
                                "question."
                            ) from repair_exc
                        print(
                            f"{context} received an invalid user question. "
                            "Requesting a properly formed English question from "
                            "the LLM "
                            f"(attempt {empty_question_reprompts}/"
                            f"{_MAX_EMPTY_QUESTION_REPROMPTS}).",
                            file=stderr,
                        )
                        messages = _build_json_repair_messages(
                            messages,
                            context=context,
                            error_message=str(repair_exc),
                            repair_instructions=(
                                repair_instructions
                                + " Return a concise, specific, properly formed "
                                "English question ending with a question mark in the "
                                "user-question field."
                            ),
                            previous_payload=repaired_payload,
                        )
                        continue
            print(
                f"{context} repair request failed; requesting the original "
                "response again with an updated correction instruction.",
                file=stderr,
            )
            messages = _build_json_repair_messages(
                messages,
                context=context,
                error_message=(
                    f"{exc} The repair request itself failed. Correct the original "
                    "response directly."
                ),
                repair_instructions=repair_instructions,
                previous_payload=(
                    repaired_payload if repaired_payload is not None else payload
                ),
            )
            continue
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
                status_stream=stderr,
            )
            _verbose_print(
                stderr,
                config.verbose,
                f"User chose {retry!r} after {context} repair failure",
            )
            if retry.strip().lower() == "retry":
                continue
            print(f"Stopping after {context} failure.", file=stderr)
            return None


def _is_invalid_user_question_error(exc: RuntimeError) -> bool:
    return "must be a non-empty, properly formed English question" in str(exc)


def _ask_to_retry_empty_response(
    *,
    context: str,
    model: str,
    attempts: int,
    provider_error: str,
    messages: Sequence[dict[str, str]],
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
) -> bool:
    """Ask for an explicit recovery choice instead of completing silently."""
    _print_empty_response_exchange(
        context=context,
        model=model,
        attempts=attempts,
        provider_error=provider_error,
        messages=messages,
        stderr=stderr,
    )

    answer = _prompt_user(
        (
            f"{context} returned an empty response after {attempts} corrective "
            "reprompt attempts. Would you like me to retry this LLM request? "
            "Answer 'retry' or 'stop': "
        ),
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    return answer.strip().lower() in {"retry", "yes", "y"}


def _print_empty_response_exchange(
    *,
    context: str,
    model: str,
    attempts: int,
    provider_error: str,
    messages: Sequence[dict[str, str]],
    stderr: TextIO,
) -> None:
    """Print every empty exchange, including ones recovered automatically."""
    diagnostic = (
        f"Empty-response context: {context}; model={model!r}; "
        f"corrective-reprompt-attempts={attempts}; provider_error={provider_error}\n"
        "LLM request messages:\n"
        f"{json.dumps(list(messages), indent=2, ensure_ascii=False)}\n"
        "LLM response: <empty>"
    )
    serialized_prompt = json.dumps(list(messages), ensure_ascii=False)
    print(diagnostic, file=stderr, flush=True)
    print(
        "[workflow] Empty-response exchange: "
        f"prompt={serialized_prompt} response=<empty>",
        file=stderr,
        flush=True,
    )


def _parse_json_object(content: str, context: str) -> dict[str, Any]:
    normalized_content = content.strip()
    try:
        parsed_content = json.loads(normalized_content)
    except json.JSONDecodeError as exc:
        try:
            parsed_content = laga.repair(normalized_content)
        except laga.LagaError:
            parsed_content = _extract_embedded_json_object(normalized_content)
            if parsed_content is None:
                raise PowdrrExecutionError(
                    f"{context} was not valid JSON: {exc.msg} at line "
                    f"{exc.lineno}, column {exc.colno}.\nResponse content:\n{content}"
                ) from exc
    if not isinstance(parsed_content, dict):
        raise PowdrrExecutionError(f"{context} must be a JSON object.")
    return cast("dict[str, Any]", parsed_content)


def _extract_embedded_json_object(content: str) -> dict[str, Any] | None:
    """Accept JSON objects surrounded by common LLM presentation noise."""
    fenced_blocks = re.findall(
        r"```(?:json|JSON)?\s*\n?(.*?)```",
        content,
        flags=re.DOTALL,
    )
    candidates = [*fenced_blocks, content]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)
    return None


def _attempt_json_repair(
    client: WorkflowLLMClient,
    messages: Sequence[dict[str, str]],
    *,
    context: str,
    model: str,
    error_message: str,
    repair_instructions: str,
    stderr: TextIO,
    verbose: bool,
    previous_payload: dict[str, Any] | None = None,
    error_recorder: Callable[[RuntimeError, dict[str, Any] | None], None] | None = None,
    response_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    repair_messages = _build_json_repair_messages(
        messages,
        context=context,
        error_message=error_message,
        repair_instructions=repair_instructions,
        previous_payload=previous_payload,
    )
    _verbose_json(
        stderr,
        verbose,
        f"{context} repair LLM input (model={model})",
        repair_messages,
    )
    try:
        _print_waiting_for_model(stderr, model)
        repaired_payload = _request_json(
            client, repair_messages, response_schema=response_schema
        )
        _verbose_json(
            stderr,
            verbose,
            f"{context} repair LLM output (model={model})",
            repaired_payload,
        )
        return repaired_payload
    except RuntimeError as exc:
        if error_recorder is not None:
            error_recorder(exc, None)
        if isinstance(exc, LocalModelRuntimeError):
            raise
        if _is_empty_response_error(exc):
            raise _EmptyProviderResponseError(
                str(exc),
                messages=repair_messages,
            ) from exc
        print(f"{context} repair request failed: {exc}", file=stderr)
    return None


def _repair_response_fingerprint(
    messages: Sequence[dict[str, str]],
    payload: dict[str, Any],
) -> tuple[str, str]:
    # The prompt history necessarily grows on every repair attempt.  Including
    # it in the fingerprint therefore made an identical malformed payload look
    # like progress forever.  Compare the response itself so a provider that
    # keeps replaying the same invalid action is stopped deterministically.
    return (
        "response",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _build_json_repair_messages(
    messages: Sequence[dict[str, str]],
    *,
    context: str,
    error_message: str,
    repair_instructions: str,
    previous_payload: dict[str, Any] | None,
) -> list[dict[str, str]]:
    # All model-required correction requests use the same clean-room builder as
    # semantic action recovery.  The original conversation is deliberately not
    # carried forward: it was the context in which the invalid response was
    # produced.  Retain only the structured failure facts needed to correct it.
    recovery_context = json.dumps(
        {
            "response_context": context,
            "previous_response": previous_payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    repaired_messages, _manifest = build_clean_room_repair_prompt(
        context=recovery_context,
        error_message=error_message,
        repair_instructions=(
            repair_instructions
            + (
                " Return only a complete corrected JSON object with no markdown or "
                "commentary."
            )
            + (
                " Do not repeat that response; change the field identified "
                "by the validation error."
                if previous_payload is not None
                else ""
            )
        ),
        model="json-repair",
    )
    return repaired_messages


def _is_json_repairable_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "was not valid json",
            "content was empty",
            "did not include any content",
            "did not include any choices",
            "choice was not an object",
            "message was not an object",
            "must be a json object",
        )
    )


def _is_empty_response_error(exc: RuntimeError) -> bool:
    return "response message content was empty" in str(exc).lower() or (
        "response content was empty" in str(exc).lower()
    )


def _is_transient_provider_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return (
        "http 429" in message
        or '"code":"1305"' in message
        or "temporarily overloaded" in message
        or _is_timeout_error(exc)
    )


def _is_timeout_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def _is_model_unavailable_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "model" in message and (
        "not available" in message
        or "unavailable" in message
        or "not found" in message
        or "does not exist" in message
        or "unsupported" in message
    )

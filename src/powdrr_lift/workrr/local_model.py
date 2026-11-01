"""Download and validate the local model used by workflow providers."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from powdrr_lift.errors import PowdrrExecutionError

LOCAL_MODEL_PATTERN = "qwen2.5-coder-14b-instruct-q5_k_m*.gguf"
LOCAL_MODEL_CONTEXT_ENV = "POWDRR_LOCAL_MODEL_CONTEXT"
_DEFAULT_LOCAL_MODEL_CONTEXT = 24576

_LOCAL_MODEL_REPOSITORY = "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF"


def download_local_qwen_model(model_cache_dir: Path) -> Path:
    """Download the local Qwen GGUF shards into the configured cache."""
    model_cache_dir.mkdir(parents=True, exist_ok=True)
    cached_model_paths = sorted(model_cache_dir.glob(LOCAL_MODEL_PATTERN))
    if _has_all_local_model_shards(cached_model_paths):
        return cached_model_paths[0]
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise PowdrrExecutionError(
            "Automatic local model downloads require huggingface-hub."
        ) from exc
    try:
        snapshot_directory = Path(
            snapshot_download(
                repo_id=_LOCAL_MODEL_REPOSITORY,
                allow_patterns=[LOCAL_MODEL_PATTERN],
                local_dir=str(model_cache_dir),
            )
        )
    except Exception as exc:
        raise PowdrrExecutionError(
            "Could not download the Qwen Q5_K_M model from Hugging Face. "
            f"Repository={_LOCAL_MODEL_REPOSITORY}, cache={model_cache_dir}. "
            f"Underlying error: {type(exc).__name__}: {exc}"
        ) from exc
    model_paths = sorted(snapshot_directory.glob(LOCAL_MODEL_PATTERN))
    if not _has_all_local_model_shards(model_paths):
        raise PowdrrExecutionError(
            "The Hugging Face Qwen repository did not provide all Q5_K_M GGUF shards."
        )
    return model_paths[0]


def resolve_local_model_path(model_cache_dir: Path) -> Path:
    cached_model_paths = sorted(model_cache_dir.glob(LOCAL_MODEL_PATTERN))
    if _has_all_local_model_shards(cached_model_paths):
        return cached_model_paths[0]
    raise PowdrrExecutionError(
        "The local Qwen model is not fully cached. Run "
        "`powdrr-lift download-qwen-model` before starting workflow-chat. "
        f"Expected cache={model_cache_dir}."
    )


def resolve_local_model_context() -> int:
    configured_context = os.environ.get(LOCAL_MODEL_CONTEXT_ENV)
    if configured_context is None or not configured_context.strip():
        return _DEFAULT_LOCAL_MODEL_CONTEXT
    try:
        context = int(configured_context)
    except ValueError as exc:
        raise PowdrrExecutionError(
            f"{LOCAL_MODEL_CONTEXT_ENV} must be a positive integer; got "
            f"{configured_context!r}."
        ) from exc
    if context <= 0:
        raise PowdrrExecutionError(
            f"{LOCAL_MODEL_CONTEXT_ENV} must be a positive integer; got "
            f"{configured_context!r}."
        )
    return context


def _has_all_local_model_shards(model_paths: Sequence[Path]) -> bool:
    if not model_paths:
        return False
    match = re.search(r"-00001-of-(\d+)\.gguf$", model_paths[0].name)
    expected_shards = int(match.group(1)) if match else 1
    present_indices = {
        int(shard.group(1))
        for model_path in model_paths
        if (shard := re.search(r"-(\d{5})-of-\d+\.gguf$", model_path.name))
    }
    return len(present_indices) == expected_shards

"""Local composition root for the staged agent/service boundary."""

from powdrr_service.local_service import LocalPowdrrService

from powdrr_local.bridge import JsonServiceBridge
from powdrr_local.miniswe_inference import create_miniswe_inference_handler

__all__ = [
    "JsonServiceBridge",
    "LocalPowdrrService",
    "create_miniswe_inference_handler",
]

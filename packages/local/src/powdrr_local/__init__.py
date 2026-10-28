"""Local composition root for the staged agent/service boundary."""

from powdrr_service.local_service import LocalPowdrrService

from powdrr_local.bridge import JsonServiceBridge

__all__ = ["JsonServiceBridge", "LocalPowdrrService"]

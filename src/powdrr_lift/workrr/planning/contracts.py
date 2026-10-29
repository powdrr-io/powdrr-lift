"""Compatibility import for service-owned planning question contracts.

The planning contract implementation moved to ``powdrr_service``. Existing
application and test imports remain supported during the staged extraction.
"""

from powdrr_service.planning import contracts as _contracts
from powdrr_service.planning.contracts import *  # noqa: F403

__all__ = _contracts.__all__

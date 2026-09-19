"""Compatibility import for source checkouts.

The installable adapter lives under ``powdrr_lift.integrations`` so it ships
in the wheel. Keep this source-tree path for existing Harbor configurations
and repository bootstrap tooling.
"""

from powdrr_lift.integrations.harbor.powdrr_agent import PowdrrAgent

__all__ = ["PowdrrAgent"]

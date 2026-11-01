"""Service-owned errors raised by concrete model provider clients."""

from __future__ import annotations


class ProviderClientError(RuntimeError):
    """A provider transport or model client failed."""


class ProviderHTTPError(ProviderClientError):
    """A provider returned an HTTP error response."""

    def __init__(self, provider: str, status_code: int, detail: str) -> None:
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"{provider} request failed with HTTP {status_code}: {detail}")


class ProviderResponseError(ProviderClientError):
    """A provider response could not satisfy the requested JSON contract."""

    def __init__(self, message: str, *, error_code: str = "invalid_provider_response"):
        self.error_code = error_code
        super().__init__(message)

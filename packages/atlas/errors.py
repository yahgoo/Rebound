"""Atlas typed errors. Codes are preserved verbatim (INTERFACES.md §0)."""

from __future__ import annotations

from decimal import Decimal


class AtlasError(Exception):
    """Base. Carries the Atlas error code verbatim; never swallow the code."""

    code: str
    message: str
    http_status: int | None

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
    ) -> None:
        self.code = str(code)
        self.message = message
        self.http_status = http_status
        super().__init__(f"[{self.code}] {self.message}")


class AtlasAuthError(AtlasError):
    """Bad credentials, or source IP not allowlisted [E]."""


class AtlasNoResultsError(AtlasError):
    """Search returned zero offers."""


class AtlasPriceMovedError(AtlasError):
    """verify.do returned a different price."""

    old_price: Decimal
    new_price: Decimal

    def __init__(
        self,
        code: str,
        message: str,
        *,
        old_price: Decimal,
        new_price: Decimal,
        http_status: int | None = None,
    ) -> None:
        self.old_price = old_price
        self.new_price = new_price
        super().__init__(code, message, http_status)


class AtlasPaymentDeclinedError(AtlasError):
    """code "604" [E]."""


class AtlasThreeDSRequiredError(AtlasError):
    """code "616" [E]."""


class AtlasTimeoutError(AtlasError):
    """Transport timed out waiting for Atlas."""


class CassetteMissError(AtlasError):
    """Replay mode, no recording for this request."""

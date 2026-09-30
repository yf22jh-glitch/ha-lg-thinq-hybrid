"""Exceptions raised by the Kocom Energy integration."""


class KocomEnergyError(Exception):
    """Base exception for Kocom Energy failures."""


class IpAddressNotFoundError(KocomEnergyError):
    """Raised when the Kocom server IP cannot be discovered."""


class AuthenticationError(KocomEnergyError):
    """Raised when Kocom authentication fails."""


class ProtocolError(KocomEnergyError):
    """Raised when a Kocom response cannot be parsed safely."""


class EnergyDataPendingError(ProtocolError):
    """Raised when the server has not created the current-month data yet."""

    def __init__(self, response_bytes: int) -> None:
        self.response_bytes = response_bytes
        super().__init__(
            f"Current-month energy data is not ready ({response_bytes} byte response)"
        )

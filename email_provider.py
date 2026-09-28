"""Provider-neutral email delivery contract.

Provider implementations receive only the already validated message fields.
"""

from typing import List, Optional, Protocol, TypedDict, runtime_checkable


class EmailProviderResult(TypedDict):
    success: bool
    provider: str
    provider_message_id: Optional[str]
    reason_code: Optional[str]


@runtime_checkable
class EmailProvider(Protocol):
    """Minimal provider-neutral email interface."""

    provider_name: str
    simulation_only: bool

    def send(self, *, to: List[str], cc: List[str], subject: str,
             body: str) -> EmailProviderResult:
        """Process only the validated recipient and message fields."""

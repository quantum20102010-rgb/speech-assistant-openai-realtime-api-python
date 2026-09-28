"""In-memory email provider test double; it never sends or uses the network."""

from copy import deepcopy
from typing import List

from email_provider import EmailProviderResult


class FakeEmailProvider:
    provider_name = "fake"
    simulation_only = True

    def __init__(self):
        self.attempts = []

    def send(self, *, to: List[str], cc: List[str], subject: str,
             body: str) -> EmailProviderResult:
        self.attempts.append(deepcopy({
            "to": to,
            "cc": cc,
            "subject": subject,
            "body": body,
        }))
        return {
            "success": True,
            "provider": self.provider_name,
            "provider_message_id": f"fake-message-{len(self.attempts)}",
            "reason_code": "simulated_success",
        }

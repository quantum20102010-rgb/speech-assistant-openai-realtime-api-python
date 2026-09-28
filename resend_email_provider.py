"""Resend-backed EmailProvider with fail-closed send controls."""

import os
import threading
from typing import List

from email_draft import EMAIL_PATTERN
from email_provider import EmailProviderResult


_SDK_LOCK = threading.RLock()


class ResendEmailProvider:
    provider_name = "resend"
    simulation_only = False

    def __init__(self, api_key, from_address, *, sdk=None):
        self._api_key = api_key
        self._from_address = from_address
        self._sdk = sdk

    @staticmethod
    def _result(success, reason_code, provider_message_id=None):
        return {
            "success": success,
            "provider": "resend",
            "provider_message_id": provider_message_id,
            "reason_code": reason_code,
        }

    def send(self, *, to: List[str], cc: List[str], subject: str,
             body: str) -> EmailProviderResult:
        """Send only when explicitly enabled outside dry-run mode.

        The method accepts no mission, transcript, credential, or call context.
        """
        dry_run = os.getenv("EMAIL_DRY_RUN", "true").strip().casefold()
        if dry_run == "true":
            return self._result(False, "dry_run_only")
        if dry_run != "false":
            return self._result(False, "email_configuration_invalid")

        enabled = os.getenv("EMAIL_ENABLED", "false").strip().casefold()
        if enabled != "true":
            return self._result(
                False,
                "email_disabled" if enabled == "false" else "email_configuration_invalid",
            )
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            return self._result(False, "provider_not_configured")
        if (not isinstance(self._from_address, str)
                or not EMAIL_PATTERN.fullmatch(self._from_address)):
            return self._result(False, "from_address_invalid")
        if (not isinstance(to, list) or not to
                or any(not isinstance(address, str) or not EMAIL_PATTERN.fullmatch(address)
                       for address in to)):
            return self._result(False, "recipient_invalid")
        if (not isinstance(cc, list)
                or any(not isinstance(address, str) or not EMAIL_PATTERN.fullmatch(address)
                       for address in cc)):
            return self._result(False, "recipient_invalid")
        if not isinstance(subject, str) or not subject.strip():
            return self._result(False, "subject_empty")
        if not isinstance(body, str) or not body.strip():
            return self._result(False, "body_empty")

        payload = {
            "from": self._from_address,
            "to": list(to),
            "subject": subject,
            "text": body,
        }
        if cc:
            payload["cc"] = list(cc)

        try:
            sdk = self._sdk
            if sdk is None:
                import resend as sdk
            with _SDK_LOCK:
                previous_key = getattr(sdk, "api_key", None)
                try:
                    sdk.api_key = self._api_key
                    response = sdk.Emails.send(payload)
                finally:
                    sdk.api_key = previous_key
            message_id = (
                response.get("id") if isinstance(response, dict)
                else getattr(response, "id", None)
            )
        except Exception:
            return self._result(False, "provider_request_failed")

        if not isinstance(message_id, str) or not message_id or len(message_id) > 200:
            return self._result(False, "provider_response_invalid")
        return self._result(True, "sent_by_provider", message_id)

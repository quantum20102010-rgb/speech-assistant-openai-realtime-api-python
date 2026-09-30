"""Fail-closed provider health classification and safe admin alerts."""

from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
import threading
from decimal import Decimal, InvalidOperation

import requests


def parse_twilio_minimum_balance(value):
    """Return a valid non-negative USD minimum, or None for invalid configuration."""
    try:
        minimum = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not minimum.is_finite() or minimum < 0:
        return None
    return minimum


class DependencyStatus(str, Enum):
    AVAILABLE = "available"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    AUTHENTICATION_ERROR = "authentication_error"
    QUOTA_OR_BALANCE_EXHAUSTED = "quota_or_balance_exhausted"
    SPEND_LIMIT = "spend_limit"
    SERVICE_ERROR = "service_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DependencyCheck:
    service: str
    status: DependencyStatus
    reason: str
    recommended_action: str
    latch: bool = False


@dataclass(frozen=True)
class AdminAlert:
    service: str
    severity: str
    reason: str
    consequence: str
    recommended_action: str

    def to_dict(self):
        return asdict(self)


class DependencyHealthRegistry:
    """In-process circuit breaker; raw provider responses are never retained."""

    LATCHED_STATUSES = {
        DependencyStatus.DISABLED,
        DependencyStatus.NOT_CONFIGURED,
        DependencyStatus.AUTHENTICATION_ERROR,
        DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
        DependencyStatus.SPEND_LIMIT,
        DependencyStatus.UNKNOWN,
    }

    def __init__(self, alert_limit=50):
        self._lock = threading.RLock()
        self._states = {}
        self._latched = set()
        self._alerts = deque(maxlen=alert_limit)

    def record(self, check, consequence="La operación dependiente queda bloqueada."):
        with self._lock:
            previous = self._states.get(check.service)
            self._states[check.service] = check
            if check.latch or check.status in self.LATCHED_STATUSES:
                self._latched.add(check.service)
            elif check.status == DependencyStatus.AVAILABLE:
                self._latched.discard(check.service)
            if previous and previous.status == check.status and previous.reason == check.reason:
                return None

            severity = (
                "critical"
                if check.status in {
                    DependencyStatus.AUTHENTICATION_ERROR,
                    DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
                    DependencyStatus.SPEND_LIMIT,
                    DependencyStatus.DISABLED,
                    DependencyStatus.UNKNOWN,
                }
                else "error"
                if check.status == DependencyStatus.SERVICE_ERROR
                else "info"
            )
            alert = AdminAlert(
                service=check.service,
                severity=severity,
                reason=check.reason,
                consequence=consequence,
                recommended_action=check.recommended_action,
            )
            self._alerts.append(alert)
            return alert

    def is_latched(self, service):
        with self._lock:
            return service in self._latched

    def state(self, service):
        with self._lock:
            return self._states.get(service)

    def alerts(self):
        with self._lock:
            return tuple(self._alerts)

    def snapshot(self):
        with self._lock:
            states = {
                service: {
                    "status": check.status.value,
                    "reason": check.reason,
                    "recommended_action": check.recommended_action,
                    "calls_blocked": check.status != DependencyStatus.AVAILABLE,
                    "latched": service in self._latched,
                }
                for service, check in self._states.items()
            }
            return {
                "dependencies": states,
                "alerts": [alert.to_dict() for alert in self._alerts],
            }


def twilio_rest_error(code=None, status_code=None):
    normalized = str(code) if code is not None else ""
    if normalized == "10001":
        return DependencyCheck(
            "twilio", DependencyStatus.UNKNOWN, "account_not_active",
            "Revisa en Twilio el estado de la cuenta, saldo y posibles restricciones antes de reactivar llamadas.",
            latch=True,
        )
    if normalized == "10005":
        return DependencyCheck(
            "twilio", DependencyStatus.DISABLED, "voice_disabled",
            "Revisa en Twilio Console si Programmable Voice está habilitado para la cuenta.",
            latch=True,
        )
    if normalized == "20003" or normalized == "20403" or status_code in {401, 403}:
        return DependencyCheck(
            "twilio", DependencyStatus.AUTHENTICATION_ERROR,
            "authentication_or_permission_denied",
            "Verifica las credenciales, permisos y el estado de la cuenta Twilio en Console.",
            latch=True,
        )
    if normalized == "20429" or status_code == 429:
        return DependencyCheck(
            "twilio", DependencyStatus.SERVICE_ERROR, "provider_rate_limited",
            "Revisa los límites de solicitudes/llamadas de Twilio; la aplicación no reintentará automáticamente.",
        )
    if status_code is not None and status_code >= 500:
        return DependencyCheck(
            "twilio", DependencyStatus.SERVICE_ERROR, "provider_service_error",
            "Revisa Twilio Status y los registros del proveedor antes de intentar otra operación manualmente.",
        )
    return DependencyCheck(
        "twilio", DependencyStatus.SERVICE_ERROR, "call_request_rejected",
        "Revisa el código de error de Twilio y la configuración de Voice; no se reintentó la llamada.",
    )


OPENAI_SPEND_CODES = {
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "billing_hard_limit_reached",
}
OPENAI_QUOTA_CODES = {
    "credit_balance_exhausted",
    "organization_usage_limit_exceeded",
    "usage_limit_reached",
    "insufficient_quota",
}
OPENAI_AUTH_CODES = {"invalid_api_key", "authentication_error", "invalid_authorization"}


def openai_error(code=None, error_type=None, status_code=None):
    safe_code = code.lower() if isinstance(code, str) else ""
    safe_type = error_type.lower() if isinstance(error_type, str) else ""
    if safe_code in OPENAI_SPEND_CODES:
        return DependencyCheck(
            "openai", DependencyStatus.SPEND_LIMIT, safe_code,
            "Revisa el límite de gasto del proyecto/organización en OpenAI Platform; no aumentes el límite sin autorización.",
            latch=True,
        )
    if safe_code in OPENAI_QUOTA_CODES or safe_type == "insufficient_quota":
        reason = safe_code if safe_code in OPENAI_QUOTA_CODES else "insufficient_quota"
        return DependencyCheck(
            "openai", DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED, reason,
            "Revisa créditos, límite de uso y facturación del proyecto/organización en OpenAI Platform.",
            latch=True,
        )
    if safe_code in OPENAI_AUTH_CODES or safe_type == "authentication_error" or status_code in {401, 403}:
        return DependencyCheck(
            "openai", DependencyStatus.AUTHENTICATION_ERROR, "authentication_or_permission_denied",
            "Verifica la API key y sus permisos/proyecto en el gestor de secretos y OpenAI Platform.",
            latch=True,
        )
    if safe_code == "rate_limit_exceeded" or status_code == 429:
        return DependencyCheck(
            "openai", DependencyStatus.SERVICE_ERROR, "request_rate_limited",
            "Revisa los límites de solicitudes/tokens del modelo; no se reintentará automáticamente.",
        )
    if safe_type == "server_error" or (status_code is not None and status_code >= 500):
        return DependencyCheck(
            "openai", DependencyStatus.SERVICE_ERROR, "provider_service_error",
            "Revisa OpenAI Status y los límites del modelo antes de volver a intentar manualmente.",
        )
    if status_code == 404:
        return DependencyCheck(
            "openai", DependencyStatus.NOT_CONFIGURED, "realtime_model_unavailable",
            "Confirma que el proyecto tenga acceso al modelo Realtime configurado.",
            latch=True,
        )
    return DependencyCheck(
        "openai", DependencyStatus.UNKNOWN, "unclassified_provider_error",
        "Revisa el estado y los códigos documentados de OpenAI; la sesión dependiente se cerró sin reintentar.",
        latch=True,
    )


def transport_error(service, reason):
    if reason not in {"timeout", "network_error", "ambiguous_timeout", "realtime_error"}:
        reason = "network_error"
    action = (
        "Confirma en los registros del proveedor si la operación alcanzó a aceptarse antes del timeout; no repitas la operación automáticamente."
        if reason == "ambiguous_timeout"
        else "Revisa conectividad, estado del proveedor y timeout configurado antes de un nuevo intento manual."
    )
    return DependencyCheck(service, DependencyStatus.SERVICE_ERROR, reason, action)


def twilio_preflight(
    account_sid, auth_token, timeout=5, http_get=None, minimum_balance=Decimal("5.00")
):
    """Read documented Twilio account and balance resources; never create calls."""
    minimum_balance = parse_twilio_minimum_balance(minimum_balance)
    if minimum_balance is None:
        return DependencyCheck(
            "twilio", DependencyStatus.UNKNOWN, "twilio_minimum_balance_invalid",
            "Corrige TWILIO_MIN_BALANCE_USD; las llamadas permanecen bloqueadas.",
            latch=True,
        )
    if not account_sid or not auth_token:
        return DependencyCheck(
            "twilio", DependencyStatus.NOT_CONFIGURED, "credentials_missing",
            "Configura las credenciales Twilio requeridas en el gestor de secretos.",
            latch=True,
        )
    get = http_get or requests.get
    base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
    try:
        account_response = get(f"{base}.json", auth=(account_sid, auth_token), timeout=timeout)
        if account_response.status_code >= 400:
            return twilio_rest_error(
                status_code=account_response.status_code,
            )
        account = account_response.json()
        if account.get("status") != "active":
            return DependencyCheck(
                "twilio", DependencyStatus.DISABLED, "account_not_active",
                "Revisa el estado de la cuenta Twilio en Console antes de habilitar llamadas.",
                latch=True,
            )
        balance_response = get(
            f"{base}/Balance.json", auth=(account_sid, auth_token), timeout=timeout
        )
        if balance_response.status_code >= 400:
            return twilio_rest_error(status_code=balance_response.status_code)
        balance_data = balance_response.json()
        if balance_data.get("currency") != "USD":
            return DependencyCheck(
                "twilio", DependencyStatus.UNKNOWN, "twilio_balance_unknown",
                "No se pudo confirmar el saldo de Twilio en USD; las llamadas permanecen bloqueadas.",
                latch=True,
            )
        try:
            balance = Decimal(str(balance_data.get("balance", "")))
        except (InvalidOperation, TypeError):
            return DependencyCheck(
                "twilio", DependencyStatus.UNKNOWN, "twilio_balance_unknown",
                "Revisa manualmente el saldo y el estado de la cuenta Twilio.", latch=True,
            )
        if not balance.is_finite():
            return DependencyCheck(
                "twilio", DependencyStatus.UNKNOWN, "twilio_balance_unknown",
                "Revisa manualmente el saldo y el estado de la cuenta Twilio.", latch=True,
            )
        if balance < minimum_balance:
            return DependencyCheck(
                "twilio", DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
                "twilio_balance_below_minimum",
                "El saldo de Twilio está por debajo del mínimo configurado; recarga saldo antes de permitir llamadas.",
                latch=True,
            )
        if balance <= 0:
            return DependencyCheck(
                "twilio", DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
                "non_positive_balance",
                "Revisa el saldo y la facturación Twilio; no se intentó crear una llamada.",
                latch=True,
            )
        return DependencyCheck(
            "twilio", DependencyStatus.AVAILABLE, "account_active_balance_positive",
            "El chequeo solo confirma estado y saldo en este momento; verifica límites de gasto antes de habilitar llamadas.",
        )
    except requests.Timeout:
        return transport_error("twilio", "timeout")
    except requests.RequestException:
        return transport_error("twilio", "network_error")
    except Exception:
        return DependencyCheck(
            "twilio", DependencyStatus.UNKNOWN, "health_check_unavailable",
            "Revisa manualmente la disponibilidad de Twilio; no se intentó crear una llamada.",
            latch=True,
        )


def openai_preflight(api_key, timeout=5, http_get=None):
    """Check documented Realtime model access without generating a response."""
    if not api_key:
        return DependencyCheck(
            "openai", DependencyStatus.NOT_CONFIGURED, "api_key_missing",
            "Configura OPENAI_API_KEY en el gestor de secretos.", latch=True,
        )
    get = http_get or requests.get
    try:
        response = get(
            "https://api.openai.com/v1/models/gpt-realtime",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if response.status_code < 400:
            return DependencyCheck(
                "openai", DependencyStatus.AVAILABLE, "realtime_model_accessible",
                "El chequeo confirma acceso al modelo, pero no garantiza saldo ni margen bajo límites de gasto.",
            )
        code = None
        error_type = None
        try:
            error_data = response.json().get("error", {})
            code = error_data.get("code")
            error_type = error_data.get("type")
        except Exception:
            pass
        return openai_error(code, error_type, response.status_code)
    except requests.Timeout:
        return transport_error("openai", "timeout")
    except requests.RequestException:
        return transport_error("openai", "network_error")
    except Exception:
        return DependencyCheck(
            "openai", DependencyStatus.UNKNOWN, "health_check_unavailable",
            "Revisa manualmente OpenAI; no se inició una sesión ni generó contenido.",
            latch=True,
        )

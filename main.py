import os
import json
import binascii
import base64
import asyncio
import hmac
import math
import re
import websockets
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlencode
import requests

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from requests.exceptions import RequestException, Timeout
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.twiml.voice_response import VoiceResponse, Connect

from dotenv import load_dotenv
from call_safety import (
    CallAdmissionError,
    CallSafetyConfig,
    InMemoryCallManager,
    SafetyConfigurationError,
    normalize_destination,
)
from call_results import (
    CallTranscript,
    LocalFileCallResultStorage,
    empty_call_result,
    normalize_model_result,
)
from decision_engine import DecisionEngine, failed_decision
from notification_decision import decide_notification
from notification_message import build_notification_message
from whatsapp_adapter import prepare_whatsapp
from call_availability import evaluate_call_availability
from missions import (
    MissionConfigurationError,
    MissionNotFoundError,
    load_mission_catalog,
)
from mission_state import update_mission_state
from email_draft import build_email_draft, update_draft_approval_status
from email_executor import execute_email_action
from resend_email_provider import ResendEmailProvider
from whatsapp_executor import execute_whatsapp_action, mask_whatsapp_recipient
from action_executor import prepare_call_actions
from mission_decision import (
    MissionDecisionError,
    normalize_decision_records,
    resolve_persisted_mission_decision,
)
from dependency_health import (
    DependencyCheck,
    DependencyHealthRegistry,
    DependencyStatus,
    openai_error,
    openai_preflight,
    transport_error,
    twilio_preflight,
    twilio_rest_error,
)

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
dependency_health = DependencyHealthRegistry()
PORT = int(os.getenv("PORT", 5050))
TWILIO_HTTP_TIMEOUT = 15
DEFAULT_MAX_CALL_DURATION_SECONDS = 240


def configured_call_duration_limit() -> float:
    try:
        duration = float(
            os.getenv(
                "MAX_CALL_DURATION_SECONDS",
                str(DEFAULT_MAX_CALL_DURATION_SECONDS),
            )
        )
    except (TypeError, ValueError):
        return float(DEFAULT_MAX_CALL_DURATION_SECONDS)

    if not math.isfinite(duration) or duration <= 0:
        return float(DEFAULT_MAX_CALL_DURATION_SECONDS)
    return duration


MAX_CALL_DURATION_SECONDS = min(configured_call_duration_limit(), 240)
call_manager = InMemoryCallManager()
call_result_storage = LocalFileCallResultStorage()
def configured_email_action_provider():
    """Build the explicitly selected provider only when fully configured."""
    if os.getenv("EMAIL_ENABLED", "false").strip().casefold() != "true":
        return None
    if os.getenv("EMAIL_PROVIDER", "").strip().casefold() != "resend":
        return None
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    from_address = os.getenv("RESEND_FROM", "").strip()
    if not api_key or not from_address:
        return None
    return ResendEmailProvider(api_key=api_key, from_address=from_address)


email_action_provider = configured_email_action_provider()


def resolve_mission(mission_id=None):
    return load_mission_catalog().get(mission_id)


def safety_config_or_none():
    try:
        return CallSafetyConfig.from_env()
    except SafetyConfigurationError:
        return None


def calls_are_enabled():
    config = safety_config_or_none()
    return bool(config and config.enabled)


def configured_dependency_status():
    config = safety_config_or_none()
    raw_enabled = os.getenv("CALLS_ENABLED", "false")
    if raw_enabled != "true":
        for service in ("twilio", "openai"):
            dependency_health.record(DependencyCheck(
                service, DependencyStatus.DISABLED, "calls_disabled",
                "Habilita llamadas solo después de revisar límites financieros y configuración.",
                latch=True,
            ))
        return None
    if config is None:
        checks = []
        for service in ("twilio", "openai"):
            check = DependencyCheck(
                service, DependencyStatus.UNKNOWN, "invalid_safety_configuration",
                "Corrige la configuración de seguridad; las llamadas permanecen bloqueadas.",
                latch=True,
            )
            dependency_health.record(check)
            checks.append(check)
        return checks[0]
    values = {
        "twilio": (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"), os.getenv("TWILIO_PHONE_NUMBER")),
        "openai": (os.getenv("OPENAI_API_KEY"),),
    }
    for service, credentials in values.items():
        if any(not value or not value.strip() for value in credentials):
            check = DependencyCheck(
                service, DependencyStatus.NOT_CONFIGURED, "required_configuration_missing",
                "Completa la configuración requerida en el gestor de secretos antes de habilitar llamadas.",
                latch=True,
            )
            dependency_health.record(check)
            return check
    return None


async def check_provider_readiness():
    missing = configured_dependency_status()
    if missing:
        return missing
    twilio_check, openai_check = await asyncio.gather(
        run_in_threadpool(
            twilio_preflight, os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN"), TWILIO_HTTP_TIMEOUT,
        ),
        run_in_threadpool(
            openai_preflight, os.getenv("OPENAI_API_KEY"), TWILIO_HTTP_TIMEOUT,
        ),
    )
    dependency_health.record(twilio_check)
    dependency_health.record(openai_check)
    if twilio_check.status != DependencyStatus.AVAILABLE:
        return twilio_check
    if openai_check.status != DependencyStatus.AVAILABLE:
        return openai_check
    return None


async def check_openai_readiness():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        check = DependencyCheck(
            "openai", DependencyStatus.NOT_CONFIGURED, "api_key_missing",
            "Configura OPENAI_API_KEY en el gestor de secretos.", latch=True,
        )
    else:
        check = await run_in_threadpool(
            openai_preflight, key, TWILIO_HTTP_TIMEOUT,
        )
    dependency_health.record(check)
    return check


def readiness_response(check):
    statuses = dependency_health.snapshot()["dependencies"]
    safe_status = check.status.value
    return JSONResponse(
        {"error": "Provider readiness check blocked the operation.",
         "dependency_status": safe_status, "dependencies": statuses},
        status_code=503,
    )


async def write_call_result(result, reservation_id=None, mission=None):
    if reservation_id and not call_manager.claim_report(reservation_id=reservation_id):
        return False
    try:
        selected_mission = mission
        if selected_mission is None and result.get("mission_id"):
            try:
                selected_mission = resolve_mission(result.get("mission_id"))
            except (MissionConfigurationError, MissionNotFoundError):
                selected_mission = None
        if selected_mission is not None:
            result["mission_id"] = selected_mission.id
            result["mission_name"] = selected_mission.name
            findings = result.get("mission_findings")
            if not isinstance(findings, dict):
                findings = {}
            for field in selected_mission.required_information:
                findings.setdefault(field["key"], None)
            result["mission_findings"] = findings
        if "decision" not in result:
            try:
                result["decision"] = await run_in_threadpool(
                    DecisionEngine().analyze,
                    result,
                    result.get("transcript", ""),
                    selected_mission,
                )
            except Exception:
                result["decision"] = failed_decision()
        result["notification_decision"] = decide_notification(
            result,
            result.get("decision"),
            result.get("transcript", ""),
        )
        result["notification_message"] = build_notification_message(
            result["notification_decision"], result
        )
        result["whatsapp_preview"] = prepare_whatsapp(result["notification_message"])
        if selected_mission is not None:
            previous_state = None
            load_previous = getattr(call_result_storage, "latest_mission_state", None)
            if callable(load_previous):
                try:
                    previous_state = load_previous(selected_mission.id)
                except Exception:
                    previous_state = None
            try:
                result["mission_state"] = update_mission_state(
                    previous_state,
                    result,
                    selected_mission,
                    result["decision"],
                    result["notification_decision"],
                    result["notification_message"],
                    whatsapp_approval_needed=(
                        result["whatsapp_preview"].get("status") == "ready_for_send"
                        and result["notification_message"].get("should_send") is True
                    ),
                )
            except Exception:
                print("Mission state could not be updated.")
            result["email_draft"] = build_email_draft(
                selected_mission.id,
                result.get("mission_state"),
                result.get("decision"),
                result.get("notification_decision"),
                result,
            )
            result["email_draft"] = update_draft_approval_status(
                result["email_draft"], result.get("mission_state")
            )
            if isinstance(result.get("mission_state"), dict):
                result["mission_state"] = prepare_call_actions(
                    result["mission_state"],
                    result.get("decision"),
                    result.get("email_draft"),
                    result.get("whatsapp_preview"),
                    notification_message=result.get("notification_message"),
                )
        await run_in_threadpool(call_result_storage.write, result)
        return True
    except Exception:
        print("Call report could not be stored.")
        return False


def call_started_datetime():
    return datetime.now(timezone.utc)

VOICE = "cedar"


class HoldDetector:
    """Conservatively detect sustained, repeating non-speech audio patterns."""

    MIN_HOLD_SECONDS = 30
    ACTIVE_RMS_THRESHOLD = 0.025
    WINDOW_FRAMES = 100  # 2 seconds at Twilio's usual 20 ms media cadence.
    MAX_PERIOD_FRAMES = 1250  # Compare repeating patterns up to 25 seconds.

    def __init__(self):
        self.frames = deque(maxlen=self.MAX_PERIOD_FRAMES + self.WINDOW_FRAMES)
        self.first_active_ms = None
        self.last_active_ms = None
        self.last_check_ms = -1000

    @staticmethod
    def _decode_ulaw_sample(value: int) -> int:
        value = (~value) & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        return -sample if sign else sample

    def observe(self, audio_payload: str, timestamp_ms: int) -> bool:
        try:
            encoded = base64.b64decode(audio_payload, validate=True)
        except (binascii.Error, ValueError, TypeError):
            return False
        if not encoded:
            return False

        samples = [self._decode_ulaw_sample(value) for value in encoded]
        square_sum = sum(sample * sample for sample in samples)
        rms = math.sqrt(square_sum / len(samples)) / 32768
        crossings = sum(
            1
            for previous, current in zip(samples, samples[1:])
            if (previous < 0 <= current) or (previous >= 0 > current)
        )
        zero_crossing_rate = crossings / max(len(samples) - 1, 1)
        self.frames.append((rms, zero_crossing_rate))

        if rms >= self.ACTIVE_RMS_THRESHOLD:
            if (
                self.last_active_ms is None
                or timestamp_ms - self.last_active_ms > 5000
            ):
                self.first_active_ms = timestamp_ms
            self.last_active_ms = timestamp_ms

        if (
            self.first_active_ms is None
            or timestamp_ms - self.first_active_ms < self.MIN_HOLD_SECONDS * 1000
            or timestamp_ms - self.last_check_ms < 1000
        ):
            return False

        self.last_check_ms = timestamp_ms
        return self._has_repeating_pattern()

    def _has_repeating_pattern(self) -> bool:
        history = list(self.frames)
        window = self.WINDOW_FRAMES
        if len(history) < window * 2:
            return False

        current = history[-window:]
        active = [
            frame for frame in current if frame[0] >= self.ACTIVE_RMS_THRESHOLD
        ]
        if len(active) < window * 0.2 or len(active) >= window * 0.98:
            return False

        # Reject flat silence/noise and require a changing, tonal pattern.
        signatures = {
            (round(rms, 2), round(zcr, 2))
            for rms, zcr in current
        }
        transitions = sum(
            current[index] != current[index - 1]
            for index in range(1, len(current))
        )
        mean_zcr = sum(frame[1] for frame in active) / len(active)
        if len(signatures) < 2 or transitions < 1 or not 0.035 <= mean_zcr <= 0.40:
            return False

        max_period = min(self.MAX_PERIOD_FRAMES, len(history) - window)
        for period in range(window, max_period + 1, 5):
            previous = history[-window - period : -period]
            matches = 0
            for (current_rms, current_zcr), (previous_rms, previous_zcr) in zip(
                current, previous
            ):
                rms_scale = max(current_rms, previous_rms, 0.03)
                if (
                    abs(current_rms - previous_rms) / rms_scale <= 0.45
                    and abs(current_zcr - previous_zcr) <= 0.12
                ):
                    matches += 1
            if matches / window >= 0.90:
                return True
        return False


async def wait_for_call_end(receive_task, send_task, duration_seconds):
    timer_task = asyncio.create_task(asyncio.sleep(duration_seconds))
    try:
        done, _ = await asyncio.wait(
            (receive_task, send_task, timer_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timer_task in done:
            return "max_duration"

        completed_task = receive_task if receive_task in done else send_task
        try:
            result = completed_task.result()
        except Exception:
            return "connection_error"
        return result or (
            "twilio_disconnected"
            if completed_task is receive_task
            else "realtime_disconnected"
        )
    finally:
        if not timer_task.done():
            timer_task.cancel()
            await asyncio.gather(timer_task, return_exceptions=True)
        for task in (receive_task, send_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive_task, send_task, return_exceptions=True)


async def finish_call(call_sid, openai_ws, twilio_ws, complete_twilio=True):
    async def close_quietly(connection):
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass

    await asyncio.gather(
        complete_twilio_call(call_sid) if complete_twilio else asyncio.sleep(0),
        close_quietly(openai_ws),
        close_quietly(twilio_ws),
    )


async def complete_twilio_call(call_sid):
    if not call_sid:
        return
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        print("Twilio call termination could not be requested.")
        return
    try:
        from twilio.rest import Client

        client = Client(
            account_sid,
            auth_token,
            http_client=TwilioHttpClient(timeout=TWILIO_HTTP_TIMEOUT),
        )
        await run_in_threadpool(
            client.calls(call_sid).update,
            status="completed",
        )
    except TwilioRestException as exc:
        dependency_health.record(twilio_rest_error(
            getattr(exc, "code", None), getattr(exc, "status", None)
        ))
        print("Twilio call termination request failed.")
    except Timeout:
        dependency_health.record(transport_error("twilio", "ambiguous_timeout"))
        print("Twilio call termination request failed.")
    except RequestException:
        dependency_health.record(transport_error("twilio", "network_error"))
        print("Twilio call termination request failed.")
    except Exception:
        dependency_health.record(transport_error("twilio", "network_error"))
        print("Twilio call termination request failed.")


REPORT_EXTRACTION_TIMEOUT_SECONDS = 12
REPORT_FIELDS_FOR_PROMPT = [
    "company", "contact_name", "contact_role", "email", "phone",
    "distribution_interest", "b2b_terms", "minimum_order_quantity",
    "pricing_information", "discount_information", "territories",
    "exclusivity", "relevant_information", "summary", "next_action",
    "follow_up_reason", "follow_up_date",
]


async def extract_call_report(openai_ws, status, language, started_at, duration,
                              transcript_text="", mission=None):
    fallback = empty_call_result(
        status, language, started_at, duration, mission
    )
    fallback["transcript"] = transcript_text
    if openai_ws is None:
        return fallback
    prompt = (
        "Prepare a post-call structured report from this conversation only. "
        "Never invent or infer a person's identity, company, contact information, "
        "commercial terms, commitments, or dates. Use null for every field not "
        "explicitly supported by the conversation. Do not put the called number "
        "in the phone field unless the contact explicitly stated it. Return only "
        "one JSON object with these fields: "
        + ", ".join(REPORT_FIELDS_FOR_PROMPT)
    )
    if mission:
        prompt += (
            ", and mission_findings with these keys: "
            + ", ".join(field["key"] for field in mission.required_information)
            + ". "
            "\n\n" + mission.instructions()
            + "\nFor mission_findings, return one string or null for each listed key. "
            "Keep the existing structured fields and summary as well."
        )
    prompt += (
        " Make summary useful and concise, including identity/company, interest, "
        "terms, objections, requested information, commitments, and next action "
        "only when those details were stated."
    )
    try:
        # Change to text only after telephony has ended so live turns remain
        # audio-only and the final report does not add text output per turn.
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": {"output_modalities": ["text"]},
        }))
        await openai_ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        }))
        await openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "output_modalities": ["text"],
                "metadata": {"purpose": "call_report"},
            },
        }))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + REPORT_EXTRACTION_TIMEOUT_SECONDS
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return fallback
            message = await asyncio.wait_for(openai_ws.recv(), timeout=remaining)
            event = json.loads(message)
            if event.get("type") != "response.done":
                continue
            response = event.get("response", {})
            if response.get("metadata", {}).get("purpose") != "call_report":
                continue
            text = "".join(
                content.get("text", "")
                for item in response.get("output", [])
                for content in item.get("content", [])
                if content.get("type") in {"output_text", "text"}
            )
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                return fallback
            extracted = json.loads(text[start:end + 1])
            result = normalize_model_result(
                extracted, status, language, started_at, duration, mission
            )
            result["transcript"] = transcript_text
            return result
    except Exception:
        return fallback


async def finalize_call(call_sid, reservation_id, openai_ws, twilio_ws,
                        end_reason, language, started_at, duration,
                        transcript_text="", mission=None):
    report_status = {
        "twilio_stopped": "completed",
        "twilio_disconnected": "disconnected",
        "realtime_disconnected": "error",
        "connection_error": "error",
        "openai_dependency_error": "error",
    }.get(end_reason, end_reason)
    if end_reason != "twilio_stopped":
        await complete_twilio_call(call_sid)
    report_socket = None if end_reason == "openai_dependency_error" else openai_ws
    result = await extract_call_report(
        report_socket, report_status, language, started_at, duration,
        transcript_text, mission,
    )
    await finish_call(
        call_sid, openai_ws, twilio_ws, complete_twilio=False
    )
    if reservation_id:
        call_manager.release(reservation_id=reservation_id)
        await write_call_result(
            result, reservation_id=reservation_id, mission=mission
        )
    else:
        await write_call_result(result, mission=mission)


async def finish_call_for_reason(end_reason, call_sid, openai_ws, twilio_ws):
    await finish_call(
        call_sid,
        openai_ws,
        twilio_ws,
        complete_twilio=end_reason != "twilio_stopped",
    )


def public_host(request: Request) -> str:
    """Return the public host used by Twilio, preferring Railway's domain."""
    configured_host = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    candidate = configured_host or forwarded_host or request.url.hostname or ""

    # Accept a hostname with an optional scheme/port, but never copy a path,
    # query, credentials, or malformed host into a TwiML URL.
    candidate = re.sub(r"^https?://", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not candidate or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", candidate):
        raise ValueError("Could not determine a valid public host.")
    return candidate


def call_is_authorized(request: Request) -> bool:
    call_secret = os.getenv("CALL_SECRET", "")
    if not call_secret:
        return False

    supplied = request.headers.get("x-call-secret", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()

    return bool(supplied) and hmac.compare_digest(
        supplied.encode("utf-8"), call_secret.encode("utf-8")
    )

# ============================================================
# LANGUAGES
# ============================================================

LANGUAGES = {
    "spanish": "español",
    "english": "inglés",
    "french": "francés",
    "german": "alemán",
    "italian": "italiano",
    "portuguese": "portugués",
    "japanese": "japonés",
    "mandarin": "mandarín",
    "chinese": "chino",
    "korean": "coreano",
    "dutch": "neerlandés",
    "swedish": "sueco",
    "danish": "danés",
    "norwegian": "noruego",
    "polish": "polaco",
    "turkish": "turco",
    "arabic": "árabe",
    "hindi": "hindi",
    "russian": "ruso",
}

# ============================================================
# GREETINGS
# ============================================================

GREETINGS = {
    "spanish": (
        "Hola, mucho gusto. Soy el asistente de voz de Guzi Stuff. "
        "¿Con quién tengo el gusto?"
    ),

    "english": (
        "Hello, nice to meet you. I'm the voice assistant calling "
        "on behalf of Guzi Stuff. Who am I speaking with?"
    ),

    "french": (
        "Bonjour, enchanté. Je suis l'assistant vocal de Guzi Stuff. "
        "À qui ai-je le plaisir de parler ?"
    ),

    "german": (
        "Hallo, schön, Sie kennenzulernen. Ich bin der Sprachassistent "
        "von Guzi Stuff. Mit wem spreche ich bitte?"
    ),

    "italian": (
        "Buongiorno, piacere di conoscerla. Sono l'assistente vocale "
        "di Guzi Stuff. Con chi ho il piacere di parlare?"
    ),

    "portuguese": (
        "Olá, muito prazer. Sou o assistente de voz da Guzi Stuff. "
        "Com quem estou falando?"
    ),

    "japanese": (
        "こんにちは。Guzi Stuffの音声アシスタントです。"
        "どちら様でしょうか？"
    ),

    "mandarin": (
        "您好，很高兴认识您。我是 Guzi Stuff 的语音助手。"
        "请问您是哪位？"
    ),

    "chinese": (
        "您好，很高兴认识您。我是 Guzi Stuff 的语音助手。"
        "请问您是哪位？"
    ),

    "korean": (
        "안녕하세요. Guzi Stuff의 음성 비서입니다. "
        "실례하지만 성함이 어떻게 되시나요?"
    ),

    "dutch": (
        "Hallo, aangenaam kennis te maken. Ik ben de spraakassistent "
        "van Guzi Stuff. Met wie spreek ik?"
    ),

    "swedish": (
        "Hej, trevligt att träffas. Jag är röstassistenten från "
        "Guzi Stuff. Vem talar jag med?"
    ),

    "danish": (
        "Hej, rart at møde dig. Jeg er stemmeassistenten fra "
        "Guzi Stuff. Hvem taler jeg med?"
    ),

    "norwegian": (
        "Hei, hyggelig å møte deg. Jeg er taleassistenten fra "
        "Guzi Stuff. Hvem snakker jeg med?"
    ),

    "polish": (
        "Dzień dobry, miło mi. Jestem asystentem głosowym Guzi Stuff. "
        "Z kim mam przyjemność rozmawiać?"
    ),

    "turkish": (
        "Merhaba, tanıştığımıza memnun oldum. Ben Guzi Stuff'ın "
        "sesli asistanıyım. Kiminle görüşüyorum?"
    ),

    "arabic": (
        "مرحباً، تشرفت بلقائك. أنا المساعد الصوتي لشركة Guzi Stuff. "
        "مع من أتحدث؟"
    ),

    "hindi": (
        "नमस्ते, आपसे मिलकर खुशी हुई। मैं Guzi Stuff का वॉइस असिस्टेंट हूँ। "
        "मैं किससे बात कर रहा हूँ?"
    ),

    "russian": (
        "Здравствуйте, очень приятно. Я голосовой ассистент Guzi Stuff. "
        "С кем я разговариваю?"
    ),
}

# ============================================================
# SYSTEM MESSAGE
# ============================================================

SYSTEM_MESSAGE = """
You are an advanced AI voice assistant for Guzi Stuff.

You speak with people by telephone.

LANGUAGE:
- Always speak in the language selected for the call.
- Never default to Spanish unless Spanish is the selected language.
- If English is selected, speak natural fluent English.
- If Spanish is selected, speak natural Mexican Spanish.
- For any other selected language, speak naturally and fluently in that language.
- Do not switch languages unless the caller clearly asks you to.

VOICE STYLE:
- Sound natural, human, warm and professional.
- Do not sound robotic.
- Keep responses concise and conversational.
- Use natural pauses.
- Ask one question at a time.
- Do not give long speeches.
- Do not repeat information unnecessarily.
- Do not read a script mechanically.
- Adapt your responses to what the caller actually says.
- Let the caller finish speaking.
- If you do not understand something, politely ask them to repeat it.

BUSINESS:
- You are calling on behalf of Guzi Stuff.
- Guzi Stuff is an e-commerce business based in Mexico.
- Follow the selected mission for the purpose and context of each call.
- Be professional, friendly and direct.
- Never invent prices, agreements, certifications, purchase volumes,
  legal entities or commercial conditions.
- If you do not know something, say so and ask for the appropriate contact.
"""

# ============================================================
# LOGGING
# ============================================================

LOG_EVENT_TYPES = [
    "error",
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
    "session.updated",
]

app = FastAPI()


# ============================================================
# HOME
# ============================================================

@app.get("/", response_class=JSONResponse)
async def index_page():

    return {
        "message": "Guzi Stuff AI Voice Assistant is running!",
        "voice": VOICE,
        "languages": list(LANGUAGES.keys()),
    }


@app.get("/health/dependencies")
async def dependency_status(request: Request):
    if not call_is_authorized(request):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    configured_dependency_status()
    snapshot = dependency_health.snapshot()
    for service in ("twilio", "openai"):
        snapshot["dependencies"].setdefault(service, {
            "status": DependencyStatus.UNKNOWN.value,
            "reason": "not_checked",
            "recommended_action": "Ejecuta un chequeo de disponibilidad antes de habilitar llamadas.",
            "calls_blocked": True,
        })
    return JSONResponse(snapshot)


@app.get("/missions/{mission_id}/decisions")
async def list_mission_decisions(mission_id: str, request: Request):
    """Return only pending decisions for a configured mission."""
    if not call_is_authorized(request):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)

    try:
        mission = load_mission_catalog().get(mission_id)
    except (MissionConfigurationError, MissionNotFoundError):
        return JSONResponse({"error": "Mission not found."}, status_code=404)

    state = call_result_storage.latest_mission_state(mission.id)
    stored = state.get("decisions") if isinstance(state, dict) else None
    legacy_pending = (
        state.get("decisions_pending")
        if isinstance(state, dict) and not isinstance(stored, list)
        else None
    )
    records = normalize_decision_records(
        mission.id,
        stored,
        legacy_pending,
    )
    pending = []
    for item in records:
        if (
            not isinstance(item, dict)
            or item.get("mission_id") != mission.id
            or item.get("status") != "pending"
            or not isinstance(item.get("decision_id"), str)
            or not isinstance(item.get("description"), str)
        ):
            continue
        pending.append({
            "decision_id": item["decision_id"],
            "description": item["description"],
            "status": "pending",
        })
    return JSONResponse({"mission_id": mission.id, "decisions": pending})


@app.get("/missions/{mission_id}/email-draft")
async def get_mission_email_draft(mission_id: str, request: Request):
    """Read a persisted draft; this endpoint has no send operation."""
    if not call_is_authorized(request):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    try:
        mission = load_mission_catalog().get(mission_id)
    except (MissionConfigurationError, MissionNotFoundError):
        return JSONResponse({"error": "Mission not found."}, status_code=404)

    load_result = getattr(call_result_storage, "latest_mission_result", None)
    result = load_result(mission.id) if callable(load_result) else None
    if not isinstance(result, dict) or result.get("mission_id") != mission.id:
        return JSONResponse({"error": "Email draft not found."}, status_code=404)
    draft = result.get("email_draft")
    if not isinstance(draft, dict):
        return JSONResponse({"error": "Email draft not found."}, status_code=404)
    # Return only public draft fields. Do not return the transcript, decision
    # artifacts, provider metadata, mission facts, or internal identifiers.
    return JSONResponse({
        "should_create": draft.get("should_create") is True,
        "to": draft.get("to") if isinstance(draft.get("to"), list) else [],
        "cc": draft.get("cc") if isinstance(draft.get("cc"), list) else [],
        "subject": draft.get("subject") if isinstance(draft.get("subject"), str) else "",
        "body": draft.get("body") if isinstance(draft.get("body"), str) else "",
        "source": draft.get("source") if isinstance(draft.get("source"), str) else "persisted_mission_result",
        "requires_approval": True,
        "sent": False,
        "approval_status": draft.get("approval_status") or "not_created",
        "reason_code": draft.get("reason_code") or "not_available",
    })


@app.get("/missions/{mission_id}/actions/{action_id}/email-result")
async def get_email_action_result(mission_id: str, action_id: str, request: Request):
    """Read a persisted email result; this GET never executes an action."""
    if not call_is_authorized(request):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    try:
        mission = load_mission_catalog().get(mission_id)
    except (MissionConfigurationError, MissionNotFoundError):
        return JSONResponse({"error": "Mission not found."}, status_code=404)
    load_result = getattr(call_result_storage, "latest_mission_result", None)
    result = load_result(mission.id) if callable(load_result) else None
    records = result.get("email_execution_results") if isinstance(result, dict) else None
    execution = records.get(action_id) if isinstance(records, dict) else None
    if (not isinstance(execution, dict)
            or execution.get("mission_id") != mission.id
            or execution.get("action_id") != action_id):
        return JSONResponse({"error": "Email result not found."}, status_code=404)
    return JSONResponse({
        "mission_id": mission.id,
        "action_id": action_id,
        "status": execution.get("status"),
        "reason_code": execution.get("reason_code"),
        "provider": execution.get("provider", "none"),
        "provider_message_id": execution.get("provider_message_id"),
        "to": execution.get("to", []),
        "cc": execution.get("cc", []),
        "subject": execution.get("subject", ""),
        "body": execution.get("body", ""),
        "requires_approval": True,
        "sent": execution.get("sent") is True,
        "dry_run": execution.get("dry_run") is True,
        "executed_at": execution.get("executed_at"),
        "processed_at": execution.get("processed_at"),
    })


@app.get("/missions/{mission_id}/actions/{action_id}/whatsapp-result")
async def get_whatsapp_action_result(mission_id: str, action_id: str, request: Request):
    """Read a persisted WhatsApp dry-run result; this route cannot send."""
    if not call_is_authorized(request):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    try:
        mission = load_mission_catalog().get(mission_id)
    except (MissionConfigurationError, MissionNotFoundError):
        return JSONResponse({"error": "Mission not found."}, status_code=404)
    load_result = getattr(call_result_storage, "latest_mission_result", None)
    result = load_result(mission.id) if callable(load_result) else None
    records = result.get("whatsapp_execution_results") if isinstance(result, dict) else None
    execution = records.get(action_id) if isinstance(records, dict) else None
    if (not isinstance(execution, dict)
            or execution.get("mission_id") != mission.id
            or execution.get("action_id") != action_id):
        return JSONResponse({"error": "WhatsApp result not found."}, status_code=404)
    recipient = execution.get("recipient")
    masked_recipient = mask_whatsapp_recipient(recipient)
    return JSONResponse({
        "mission_id": mission.id,
        "action_id": action_id,
        "status": execution.get("status"),
        "reason_code": execution.get("reason_code"),
        "provider": "none",
        "recipient": masked_recipient,
        "message": execution.get("message", ""),
        "sent": False,
        "dry_run": execution.get("dry_run") is True,
        "executed_at": None,
        "processed_at": execution.get("processed_at"),
    })


@app.get("/missions/{mission_id}/actions")
async def list_mission_actions(mission_id: str, request: Request):
    """List local action metadata for a mission; no action can run here."""
    if not call_is_authorized(request):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    try:
        mission = load_mission_catalog().get(mission_id)
    except (MissionConfigurationError, MissionNotFoundError):
        return JSONResponse({"error": "Mission not found."}, status_code=404)
    state = call_result_storage.latest_mission_state(mission.id)
    records = state.get("actions", []) if isinstance(state, dict) else []
    actions = []
    for action in records if isinstance(records, list) else []:
        if not isinstance(action, dict) or action.get("mission_id") != mission.id:
            continue
        actions.append({
            "action_id": action.get("action_id"),
            "mission_id": mission.id,
            "action_type": action.get("action_type"),
            "description": action.get("description"),
            "status": action.get("status"),
            "requires_approval": action.get("requires_approval") is True,
            "approval_status": action.get("approval_status"),
            "created_at": action.get("created_at"),
            "approved_at": action.get("approved_at"),
            "executed_at": action.get("executed_at"),
            "reason_code": action.get("reason_code"),
        })
    return JSONResponse({"mission_id": mission.id, "actions": actions})


@app.post("/missions/{mission_id}/decisions/{decision_id}/resolve")
async def resolve_mission_decision_endpoint(
    mission_id: str, decision_id: str, request: Request
):
    """Record Fabian's decision and, for supported types, produce safe previews only."""
    if not call_is_authorized(request):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)

    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse({"error": "A JSON body is required."}, status_code=400)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    if not isinstance(payload, dict) or set(payload) - {"status", "resolution"}:
        return JSONResponse({"error": "Invalid resolution request."}, status_code=400)
    status = payload.get("status")
    if not isinstance(status, str) or status not in {"approved", "rejected", "deferred"}:
        return JSONResponse({"error": "Invalid decision status."}, status_code=400)
    resolution = payload.get("resolution")
    if resolution is not None and (
        not isinstance(resolution, str) or len(resolution.strip()) > 500
    ):
        return JSONResponse({"error": "Invalid resolution text."}, status_code=400)
    if not resolution or not resolution.strip():
        resolution = "Decisión registrada por Fabian."

    try:
        updated = resolve_persisted_mission_decision(
            call_result_storage,
            mission_id,
            decision_id,
            status,
            resolution,
            mission_loader=load_mission_catalog,
        )
    except MissionDecisionError as error:
        code = str(error)
        if code in {"mission_not_found", "mission_mismatch", "decision_not_found"}:
            return JSONResponse({"error": "Mission or decision not found."}, status_code=404)
        if code == "decision_already_resolved":
            return JSONResponse({"error": "Decision is already resolved."}, status_code=409)
        if code in {"resolution_status_invalid", "resolution_invalid"}:
            return JSONResponse({"error": "Invalid resolution request."}, status_code=400)
        return JSONResponse({"error": "Decision could not be saved."}, status_code=503)
    except Exception:
        # Do not expose or log storage/provider exception details.
        return JSONResponse({"error": "Decision could not be saved."}, status_code=503)

    record = next(
        (item for item in updated.get("decisions", [])
         if isinstance(item, dict) and item.get("decision_id") == decision_id),
        None,
    )
    if record is None:
        return JSONResponse({"error": "Decision could not be saved."}, status_code=503)
    load_result = getattr(call_result_storage, "latest_mission_result", None)
    update_draft = getattr(call_result_storage, "update_latest_email_draft", None)
    if callable(load_result) and callable(update_draft):
        try:
            latest_result = load_result(mission_id)
            draft = latest_result.get("email_draft") if isinstance(latest_result, dict) else None
            if isinstance(draft, dict):
                updated_draft = update_draft_approval_status(draft, updated)
                if updated_draft.get("approval_status") != draft.get("approval_status"):
                    update_draft(mission_id, updated_draft)
        except Exception:
            # Resolution remains a local Mission State record; no action is run.
            pass
    if status == "approved" and callable(load_result):
        try:
            latest_result = load_result(mission_id)
            state = updated
            draft = latest_result.get("email_draft") if isinstance(latest_result, dict) else None
            actions = state.get("actions", []) if isinstance(state, dict) else []
            action = next((item for item in actions if isinstance(item, dict)
                           and item.get("action_type") == "send_email"
                           and item.get("approval_decision_id") == decision_id), None)
            if action is not None:
                existing = latest_result.get("email_execution_results", {}) if isinstance(latest_result, dict) else {}
                email_result = execute_email_action(
                    mission_id, action.get("action_id"), state, draft,
                    latest_result, existing_results=existing,
                    provider=email_action_provider,
                )
                if (email_result.get("status") in {"dry_run", "sent"}
                        or email_result.get("reason_code") == "email_provider_failed"):
                    persist_execution = getattr(call_result_storage, "record_email_execution_result", None)
                    if callable(persist_execution):
                        persist_execution(mission_id, action["action_id"], email_result)
        except Exception:
            # Executor is local and fail-closed; never log message contents.
            pass
    if status == "approved" and callable(load_result):
        try:
            latest_result = load_result(mission_id)
            actions = updated.get("actions", []) if isinstance(updated, dict) else []
            action = next((item for item in actions if isinstance(item, dict)
                           and item.get("action_type") == "send_whatsapp"
                           and item.get("approval_decision_id") == decision_id), None)
            if action is not None and isinstance(latest_result, dict):
                existing = latest_result.get("whatsapp_execution_results", {})
                dry_run_result = execute_whatsapp_action(
                    mission_id, action.get("action_id"), updated, latest_result,
                    existing_results=existing,
                )
                if dry_run_result.get("status") == "dry_run":
                    persist_execution = getattr(
                        call_result_storage, "record_whatsapp_execution_result", None
                    )
                    if callable(persist_execution):
                        persist_execution(mission_id, action["action_id"], dry_run_result)
        except Exception:
            # No provider exists here; do not expose message or destination data.
            pass
    return JSONResponse({
        "mission_id": mission_id,
        "decision_id": decision_id,
        "status": record["status"],
        "resolution": record["resolution"],
        "resolved_by": record["resolved_by"],
        "resolved_at": record["resolved_at"],
        "mission_state": {
            "status": updated.get("status"),
            "needs_follow_up": updated.get("needs_follow_up") is True,
            "evidence_complete": updated.get("evidence_complete") is True,
            "decisions_pending": [
                value for value in updated.get("decisions_pending", [])
                if isinstance(value, str)
            ],
        },
    })


# ============================================================
# INCOMING CALL
# ============================================================

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):

    response = VoiceResponse()

    if not calls_are_enabled():
        response.say("Calls are currently unavailable.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    host = public_host(request)

    language = "spanish"
    try:
        mission = resolve_mission()
    except MissionConfigurationError:
        response.say("Calls are currently unavailable.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    connect = Connect()

    stream = connect.stream(
        url=f"wss://{host}/media-stream/{language}"
    )

    # Backup parameter
    stream.parameter(
        name="language",
        value=language
    )
    stream.parameter(name="mission", value=mission.id)

    response.append(connect)

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# MAKE OUTBOUND CALL
# ============================================================

@app.post("/make-call")
async def make_call(request: Request):
    if not call_is_authorized(request):
        status_code = 503 if not os.getenv("CALL_SECRET") else 401
        return JSONResponse(
            {"error": "Call authorization is unavailable." if status_code == 503 else "Unauthorized."},
            status_code=status_code,
        )

    try:
        safety_config = CallSafetyConfig.from_env()
    except SafetyConfigurationError:
        return JSONResponse(
            {"error": "Call safety configuration is invalid."}, status_code=503
        )
    if not safety_config.enabled:
        return JSONResponse({"error": "Outbound calls are disabled."}, status_code=503)

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse(
            {"error": "A JSON request body is required."},
            status_code=415,
        )

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"error": "The request body must contain valid JSON."},
            status_code=400,
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "The JSON body must be an object."},
            status_code=400,
        )

    try:
        to_number = normalize_destination(
            payload.get("to"), safety_config.allowed_prefixes
        )
    except CallAdmissionError as exc:
        return JSONResponse(
            {"error": "A valid destination is required."},
            status_code=400 if exc.code == "invalid_destination" else 403,
        )

    language = payload.get("language", "spanish")
    if not isinstance(language, str):
        language = "spanish"
    language = language.lower().strip()

    if language not in LANGUAGES:
        language = "spanish"

    requested_mission = payload.get("mission")
    if requested_mission is not None and not isinstance(requested_mission, str):
        return JSONResponse({"error": "Mission ID is invalid."}, status_code=400)
    try:
        mission = resolve_mission(requested_mission)
    except MissionNotFoundError:
        return JSONResponse({"error": "Mission ID is invalid."}, status_code=400)
    except MissionConfigurationError:
        return JSONResponse({"error": "Mission configuration is unavailable."}, status_code=503)

    account_sid = os.getenv(
        "TWILIO_ACCOUNT_SID"
    )

    auth_token = os.getenv(
        "TWILIO_AUTH_TOKEN"
    )

    from_number = os.getenv(
        "TWILIO_PHONE_NUMBER"
    )

    if not account_sid or not auth_token or not from_number:

        return JSONResponse(
            {
                "error":
                    "Missing Twilio environment variables."
            },
            status_code=500
        )

    try:
        record = call_manager.reserve(safety_config, language, mission)
    except CallAdmissionError as exc:
        messages = {
            "concurrent_limit": "The concurrent call limit has been reached.",
            "hourly_limit": "The hourly call limit has been reached.",
            "daily_limit": "The daily call limit has been reached.",
        }
        return JSONResponse({"error": messages[exc.code]}, status_code=429)

    availability = evaluate_call_availability()
    if not availability.allowed:
        availability_data = availability.to_dict()
        call_manager.release(reservation_id=record.reservation_id)
        blocked_result = empty_call_result(
            "blocked", language, call_started_datetime(), mission=mission
        )
        blocked_result["availability"] = availability_data
        await write_call_result(blocked_result, mission=mission)
        return JSONResponse(
            {
                "error": "Outbound calls are outside the configured operating window.",
                "availability": availability_data,
            },
            status_code=403 if availability.reason in {
                "outside_allowed_day", "outside_operating_hours"
            } else 503,
        )

    configured_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not configured_domain or not re.fullmatch(
        r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",
        configured_domain,
    ):
        call_manager.release(reservation_id=record.reservation_id)
        return JSONResponse(
            {"error": "Public call URL configuration is invalid or unavailable."}, status_code=503
        )

    try:
        host = public_host(request)
    except ValueError:
        call_manager.release(reservation_id=record.reservation_id)
        return JSONResponse(
            {"error": "Public call URL configuration is invalid."}, status_code=503
        )

    readiness_check = await check_provider_readiness()
    if readiness_check:
        call_manager.release(reservation_id=record.reservation_id)
        return readiness_response(readiness_check)

    try:
        from twilio.rest import Client

        client = Client(
            account_sid,
            auth_token,
            http_client=TwilioHttpClient(timeout=TWILIO_HTTP_TIMEOUT),
        )
        call = await run_in_threadpool(
            client.calls.create,
            to=to_number,
            from_=from_number,
            url=(
                f"https://{host}"
                f"/outbound-call?{urlencode({'language': language, 'mission': mission.id})}"
            ),
            status_callback=f"https://{host}/call-status",
            status_callback_method="POST",
            status_callback_event=["completed"],
        )
    except Timeout:
        # The provider may have accepted the request before the HTTP timeout.
        # Keep the concurrency lease until its bounded stale-call expiry.
        dependency_health.record(transport_error("twilio", "ambiguous_timeout"))
        await write_call_result(
            empty_call_result("error", language, record.started_at, mission=mission)
        )
        return JSONResponse(
            {"error": "The request to Twilio timed out."},
            status_code=504,
        )
    except TwilioRestException as exc:
        check = twilio_rest_error(
            getattr(exc, "code", None), getattr(exc, "status", None)
        )
        dependency_health.record(check)
        call_manager.release(reservation_id=record.reservation_id)
        await write_call_result(
            empty_call_result("rejected", language, record.started_at, mission=mission),
            reservation_id=record.reservation_id,
        )
        return JSONResponse({"error": "Twilio rejected the call request."}, status_code=502)
    except RequestException:
        dependency_health.record(transport_error("twilio", "network_error"))
        await write_call_result(
            empty_call_result("error", language, record.started_at, mission=mission)
        )
        return JSONResponse(
            {"error": "Could not communicate with Twilio."},
            status_code=502,
        )
    except Exception:
        await write_call_result(
            empty_call_result("error", language, record.started_at, mission=mission)
        )
        return JSONResponse(
            {"error": "Could not create the call."},
            status_code=502,
        )

    call_sid = getattr(call, "sid", None)
    if not isinstance(call_sid, str) or not call_sid:
        call_manager.release(reservation_id=record.reservation_id)
        await write_call_result(
            empty_call_result("error", language, record.started_at, mission=mission),
            reservation_id=record.reservation_id,
        )
        return JSONResponse({"error": "Could not create the call."}, status_code=502)
    call_manager.bind_call_sid(record.reservation_id, call_sid)
    if record.terminal_status:
        call_manager.release(reservation_id=record.reservation_id)
        terminal_result_status = (
            "completed"
            if record.terminal_status == "completed"
            else record.terminal_status
        )
        await write_call_result(
            empty_call_result(terminal_result_status, language, record.started_at, mission=mission),
            reservation_id=record.reservation_id,
        )

    print(f"Outbound call request accepted | LANGUAGE={language}")

    return JSONResponse(
        {
            "status": "call_created",
            "language": language,
            "language_name": LANGUAGES[language],
            "mission": mission.id,
        }
    )


@app.post("/call-status")
async def handle_call_status(request: Request):
    """Consume Twilio's signed terminal callback and free the concurrency slot."""
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    signature = request.headers.get("x-twilio-signature", "")
    if not auth_token or not signature:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)

    try:
        body = await request.body()
        if len(body) > 65536:
            return JSONResponse({"error": "Invalid callback."}, status_code=400)
        from urllib.parse import parse_qs
        from twilio.request_validator import RequestValidator

        values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        params = {key: items[-1] for key, items in values.items()}
        host = public_host(request)
        callback_url = f"https://{host}/call-status"
        if not RequestValidator(auth_token).validate(callback_url, params, signature):
            return JSONResponse({"error": "Unauthorized."}, status_code=401)
    except Exception:
        return JSONResponse({"error": "Invalid callback."}, status_code=400)

    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "").lower()
    terminal_statuses = {"completed", "busy", "failed", "no-answer", "canceled"}
    if not call_sid or call_status not in terminal_statuses:
        return JSONResponse({"status": "ignored"})

    record = call_manager.get_record(call_sid)
    if record and record.stream_started:
        call_manager.mark_terminal(call_sid, call_status)
    else:
        call_manager.release(call_sid=call_sid, terminal_status=call_status)
    if record and not record.stream_started:
        report_status = "completed" if call_status == "completed" else call_status
        callback_mission = None
        if record.mission_id:
            try:
                callback_mission = resolve_mission(record.mission_id)
            except (MissionConfigurationError, MissionNotFoundError):
                callback_mission = None
        await write_call_result(
            empty_call_result(
                report_status, record.language, record.started_at,
                mission=callback_mission,
            ),
            reservation_id=record.reservation_id,
            mission=callback_mission,
        )
    return JSONResponse({"status": "accepted"})


# ============================================================
# OUTBOUND CALL TWIML
# ============================================================

@app.api_route("/outbound-call", methods=["GET", "POST"])
async def handle_outbound_call(request: Request):

    if not calls_are_enabled():
        response = VoiceResponse()
        response.say("Calls are currently unavailable.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    language = request.query_params.get(
        "language",
        "spanish"
    ).lower().strip()

    if language not in LANGUAGES:
        language = "spanish"

    requested_mission = request.query_params.get("mission")
    try:
        mission = resolve_mission(requested_mission)
    except (MissionNotFoundError, MissionConfigurationError):
        response = VoiceResponse()
        response.say("This call configuration is unavailable.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    host = public_host(request)

    response = VoiceResponse()

    connect = Connect()

    # IMPORTANT:
    # No query string in the WebSocket URL.
    # The language is part of the path.
    stream = connect.stream(
        url=f"wss://{host}/media-stream/{language}"
    )

    # Backup custom parameter
    stream.parameter(
        name="language",
        value=language
    )
    stream.parameter(name="mission", value=mission.id)

    response.append(connect)

    print(
        f"OUTBOUND TWIML | "
        f"language={language} | "
        f"language_name={LANGUAGES[language]}"
    )

    print(
        f"WEBSOCKET URL | "
        f"wss://{host}/media-stream/{language}"
    )

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# MEDIA STREAM
# ============================================================

@app.websocket("/media-stream/{path_language}")
async def handle_media_stream(
    websocket: WebSocket,
    path_language: str
):

    await websocket.accept()
    call_started_at = asyncio.get_running_loop().time()
    call_started_datetime = datetime.now(timezone.utc)
    call_sid = None
    reservation_id = None
    mission = None
    stream_config = safety_config_or_none()
    openai_ws_for_report = None
    finalized = False
    transcript = CallTranscript()

    if not stream_config or not stream_config.enabled:
        print("Call stream rejected by a safety control.")
        await finish_call(None, None, websocket, complete_twilio=False)
        return

    # ========================================================
    # LANGUAGE FROM URL PATH
    # ========================================================

    language = path_language.lower().strip()

    if language not in LANGUAGES:
        language = "spanish"

    print(
        "=================================================="
    )

    print(
        f"MEDIA STREAM CONNECTED"
    )

    print(
        f"LANGUAGE FROM URL PATH: {language}"
    )

    print(
        f"LANGUAGE NAME: {LANGUAGES[language]}"
    )

    print(
        "=================================================="
    )

    # ========================================================
    # RECEIVE TWILIO START MESSAGE
    # ========================================================

    stream_sid = None

    try:

        remaining_duration = max(
            0,
            stream_config.max_duration_seconds
            - (asyncio.get_running_loop().time() - call_started_at),
        )
        first_message = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=remaining_duration,
        )

        first_data = json.loads(
            first_message
        )

        print(
            f"TWILIO FIRST EVENT: "
            f"{first_data.get('event')}"
        )

        if first_data.get("event") == "connected":

            remaining_duration = max(
                0,
                stream_config.max_duration_seconds
                - (asyncio.get_running_loop().time() - call_started_at),
            )
            start_message = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=remaining_duration,
            )

            start_data = json.loads(
                start_message
            )

        else:

            start_data = first_data

        # ----------------------------------------------------
        # START EVENT
        # ----------------------------------------------------

        if start_data.get("event") == "start":

            stream_sid = (
                start_data["start"]["streamSid"]
            )
            call_sid = start_data["start"].get("callSid")

            custom_parameters = (
                start_data["start"]
                .get(
                    "customParameters",
                    {}
                )
            )

            # ------------------------------------------------
            # If URL path is valid, it is the primary source.
            # Custom parameter is only a backup.
            # ------------------------------------------------

            parameter_language = (
                custom_parameters
                .get(
                    "language",
                    ""
                )
                .lower()
                .strip()
            )

            if (
                language not in LANGUAGES
                and parameter_language in LANGUAGES
            ):

                language = parameter_language

            try:
                mission = resolve_mission(
                    custom_parameters.get("mission") or None
                )
            except (MissionNotFoundError, MissionConfigurationError):
                print("Call stream rejected by mission configuration.")
                await finish_call(call_sid, None, websocket)
                await write_call_result(
                    empty_call_result("rejected", language, call_started_datetime)
                )
                return

            print(
                f"FINAL CALL LANGUAGE: "
                f"{language}"
            )

            print(
                f"FINAL LANGUAGE NAME: "
                f"{LANGUAGES[language]}"
            )

            try:
                if not call_sid:
                    raise CallAdmissionError("missing_call_sid")
                record = call_manager.admit_media_stream(
                    call_sid, stream_config, language, mission
                )
                reservation_id = record.reservation_id
                call_started_datetime = record.started_at
            except (CallAdmissionError, SafetyConfigurationError):
                print("Call stream rejected by a safety control.")
                await finish_call(call_sid, None, websocket)
                await write_call_result(
                    empty_call_result("rejected", language, call_started_datetime)
                )
                return

        else:

            print(
                "WARNING: Twilio start event "
                "was not received."
            )
            await finish_call(None, None, websocket, complete_twilio=False)
            return

    except Exception:

        print("Could not read the Twilio stream start event.")
        await finish_call(call_sid, None, websocket)
        await write_call_result(
            empty_call_result("error", language, call_started_datetime)
        )
        return

    # ========================================================
    # CONNECT TO OPENAI
    # ========================================================

    openai_check = await check_openai_readiness()
    if openai_check.status != DependencyStatus.AVAILABLE:
        await finalize_call(
            call_sid, reservation_id, None, websocket,
            "openai_dependency_error", language, call_started_datetime,
            asyncio.get_running_loop().time() - call_started_at,
            transcript.render(), mission,
        )
        finalized = True
        return

    openai_url = (
        "wss://api.openai.com/v1/realtime"
        "?model=gpt-realtime"
    )

    try:

        remaining_duration = max(
            0,
            stream_config.max_duration_seconds
            - (asyncio.get_running_loop().time() - call_started_at),
        )

        async with websockets.connect(
            openai_url,
            open_timeout=max(0.1, min(10, remaining_duration)),
            additional_headers={
                "Authorization":
                    f"Bearer {OPENAI_API_KEY}"
            }
        ) as openai_ws:

            openai_ws_for_report = openai_ws

            print(
                "OPENAI CONNECTED"
            )

            print(
                f"OPENAI LANGUAGE: {language}"
            )

            # ------------------------------------------------
            # Initialize OpenAI
            # ------------------------------------------------

            initialization_time_left = max(
                0,
                stream_config.max_duration_seconds
                - (asyncio.get_running_loop().time() - call_started_at),
            )
            await asyncio.wait_for(
                initialize_session(openai_ws, language, mission),
                timeout=initialization_time_left,
            )

            # =================================================
            # STATE
            # =================================================

            latest_media_timestamp = 0

            last_assistant_item = None

            mark_queue = []

            response_start_timestamp_twilio = None
            hold_detector = HoldDetector()

            # =================================================
            # RECEIVE FROM TWILIO
            # =================================================

            async def receive_from_twilio():

                nonlocal latest_media_timestamp
                nonlocal stream_sid

                try:

                    async for message in websocket.iter_text():

                        data = json.loads(
                            message
                        )

                        event_type = data.get(
                            "event"
                        )

                        # ------------------------------------
                        # MEDIA
                        # ------------------------------------

                        if event_type == "media":

                            latest_media_timestamp = int(
                                data["media"]["timestamp"]
                            )

                            if hold_detector.observe(
                                data["media"]["payload"],
                                latest_media_timestamp,
                            ):
                                print("Sustained hold pattern detected.")
                                return "hold_detected"

                            audio_append = {

                                "type":
                                    "input_audio_buffer.append",

                                "audio":
                                    data["media"]["payload"]
                            }

                            await openai_ws.send(
                                json.dumps(
                                    audio_append
                                )
                            )

                        # ------------------------------------
                        # MARK
                        # ------------------------------------

                        elif event_type == "mark":

                            if mark_queue:

                                mark_queue.pop(0)

                        # ------------------------------------
                        # STOP
                        # ------------------------------------

                        elif event_type == "stop":

                            print(
                                "TWILIO STREAM STOPPED"
                            )
                            return "twilio_stopped"

                except WebSocketDisconnect:

                    return "twilio_disconnected"
                except Exception:
                    print("Error receiving the Twilio media stream.")
                    return "connection_error"
                return "twilio_disconnected"

            # =================================================
            # SEND TO TWILIO
            # =================================================

            async def send_to_twilio():

                nonlocal last_assistant_item
                nonlocal response_start_timestamp_twilio

                try:

                    async for openai_message in openai_ws:

                        response = json.loads(
                            openai_message
                        )

                        response_type = response.get(
                            "type"
                        )

                        transcript.add_realtime_event(response)

                        if response_type in LOG_EVENT_TYPES:

                            print(
                                f"OpenAI event: "
                                f"{response_type}"
                            )

                        # ------------------------------------
                        # OPENAI ERROR
                        # ------------------------------------

                        if response_type == "error":
                            error_data = response.get("error", {})
                            check = openai_error(
                                error_data.get("code"),
                                error_data.get("type"),
                                error_data.get("status"),
                            )
                            dependency_health.record(check)
                            print("OpenAI Realtime error; ending dependent operation.")
                            return "openai_dependency_error"

                        # ------------------------------------
                        # AUDIO
                        # ------------------------------------

                        if (
                            response_type
                            == "response.output_audio.delta"
                            and "delta" in response
                        ):

                            audio_payload = (
                                base64.b64encode(
                                    base64.b64decode(
                                        response["delta"]
                                    )
                                ).decode(
                                    "utf-8"
                                )
                            )

                            audio_delta = {

                                "event":
                                    "media",

                                "streamSid":
                                    stream_sid,

                                "media": {

                                    "payload":
                                        audio_payload
                                }
                            }

                            await websocket.send_json(
                                audio_delta
                            )

                            if (
                                response.get(
                                    "item_id"
                                )
                                and response["item_id"]
                                != last_assistant_item
                            ):

                                response_start_timestamp_twilio = (
                                    latest_media_timestamp
                                )

                                last_assistant_item = (
                                    response["item_id"]
                                )

                            await send_mark(
                                websocket,
                                stream_sid,
                                mark_queue
                            )

                        # ------------------------------------
                        # SPEECH STARTED
                        # ------------------------------------

                        if (
                            response_type
                            == "input_audio_buffer.speech_started"
                        ):

                            if last_assistant_item:

                                await handle_speech_started_event()

                except Exception:
                    print("Error forwarding Realtime audio to Twilio.")
                    return "connection_error"
                return "realtime_disconnected"

            # =================================================
            # INTERRUPTION
            # =================================================

            async def handle_speech_started_event():

                nonlocal response_start_timestamp_twilio
                nonlocal last_assistant_item

                print(
                    "HANDLING INTERRUPTION"
                )

                if (
                    mark_queue
                    and response_start_timestamp_twilio
                    is not None
                ):

                    elapsed_time = (
                        latest_media_timestamp
                        - response_start_timestamp_twilio
                    )

                    if last_assistant_item:

                        truncate_event = {

                            "type":
                                "conversation.item.truncate",

                            "item_id":
                                last_assistant_item,

                            "content_index":
                                0,

                            "audio_end_ms":
                                elapsed_time
                        }

                        await openai_ws.send(
                            json.dumps(
                                truncate_event
                            )
                        )

                        await websocket.send_json(
                            {
                                "event":
                                    "clear",

                                "streamSid":
                                    stream_sid
                            }
                        )

                        mark_queue.clear()

                        last_assistant_item = None

                        response_start_timestamp_twilio = None

            # =================================================
            # RUN BOTH DIRECTIONS
            # =================================================

            receive_task = asyncio.create_task(receive_from_twilio())
            send_task = asyncio.create_task(send_to_twilio())
            remaining_duration = max(
                0,
                stream_config.max_duration_seconds
                - (asyncio.get_running_loop().time() - call_started_at),
            )
            end_reason = await wait_for_call_end(
                receive_task,
                send_task,
                remaining_duration,
            )

            if end_reason != "twilio_stopped":
                print(f"Ending call: {end_reason}.")
            await finalize_call(
                call_sid,
                reservation_id,
                None if end_reason == "openai_dependency_error" else openai_ws,
                websocket,
                end_reason,
                language,
                call_started_datetime,
                asyncio.get_running_loop().time() - call_started_at,
                transcript.render(),
                mission,
            )
            finalized = True

    except Exception:
        print("Realtime call stream failed.")
        if not finalized:
            await finalize_call(
                call_sid,
                reservation_id,
                openai_ws_for_report,
                websocket,
                "connection_error",
                language,
                call_started_datetime,
                asyncio.get_running_loop().time() - call_started_at,
                transcript.render(),
                mission,
            )


# ============================================================
# SEND MARK
# ============================================================

async def send_mark(
    connection,
    stream_sid,
    mark_queue
):

    if stream_sid:

        mark_event = {

            "event":
                "mark",

            "streamSid":
                stream_sid,

            "mark": {

                "name":
                    "responsePart"
            }
        }

        await connection.send_json(
            mark_event
        )

        mark_queue.append(
            "responsePart"
        )


# ============================================================
# INITIAL GREETING
# ============================================================

async def send_initial_conversation_item(
    openai_ws,
    language
):

    greeting = GREETINGS.get(
        language,
        GREETINGS["spanish"]
    )

    initial_conversation_item = {

        "type":
            "conversation.item.create",

        "item": {

            "type":
                "message",

            "role":
                "user",

            "content": [

                {
                    "type":
                        "input_text",

                    "text":
                        (
                            "Start the telephone call naturally. "
                            f"Speak in {LANGUAGES[language]}. "
                            "Use natural conversational delivery. "
                            "Say this greeting first: "
                            f"{greeting}"
                        )
                }
            ]
        }
    }

    await openai_ws.send(
        json.dumps(
            initial_conversation_item
        )
    )

    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {"output_modalities": ["audio"]},
            }
        )
    )


# ============================================================
# INITIALIZE OPENAI SESSION
# ============================================================

async def initialize_session(
    openai_ws,
    language,
    mission=None,
):

    mission = mission or resolve_mission()

    language_name = LANGUAGES.get(
        language,
        "español"
    )

    language_instructions = f"""

IMPORTANT LANGUAGE INSTRUCTION:

The selected language for this telephone call is:
{language_name}

You MUST speak in {language_name}.

Do not speak Spanish unless Spanish is the selected language.

Do not translate your response into another language.

Maintain {language_name} throughout the conversation unless
the caller explicitly requests another language.

Use natural conversational pronunciation and rhythm.
"""

    session_update = {

        "type":
            "session.update",

        "session": {

            "type":
                "realtime",

            "model":
                "gpt-realtime",

            "output_modalities":
                ["audio"],

            "audio": {

                "input": {

                    "format": {

                        "type":
                            "audio/pcmu"
                    },

                    "transcription": {
                        "model": "gpt-4o-mini-transcribe"
                    },

                    "turn_detection": {

                        "type":
                            "server_vad"
                    }
                },

                "output": {

                    "format": {

                        "type":
                            "audio/pcmu"
                    },

                    "voice":
                        VOICE
                }
            },

            "instructions":
                SYSTEM_MESSAGE
                + language_instructions
                + "\n\n"
                + mission.instructions()
        }
    }

    print(
        "=================================================="
    )

    print(
        "INITIALIZING OPENAI SESSION"
    )

    print(
        f"LANGUAGE: {language}"
    )

    print(
        f"LANGUAGE NAME: {language_name}"
    )

    print(
        f"VOICE: {VOICE}"
    )

    print(
        "=================================================="
    )

    await openai_ws.send(
        json.dumps(
            session_update
        )
    )

    # AI speaks first
    await send_initial_conversation_item(
        openai_ws,
        language
    )


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )

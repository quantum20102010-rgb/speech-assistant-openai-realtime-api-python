import os
import json
import base64
import asyncio
import hmac
import re
import websockets

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect

from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 5050))

VOICE = "cedar"


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

    return bool(supplied) and hmac.compare_digest(supplied, call_secret)

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
- Calls may involve establishing commercial relationships with suppliers,
  distributors, brands and companies.
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

if not OPENAI_API_KEY:
    raise ValueError("Missing the OpenAI API key.")


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


# ============================================================
# INCOMING CALL
# ============================================================

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):

    response = VoiceResponse()

    host = public_host(request)

    language = "spanish"

    connect = Connect()

    stream = connect.stream(
        url=f"wss://{host}/media-stream/{language}"
    )

    # Backup parameter
    stream.parameter(
        name="language",
        value=language
    )

    response.append(connect)

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# MAKE OUTBOUND CALL
# ============================================================

@app.api_route("/make-call", methods=["GET", "POST"])
async def make_call(request: Request):

    if not call_is_authorized(request):
        status_code = 503 if not os.getenv("CALL_SECRET") else 401
        return JSONResponse(
            {"error": "Call authorization is unavailable." if status_code == 503 else "Unauthorized."},
            status_code=status_code,
        )

    params = dict(request.query_params)

    to_number = params.get("to")
    language = params.get(
        "language",
        "spanish"
    ).lower().strip()

    if language not in LANGUAGES:
        language = "spanish"

    if not to_number:

        return JSONResponse(
            {
                "error": "Missing 'to' phone number."
            },
            status_code=400
        )

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

    from twilio.rest import Client

    client = Client(
        account_sid,
        auth_token
    )

    host = public_host(request)

    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=(
            f"https://{host}"
            f"/outbound-call?language={language}"
        ),
    )

    print(
        f"CALL CREATED | "
        f"SID={call.sid} | "
        f"TO={to_number} | "
        f"LANGUAGE={language}"
    )

    return JSONResponse(
        {
            "status": "call_created",
            "call_sid": call.sid,
            "to": to_number,
            "language": language,
            "language_name": LANGUAGES[language],
        }
    )


# ============================================================
# OUTBOUND CALL TWIML
# ============================================================

@app.api_route("/outbound-call", methods=["GET", "POST"])
async def handle_outbound_call(request: Request):

    language = request.query_params.get(
        "language",
        "spanish"
    ).lower().strip()

    if language not in LANGUAGES:
        language = "spanish"

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

        first_message = await websocket.receive_text()

        first_data = json.loads(
            first_message
        )

        print(
            f"TWILIO FIRST EVENT: "
            f"{first_data.get('event')}"
        )

        if first_data.get("event") == "connected":

            start_message = (
                await websocket.receive_text()
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

            custom_parameters = (
                start_data["start"]
                .get(
                    "customParameters",
                    {}
                )
            )

            print(
                "=================================================="
            )

            print(
                "TWILIO START DATA:"
            )

            print(
                json.dumps(
                    start_data,
                    indent=2
                )
            )

            print(
                f"TWILIO CUSTOM PARAMETERS: "
                f"{custom_parameters}"
            )

            print(
                f"LANGUAGE FROM URL PATH: "
                f"{language}"
            )

            print(
                "=================================================="
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

            print(
                f"FINAL CALL LANGUAGE: "
                f"{language}"
            )

            print(
                f"FINAL LANGUAGE NAME: "
                f"{LANGUAGES[language]}"
            )

        else:

            print(
                "WARNING: Twilio start event "
                "was not received."
            )

    except Exception as e:

        print(
            f"ERROR READING TWILIO START: {e}"
        )

        return

    # ========================================================
    # CONNECT TO OPENAI
    # ========================================================

    openai_url = (
        "wss://api.openai.com/v1/realtime"
        "?model=gpt-realtime"
    )

    try:

        async with websockets.connect(
            openai_url,
            additional_headers={
                "Authorization":
                    f"Bearer {OPENAI_API_KEY}"
            }
        ) as openai_ws:

            print(
                "OPENAI CONNECTED"
            )

            print(
                f"OPENAI LANGUAGE: {language}"
            )

            # ------------------------------------------------
            # Initialize OpenAI
            # ------------------------------------------------

            await initialize_session(
                openai_ws,
                language
            )

            # =================================================
            # STATE
            # =================================================

            latest_media_timestamp = 0

            last_assistant_item = None

            mark_queue = []

            response_start_timestamp_twilio = None

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

                            break

                except WebSocketDisconnect:

                    print(
                        "TWILIO DISCONNECTED"
                    )

                    try:
                        await openai_ws.close()
                    except Exception:
                        pass

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

                        if response_type in LOG_EVENT_TYPES:

                            print(
                                f"OpenAI event: "
                                f"{response_type}"
                            )

                        # ------------------------------------
                        # OPENAI ERROR
                        # ------------------------------------

                        if response_type == "error":

                            print(
                                "OPENAI ERROR DETAIL:"
                            )

                            print(
                                json.dumps(
                                    response,
                                    indent=2
                                )
                            )

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

                            print(
                                "CALLER STARTED SPEAKING"
                            )

                            if last_assistant_item:

                                await handle_speech_started_event()

                except Exception as e:

                    print(
                        f"ERROR SEND TO TWILIO: {e}"
                    )

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

            await asyncio.gather(
                receive_from_twilio(),
                send_to_twilio()
            )

    except Exception as e:

        print(
            "=================================================="
        )

        print(
            f"OPENAI/TWILIO CONNECTION ERROR: {e}"
        )

        print(
            "=================================================="
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
                "type":
                    "response.create"
            }
        )
    )


# ============================================================
# INITIALIZE OPENAI SESSION
# ============================================================

async def initialize_session(
    openai_ws,
    language
):

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

import os
import json
import base64
import asyncio
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
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.8))

VOICE = "cedar"

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
# SYSTEM MESSAGE
# ============================================================

SYSTEM_MESSAGE = """
You are an advanced AI voice assistant for Guzi Stuff.

You are speaking with people by telephone.

IMPORTANT LANGUAGE RULES:
- Always speak in the language selected for this call.
- If the selected language is Spanish, speak natural Mexican Spanish.
- If the selected language is English, speak natural fluent English.
- For any other selected language, speak naturally and fluently in that language.
- Never switch languages unless the caller clearly asks you to.
- Understand accents and imperfect pronunciation.
- If the caller speaks another language, adapt naturally when appropriate.

VOICE STYLE:
- Sound natural, human, warm and professional.
- Do not sound robotic.
- Keep responses concise and conversational.
- Use natural pauses.
- Do not give long speeches.
- Ask only one question at a time.
- Do not repeat information unnecessarily.
- Do not read a script mechanically.
- Adapt your responses to what the person actually says.
- If you do not understand something, politely ask the person to repeat it.

BUSINESS STYLE:
- You are calling on behalf of Guzi Stuff.
- Guzi Stuff is an e-commerce business based in Mexico.
- The purpose of calls may include establishing commercial relationships with suppliers, distributors, brands and companies.
- Be professional, friendly and direct.
- Do not invent prices, agreements, certifications, purchase volumes, legal entities or commercial conditions.
- If you do not know something, say so and ask for the appropriate contact.

CALL BEHAVIOR:
- Let the person finish speaking.
- Do not interrupt unnecessarily.
- Respond naturally to their answers.
- Keep the conversation moving.
- If the person gives you the name of another department or contact, acknowledge it and ask for the appropriate next step.
"""

# ============================================================
# GREETINGS
# ============================================================

GREETINGS = {
    "spanish": (
        "Hola, mucho gusto. Soy el asistente de voz de Guzi Stuff. "
        "¿Con quién tengo el gusto?"
    ),
    "english": (
        "Hello, nice to meet you. I'm the voice assistant calling on behalf "
        "of Guzi Stuff. Who am I speaking with?"
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
        "Hej, trevligt att träffas. Jag är röstassistenten från Guzi Stuff. "
        "Vem talar jag med?"
    ),
    "danish": (
        "Hej, rart at møde dig. Jeg er stemmeassistenten fra Guzi Stuff. "
        "Hvem taler jeg med?"
    ),
    "norwegian": (
        "Hei, hyggelig å møte deg. Jeg er taleassistenten fra Guzi Stuff. "
        "Hvem snakker jeg med?"
    ),
    "polish": (
        "Dzień dobry, miło mi. Jestem asystentem głosowym Guzi Stuff. "
        "Z kim mam przyjemność rozmawiać?"
    ),
    "turkish": (
        "Merhaba, tanıştığımıza memnun oldum. Ben Guzi Stuff'ın sesli "
        "asistanıyım. Kiminle görüşüyorum?"
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

SHOW_TIMING_MATH = False

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

    host = request.url.hostname

    connect = Connect()

    stream = connect.stream(
        url=f"wss://{host}/media-stream"
    )

    # IMPORTANT:
    # Twilio does not support query strings in <Stream>.
    # Language is therefore passed as a custom parameter.
    stream.parameter(
        name="language",
        value="spanish"
    )

    response.append(connect)

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# OUTBOUND CALL
# ============================================================

@app.api_route("/make-call", methods=["GET", "POST"])
async def make_call(request: Request):

    params = dict(request.query_params)

    to_number = params.get("to")
    language = params.get("language", "spanish").lower().strip()

    if language not in LANGUAGES:
        language = "spanish"

    if not to_number:
        return JSONResponse(
            {
                "error": "Missing 'to' phone number."
            },
            status_code=400
        )

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")

    if not account_sid or not auth_token or not from_number:
        return JSONResponse(
            {
                "error": "Missing Twilio environment variables."
            },
            status_code=500
        )

    from twilio.rest import Client

    client = Client(
        account_sid,
        auth_token
    )

    host = request.url.hostname

    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=f"https://{host}/outbound-call?language={language}",
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

    host = request.url.hostname

    response = VoiceResponse()

    connect = Connect()

    # IMPORTANT:
    # DO NOT put ?language=... on the WebSocket URL.
    # Twilio requires custom parameters instead.
    stream = connect.stream(
        url=f"wss://{host}/media-stream"
    )

    stream.parameter(
        name="language",
        value=language
    )

    response.append(connect)

    print(
        f"Outbound call TwiML | "
        f"language={language} | "
        f"language_name={LANGUAGES[language]}"
    )

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# MEDIA STREAM
# ============================================================

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):

    print("Twilio WebSocket connecting...")

    await websocket.accept()

    print("Twilio WebSocket accepted.")

    # ========================================================
    # FIRST: RECEIVE TWILIO START MESSAGE
    # ========================================================

    stream_sid = None
    language = "spanish"

    try:

        first_message = await websocket.receive_text()

        first_data = json.loads(first_message)

        print(
            f"First Twilio event: "
            f"{first_data.get('event')}"
        )

        # Twilio normally sends:
        # connected
        # start
        # media
        #
        # We need the start message before connecting
        # to OpenAI because the language lives there.

        if first_data.get("event") == "connected":

            start_message = await websocket.receive_text()

            start_data = json.loads(start_message)

        else:

            start_data = first_data

        if start_data.get("event") == "start":

            stream_sid = start_data["start"]["streamSid"]

            custom_parameters = (
                start_data["start"]
                .get("customParameters", {})
            )

            language = (
                custom_parameters
                .get("language", "spanish")
                .lower()
                .strip()
            )

            if language not in LANGUAGES:
                language = "spanish"

            print(
                f"CALL LANGUAGE: {language} "
                f"({LANGUAGES[language]})"
            )

            print(
                f"Stream SID: {stream_sid}"
            )

            print(
                f"Custom parameters: "
                f"{custom_parameters}"
            )

        else:

            print(
                "WARNING: Did not receive Twilio start event."
            )

    except Exception as e:

        print(
            f"Error reading initial Twilio messages: {e}"
        )

        return

    # ========================================================
    # CONNECT TO OPENAI
    # ========================================================

    openai_url = (
        "wss://api.openai.com/v1/realtime"
        "?model=gpt-realtime"
        f"&temperature={TEMPERATURE}"
    )

    try:

        async with websockets.connect(
            openai_url,
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}"
            }
        ) as openai_ws:

            print(
                f"OpenAI connected. "
                f"Language={language}"
            )

            await initialize_session(
                openai_ws,
                language
            )

            # ====================================================
            # CONNECTION STATE
            # ====================================================

            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None

            # ====================================================
            # RECEIVE FROM TWILIO
            # ====================================================

            async def receive_from_twilio():

                nonlocal latest_media_timestamp
                nonlocal stream_sid
                nonlocal response_start_timestamp_twilio
                nonlocal last_assistant_item

                try:

                    async for message in websocket.iter_text():

                        data = json.loads(message)

                        event_type = data.get("event")

                        # ----------------------------------------
                        # AUDIO FROM CALLER
                        # ----------------------------------------

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
                                json.dumps(audio_append)
                            )

                        # ----------------------------------------
                        # START
                        # ----------------------------------------

                        elif event_type == "start":

                            stream_sid = (
                                data["start"]["streamSid"]
                            )

                            print(
                                f"Stream started: "
                                f"{stream_sid}"
                            )

                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None

                        # ----------------------------------------
                        # MARK
                        # ----------------------------------------

                        elif event_type == "mark":

                            if mark_queue:
                                mark_queue.pop(0)

                        # ----------------------------------------
                        # STOP
                        # ----------------------------------------

                        elif event_type == "stop":

                            print(
                                "Twilio stream stopped."
                            )

                            break

                except WebSocketDisconnect:

                    print(
                        "Twilio client disconnected."
                    )

                    try:
                        await openai_ws.close()
                    except Exception:
                        pass

            # ====================================================
            # SEND TO TWILIO
            # ====================================================

            async def send_to_twilio():

                nonlocal stream_sid
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

                        # ----------------------------------------
                        # AUDIO RESPONSE
                        # ----------------------------------------

                        if (
                            response_type
                            == "response.output_audio.delta"
                            and "delta" in response
                        ):

                            audio_payload = base64.b64encode(
                                base64.b64decode(
                                    response["delta"]
                                )
                            ).decode("utf-8")

                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload":
                                        audio_payload
                                }
                            }

                            await websocket.send_json(
                                audio_delta
                            )

                            if (
                                response.get("item_id")
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

                        # ----------------------------------------
                        # CALLER STARTED SPEAKING
                        # ----------------------------------------

                        if (
                            response_type
                            == "input_audio_buffer.speech_started"
                        ):

                            print(
                                "Caller started speaking."
                            )

                            if last_assistant_item:

                                await handle_speech_started_event()

                except Exception as e:

                    print(
                        f"Error in send_to_twilio: {e}"
                    )

            # ====================================================
            # INTERRUPTION HANDLER
            # ====================================================

            async def handle_speech_started_event():

                nonlocal response_start_timestamp_twilio
                nonlocal last_assistant_item

                print(
                    "Handling speech interruption."
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

                    if SHOW_TIMING_MATH:

                        print(
                            "Elapsed time: "
                            f"{elapsed_time}ms"
                        )

                    if last_assistant_item:

                        truncate_event = {
                            "type":
                                "conversation.item.truncate",
                            "item_id":
                                last_assistant_item,
                            "content_index": 0,
                            "audio_end_ms":
                                elapsed_time
                        }

                        await openai_ws.send(
                            json.dumps(truncate_event)
                        )

                        await websocket.send_json(
                            {
                                "event": "clear",
                                "streamSid":
                                    stream_sid
                            }
                        )

                        mark_queue.clear()

                        last_assistant_item = None
                        response_start_timestamp_twilio = None

            # ====================================================
            # RUN BOTH DIRECTIONS
            # ====================================================

            await asyncio.gather(
                receive_from_twilio(),
                send_to_twilio()
            )

    except Exception as e:

        print(
            f"OpenAI/Twilio connection error: {e}"
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
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {
                "name": "responsePart"
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
                            "Start the call naturally. "
                            "Speak in the selected language. "
                            "Say exactly this greeting, "
                            "with natural conversational delivery: "
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
                "type": "response.create"
            }
        )
    )


# ============================================================
# OPENAI SESSION
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

IMPORTANT:
The selected language for this call is:
{language_name}

The caller must hear you speaking in:
{language_name}

Do not default to Spanish unless Spanish is the selected language.

Speak naturally and conversationally in the selected language.
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
                        "type": "audio/pcmu"
                    },

                    "turn_detection": {
                        "type": "server_vad"
                    }
                },

                "output": {

                    "format": {
                        "type": "audio/pcmu"
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
        f"Initializing OpenAI session "
        f"with language={language}"
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
        port=PORT
    )

import os
import json
import base64
import asyncio
import websockets

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect

from dotenv import load_dotenv


load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 5050))
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.8))

# Twilio credentials - these will be stored ONLY in Railway
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Private secret used to authorize outbound-call requests
CALL_SECRET = os.getenv("CALL_SECRET")


SYSTEM_MESSAGE = (
    "Hablas en español de México y realizas llamadas comerciales en nombre de "
    "Fabián Guzmán Bravo, propietario y Director General de Guzi Stuff. "
    "Guzi Stuff es el nombre comercial del negocio de Fabián; no lo describas como "
    "una sociedad o persona moral. "

    "Tu objetivo principal es establecer contacto con el área comercial, ventas "
    "mayoristas o distribución de proveedores y marcas en México. "

    "Al iniciar una llamada, di de forma natural: "
    "'Buenos días. Mi nombre es Fabián Guzmán Bravo y le llamo de Guzi Stuff. "
    "Estoy interesado en establecer una relación comercial con ustedes para la "
    "distribución de sus productos en México, principalmente a través de Amazon "
    "México, Mercado Libre y Walmart. Quisiera saber con quién puedo comunicarme "
    "del área comercial o de distribución.' "

    "Si preguntan qué es Guzi Stuff, explica: "
    "'Guzi Stuff es mi negocio de comercialización y distribución de productos "
    "en marketplaces en México. Actualmente estoy buscando incorporar nuevas "
    "marcas y proveedores con los que pueda trabajar directamente.' "

    "Tu objetivo es obtener, cuando sea posible, la siguiente información: "
    "nombre de la persona responsable, correo electrónico, teléfono o extensión, "
    "requisitos para abrir una cuenta comercial, catálogo, precios mayoristas, "
    "mínimos de compra, descuentos por volumen, condiciones de pago, disponibilidad "
    "de productos y políticas o autorizaciones para vender en Amazon México, "
    "Mercado Libre y Walmart México. "

    "Si la persona responsable no está disponible, solicita amablemente su nombre, "
    "correo electrónico, teléfono o extensión para poder darle seguimiento. "

    "No inventes información. No proporciones el RFC de Fabián salvo que te lo "
    "soliciten expresamente como parte de un proceso formal de alta comercial. "

    "Habla de manera profesional, cordial, breve y natural. No leas una lista "
    "de preguntas de manera mecánica; adapta la conversación a las respuestas "
    "de la persona. No hagas afirmaciones que no conozcas. "

    "Si te preguntan directamente si eres Fabián Guzmán Bravo, responde con "
    "transparencia que eres un asistente de voz que realiza la llamada en su "
    "nombre. No afirmes ser una persona humana. "

    "La finalidad de la llamada es conseguir el contacto adecuado y la información "
    "necesaria para continuar la relación comercial por correo electrónico. "
)


VOICE = "alloy"

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


# ============================================================
# BASIC VALIDATION
# ============================================================

if not OPENAI_API_KEY:
    raise ValueError(
        "Missing the OpenAI API key. Please set OPENAI_API_KEY."
    )

if not TWILIO_ACCOUNT_SID:
    raise ValueError(
        "Missing TWILIO_ACCOUNT_SID."
    )

if not TWILIO_AUTH_TOKEN:
    raise ValueError(
        "Missing TWILIO_AUTH_TOKEN."
    )

if not TWILIO_PHONE_NUMBER:
    raise ValueError(
        "Missing TWILIO_PHONE_NUMBER."
    )

if not CALL_SECRET:
    raise ValueError(
        "Missing CALL_SECRET."
    )


# Twilio REST client
twilio_client = Client(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN
)


# ============================================================
# HOME
# ============================================================

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {
        "message": "Guzi Stuff AI Voice Assistant is running."
    }


# ============================================================
# INCOMING CALL
# ============================================================

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """
    Handle an incoming call and connect it to the OpenAI Realtime API.
    """

    response = VoiceResponse()

    response.say(
        "Espere un momento mientras conecto su llamada con el asistente "
        "de voz de Guzi Stuff.",
        voice="Google.en-US-Chirp3-HD-Aoede"
    )

    response.pause(length=1)

    response.say(
        "Ya puede comenzar a hablar.",
        voice="Google.en-US-Chirp3-HD-Aoede"
    )

    host = request.url.hostname

    connect = Connect()

    connect.stream(
        url=f"wss://{host}/media-stream"
    )

    response.append(connect)

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# OUTBOUND CALL - TWIML
# ============================================================

@app.api_route("/outbound-call", methods=["GET", "POST"])
async def handle_outbound_call(request: Request):
    """
    TwiML returned to Twilio after an outbound call is answered.
    """

    response = VoiceResponse()

    response.say(
        "Buenos días. Mi nombre es Fabián Guzmán Bravo y le llamo "
        "de Guzi Stuff. Estoy interesado en establecer una relación "
        "comercial con ustedes.",
        voice="Google.en-US-Chirp3-HD-Aoede"
    )

    response.pause(length=1)

    host = request.url.hostname

    connect = Connect()

    connect.stream(
        url=f"wss://{host}/media-stream"
    )

    response.append(connect)

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# START OUTBOUND CALL
# ============================================================

@app.get("/make-call", response_class=JSONResponse)
async def make_outbound_call(
    request: Request,
    to: str,
    key: str
):
    """
    Start an outbound phone call.

    Example:
    /make-call?to=%2B525574986500&key=YOUR_SECRET
    """

    # Security check
    if key != CALL_SECRET:
        return JSONResponse(
            status_code=403,
            content={
                "error": "Unauthorized"
            }
        )

    # Basic phone number validation
    if not to.startswith("+"):
        return JSONResponse(
            status_code=400,
            content={
                "error": "Phone number must use E.164 format, e.g. +525574986500"
            }
        )

    host = request.url.hostname

    twiml_url = f"https://{host}/outbound-call"

    try:

        call = twilio_client.calls.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,
        )

        return {
            "status": "call_started",
            "call_sid": call.sid,
            "to": to,
            "from": TWILIO_PHONE_NUMBER,
        }

    except Exception as e:

        print(f"Error starting outbound call: {e}")

        return JSONResponse(
            status_code=500,
            content={
                "error": str(e)
            }
        )


# ============================================================
# MEDIA STREAM
# ============================================================

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    Handle the WebSocket connection between Twilio and OpenAI.
    """

    print("Client connected")

    await websocket.accept()

    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime"
        f"?model=gpt-realtime"
        f"&temperature={TEMPERATURE}",
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
    ) as openai_ws:

        await initialize_session(openai_ws)

        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        # ----------------------------------------------------
        # RECEIVE FROM TWILIO
        # ----------------------------------------------------

        async def receive_from_twilio():

            nonlocal stream_sid
            nonlocal latest_media_timestamp

            try:

                async for message in websocket.iter_text():

                    data = json.loads(message)

                    if (
                        data["event"] == "media"
                        and openai_ws.state.name == "OPEN"
                    ):

                        latest_media_timestamp = int(
                            data["media"]["timestamp"]
                        )

                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"]
                        }

                        await openai_ws.send(
                            json.dumps(audio_append)
                        )

                    elif data["event"] == "start":

                        stream_sid = data["start"]["streamSid"]

                        print(
                            f"Stream started: {stream_sid}"
                        )

                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None

                    elif data["event"] == "mark":

                        if mark_queue:
                            mark_queue.pop(0)

            except WebSocketDisconnect:

                print("Client disconnected.")

                if openai_ws.state.name == "OPEN":
                    await openai_ws.close()


        # ----------------------------------------------------
        # SEND TO TWILIO
        # ----------------------------------------------------

        async def send_to_twilio():

            nonlocal stream_sid
            nonlocal last_assistant_item
            nonlocal response_start_timestamp_twilio

            try:

                async for openai_message in openai_ws:

                    response = json.loads(openai_message)

                    if response.get("type") in LOG_EVENT_TYPES:

                        print(
                            f"Received event: {response.get('type')}"
                        )

                    # Audio from OpenAI
                    if (
                        response.get("type")
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
                                "payload": audio_payload
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
                            stream_sid
                        )

                    # Detect caller interruption
                    if (
                        response.get("type")
                        == "input_audio_buffer.speech_started"
                    ):

                        print(
                            "Speech started detected."
                        )

                        if last_assistant_item:

                            await handle_speech_started_event()

            except Exception as e:

                print(
                    f"Error in send_to_twilio: {e}"
                )


        # ----------------------------------------------------
        # HANDLE INTERRUPTION
        # ----------------------------------------------------

        async def handle_speech_started_event():

            nonlocal response_start_timestamp_twilio
            nonlocal last_assistant_item

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
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }

                    await openai_ws.send(
                        json.dumps(truncate_event)
                    )

                await websocket.send_json(
                    {
                        "event": "clear",
                        "streamSid": stream_sid
                    }
                )

                mark_queue.clear()

                last_assistant_item = None

                response_start_timestamp_twilio = None


        # ----------------------------------------------------
        # SEND MARK
        # ----------------------------------------------------

        async def send_mark(connection, stream_sid):

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


        # Run both directions simultaneously
        await asyncio.gather(
            receive_from_twilio(),
            send_to_twilio()
        )


# ============================================================
# INITIAL CONVERSATION
# ============================================================

async def send_initial_conversation_item(openai_ws):

    initial_conversation_item = {

        "type": "conversation.item.create",

        "item": {

            "type": "message",

            "role": "user",

            "content": [

                {
                    "type": "input_text",

                    "text": (
                        "Inicia la conversación de manera natural. "
                        "Preséntate como asistente de voz de Fabián "
                        "Guzmán Bravo y explica brevemente que llamas "
                        "de Guzi Stuff para establecer contacto con "
                        "el área comercial."
                    )
                }

            ]
        }
    }

    await openai_ws.send(
        json.dumps(initial_conversation_item)
    )

    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create"
            }
        )
    )


# ============================================================
# OPENAI REALTIME SESSION
# ============================================================

async def initialize_session(openai_ws):

    session_update = {

        "type": "session.update",

        "session": {

            "type": "realtime",

            "model": "gpt-realtime",

            "output_modalities": [
                "audio"
            ],

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

                    "voice": VOICE
                }
            },

            "instructions": SYSTEM_MESSAGE
        }
    }

    print(
        "Sending session update"
    )

    await openai_ws.send(
        json.dumps(session_update)
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

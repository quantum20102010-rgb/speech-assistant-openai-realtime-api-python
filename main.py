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
# CONFIGURACIÓN
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 5050))
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.8))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
CALL_SECRET = os.getenv("CALL_SECRET")


# ============================================================
# PERSONALIDAD Y OBJETIVO DEL AGENTE
# ============================================================

SYSTEM_MESSAGE = (
    "Hablas en español de México, con una voz cálida, profesional, natural "
    "y conversacional. Tu forma de hablar debe sonar como una persona mexicana "
    "en una llamada comercial, sin sonar robótica, exagerada ni demasiado formal. "

    "Realizas llamadas comerciales en nombre de Fabián Guzmán Bravo, "
    "propietario y Director General de Guzi Stuff. "
    "Guzi Stuff es el nombre comercial del negocio de Fabián; "
    "no lo describas como una sociedad o persona moral. "

    "El objetivo de la llamada es establecer contacto con el área comercial, "
    "ventas mayoristas o distribución de marcas y proveedores en México. "

    "Al comenzar la llamada, preséntate de manera breve y natural. "
    "Puedes decir algo como: "
    "'Buenos días, ¿con quién tengo el gusto? Mi nombre es Fabián Guzmán Bravo "
    "y le llamo de Guzi Stuff. Estoy buscando establecer relaciones comerciales "
    "con marcas y proveedores en México. ¿Me podría comunicar con la persona "
    "encargada del área comercial o distribución?' "

    "Después del saludo, escucha a la persona y responde de acuerdo con lo que diga. "
    "No hagas un monólogo ni leas una lista de preguntas. "

    "Si preguntan qué es Guzi Stuff, explica que es un negocio de comercialización "
    "y distribución de productos en marketplaces en México, principalmente "
    "Amazon México, Mercado Libre y Walmart México. "

    "El objetivo principal es obtener, cuando sea posible, el nombre de la persona "
    "responsable, correo electrónico, teléfono o extensión, requisitos para abrir "
    "una cuenta comercial, catálogo, precios mayoristas, mínimos de compra, "
    "descuentos por volumen, condiciones de pago, disponibilidad y políticas "
    "o autorizaciones para vender en Amazon México, Mercado Libre y Walmart México. "

    "Si la persona responsable no está disponible, solicita amablemente su nombre, "
    "correo electrónico, teléfono o extensión para poder darle seguimiento. "

    "No inventes información. No proporciones el RFC de Fabián salvo que te lo "
    "soliciten expresamente como parte de un proceso formal de alta comercial. "

    "Sé breve. Deja que la otra persona hable. Haz una pregunta a la vez. "
    "Mantén un tono cordial y profesional. "

    "Si te preguntan directamente si eres Fabián Guzmán Bravo, responde con "
    "transparencia que eres un asistente de voz que realiza la llamada en su nombre. "
    "No afirmes ser una persona humana."
)


# Voz de OpenAI
VOICE = "marin"


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
# VALIDACIONES
# ============================================================

if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY")

if not TWILIO_ACCOUNT_SID:
    raise ValueError("Missing TWILIO_ACCOUNT_SID")

if not TWILIO_AUTH_TOKEN:
    raise ValueError("Missing TWILIO_AUTH_TOKEN")

if not TWILIO_PHONE_NUMBER:
    raise ValueError("Missing TWILIO_PHONE_NUMBER")

if not CALL_SECRET:
    raise ValueError("Missing CALL_SECRET")


twilio_client = Client(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN
)


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {
        "message": "Guzi Stuff AI Voice Assistant is running."
    }


# ============================================================
# LLAMADAS ENTRANTES
# ============================================================

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):

    response = VoiceResponse()

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
# TWIML PARA LLAMADA SALIENTE
# ============================================================

@app.api_route("/outbound-call", methods=["GET", "POST"])
async def outbound_call(request: Request):

    response = VoiceResponse()

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
# INICIAR LLAMADA SALIENTE
# ============================================================

@app.get("/make-call")
async def make_call(
    request: Request,
    to: str,
    key: str
):

    if key != CALL_SECRET:
        return JSONResponse(
            status_code=403,
            content={"error": "Invalid key"}
        )

    host = request.url.hostname

    twiml_url = (
        f"https://{host}/outbound-call"
    )

    try:

        call = twilio_client.calls.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url
        )

        return {
            "status": "call_started",
            "call_sid": call.sid,
            "to": to,
            "from": TWILIO_PHONE_NUMBER
        }

    except Exception as e:

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

    print("Client connected")

    await websocket.accept()

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime"
        "?model=gpt-realtime"
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


        # ====================================================
        # RECIBIR AUDIO DE TWILIO
        # ====================================================

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
                            f"Incoming stream started: {stream_sid}"
                        )

                        latest_media_timestamp = 0

                        mark_queue.clear()

                    elif data["event"] == "mark":

                        if mark_queue:
                            mark_queue.pop(0)

            except WebSocketDisconnect:

                print("Client disconnected.")

                if openai_ws.state.name == "OPEN":
                    await openai_ws.close()


        # ====================================================
        # ENVIAR AUDIO DE OPENAI A TWILIO
        # ====================================================

        async def send_to_twilio():

            nonlocal stream_sid
            nonlocal last_assistant_item
            nonlocal response_start_timestamp_twilio

            try:

                async for openai_message in openai_ws:

                    response = json.loads(openai_message)

                    if response["type"] in LOG_EVENT_TYPES:

                        print(
                            f"Received event: {response['type']}"
                        )

                    # ----------------------------------------
                    # AUDIO GENERADO POR OPENAI
                    # ----------------------------------------

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

                    # ----------------------------------------
                    # NUEVA RESPUESTA DEL ASISTENTE
                    # ----------------------------------------

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

                    # ----------------------------------------
                    # INTERRUPCIÓN
                    # ----------------------------------------

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


        # ====================================================
        # MANEJAR INTERRUPCIÓN DEL USUARIO
        # ====================================================

        async def handle_speech_started_event():

            nonlocal response_start_timestamp_twilio
            nonlocal last_assistant_item

            print(
                "Handling speech started event."
            )

            if (
                mark_queue
                and response_start_timestamp_twilio is not None
            ):

                elapsed_time = (
                    latest_media_timestamp
                    - response_start_timestamp_twilio
                )

                if SHOW_TIMING_MATH:

                    print(
                        "Elapsed time:",
                        elapsed_time
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


        # ====================================================
        # MARCA DE AUDIO
        # ====================================================

        async def send_mark(
            connection,
            stream_sid
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


        # ====================================================
        # CONVERSACIÓN INICIAL
        # ====================================================

        await send_initial_conversation_item(
            openai_ws
        )


        # ====================================================
        # EJECUTAR LAS DOS DIRECCIONES DE AUDIO
        # ====================================================

        await asyncio.gather(
            receive_from_twilio(),
            send_to_twilio()
        )


# ============================================================
# HACER QUE OPENAI HABLE PRIMERO
# ============================================================

async def send_initial_conversation_item(
    openai_ws
):

    initial_conversation_item = {

        "type": "conversation.item.create",

        "item": {

            "type": "message",

            "role": "user",

            "content": [

                {
                    "type": "input_text",

                    "text": (
                        "La llamada acaba de comenzar. "
                        "Saluda de manera breve, natural y profesional. "
                        "Preséntate como Fabián Guzmán Bravo de Guzi Stuff "
                        "y pregunta con quién tienes el gusto y si te puede "
                        "comunicar con la persona encargada del área comercial "
                        "o distribución."
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
# CONFIGURAR SESIÓN OPENAI
# ============================================================

async def initialize_session(
    openai_ws
):

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
        "Sending session update:",
        json.dumps(session_update)
    )

    await openai_ws.send(
        json.dumps(session_update)
    )


# ============================================================
# ARRANQUE
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )

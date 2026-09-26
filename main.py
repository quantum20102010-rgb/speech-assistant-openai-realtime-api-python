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

VOICE = "cedar"


# ============================================================
# IDIOMAS DISPONIBLES
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
    "chinese": "mandarín",
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
# INSTRUCCIONES BASE DEL AGENTE
# ============================================================

BASE_SYSTEM_MESSAGE = """
Realizas llamadas comerciales profesionales en nombre de
Fabián Guzmán Bravo, propietario y Director General de Guzi Stuff.

Guzi Stuff es el nombre comercial del negocio de Fabián.
No describas Guzi Stuff como una sociedad, empresa constituida
o persona moral.

El objetivo principal de la llamada es establecer contacto con
el área comercial, ventas mayoristas, distribución o compras
de marcas y proveedores.

Tu objetivo es obtener, cuando sea posible:

- nombre de la persona responsable;
- correo electrónico;
- teléfono o extensión;
- requisitos para abrir una cuenta comercial;
- catálogo;
- precios mayoristas;
- mínimos de compra;
- descuentos por volumen;
- condiciones de pago;
- disponibilidad;
- políticas de distribución;
- autorización o políticas para vender en marketplaces.

Fabián comercializa principalmente mediante Amazon, Mercado Libre
y Walmart, y está interesado en establecer relaciones comerciales
directas con marcas y proveedores.

Habla de manera natural, cordial, profesional y breve.

No leas una lista de preguntas de manera mecánica.

Haz una pregunta a la vez.

Escucha cuidadosamente la respuesta antes de continuar.

No interrumpas innecesariamente a la persona.

Permite pausas naturales.

No hagas monólogos largos.

Adapta la conversación a lo que diga la persona.

No inventes información.

No proporciones el RFC de Fabián salvo que te lo soliciten
expresamente como parte de un proceso formal de alta comercial.

Si la persona responsable no está disponible, solicita amablemente
su nombre, correo electrónico, teléfono o extensión para poder
dar seguimiento.

Si preguntan directamente si eres Fabián Guzmán Bravo, responde
con transparencia que eres un asistente de voz que realiza la
llamada en su nombre.

No afirmes ser una persona humana.

La finalidad de la llamada es conseguir el contacto adecuado y
la información necesaria para continuar la relación comercial
por correo electrónico.
"""


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


app = FastAPI()


# ============================================================
# CREAR INSTRUCCIONES SEGÚN IDIOMA
# ============================================================

def build_system_message(language_code: str) -> str:

    language_code = (
        language_code or "spanish"
    ).lower().strip()

    language_name = LANGUAGES.get(
        language_code,
        "español"
    )

    language_instructions = f"""

IDIOMA DE ESTA LLAMADA

El idioma objetivo de esta llamada es {language_name}.

Desde el primer saludo debes hablar en {language_name}.

Mantén toda la conversación en {language_name}.

No cambies de idioma por nombres propios, marcas,
palabras aisladas o acentos.

Si la persona cambia claramente a otro idioma y continúa
hablando en ese idioma, puedes adaptarte al nuevo idioma.

Si existe duda sobre el idioma, conserva {language_name}.

No traduzcas mentalmente la conversación para la persona.
Responde directamente en el idioma correspondiente.

La pronunciación debe ser clara y natural.

"""

    return BASE_SYSTEM_MESSAGE + language_instructions


# ============================================================
# SALUDO INICIAL SEGÚN IDIOMA
# ============================================================

def get_initial_instruction(language_code: str) -> str:

    language_code = (
        language_code or "spanish"
    ).lower().strip()

    greetings = {

        "spanish": (
            "La llamada acaba de comenzar. "
            "Saluda brevemente en español. "
            "Di: 'Buenos días, ¿con quién tengo el gusto? "
            "Mi nombre es Fabián Guzmán Bravo y le llamo de Guzi Stuff. "
            "Estoy buscando establecer relaciones comerciales con marcas "
            "y proveedores. ¿Me podría comunicar con la persona encargada "
            "del área comercial o distribución?' "
            "Después escucha y continúa la conversación naturalmente."
        ),

        "english": (
            "The call has just started. "
            "Greet the person briefly and naturally in English. "
            "Introduce yourself as Fabián Guzmán Bravo calling from Guzi Stuff "
            "and ask who you are speaking with and whether they can connect "
            "you with the person responsible for commercial sales or distribution. "
            "Then listen and continue the conversation naturally."
        ),

        "french": (
            "L'appel vient de commencer. "
            "Saluez brièvement et naturellement en français. "
            "Présentez-vous comme Fabián Guzmán Bravo de Guzi Stuff "
            "et demandez avec qui vous avez le plaisir de parler et "
            "si cette personne peut vous mettre en relation avec le "
            "responsable commercial ou de la distribution. "
            "Écoutez ensuite et poursuivez naturellement."
        ),

        "german": (
            "Das Gespräch hat gerade begonnen. "
            "Begrüße die Person kurz und natürlich auf Deutsch. "
            "Stelle dich als Fabián Guzmán Bravo von Guzi Stuff vor "
            "und frage, mit wem du sprichst und ob die Person dich mit "
            "dem zuständigen Ansprechpartner für Vertrieb oder Distribution "
            "verbinden kann. Höre anschließend aufmerksam zu."
        ),

        "italian": (
            "La chiamata è appena iniziata. "
            "Saluta brevemente e naturalmente in italiano. "
            "Presentati come Fabián Guzmán Bravo di Guzi Stuff "
            "e chiedi con chi stai parlando e se può metterti in contatto "
            "con la persona responsabile dell'area commerciale o della distribuzione. "
            "Poi ascolta e continua naturalmente."
        ),

        "portuguese": (
            "A chamada acabou de começar. "
            "Cumprimente a pessoa de forma breve e natural em português. "
            "Apresente-se como Fabián Guzmán Bravo da Guzi Stuff "
            "e pergunte com quem está falando e se pode encaminhá-lo "
            "para a pessoa responsável pela área comercial ou distribuição. "
            "Depois escute e continue a conversa naturalmente."
        ),

        "japanese": (
            "電話が始まったばかりです。 "
            "日本語で簡潔かつ自然に挨拶してください。 "
            "Guzi StuffのFabián Guzmán Bravoと名乗り、 "
            "誰と話しているのか確認し、営業または流通担当者に "
            "つないでもらえるか丁寧に尋ねてください。 "
            "その後は相手の話をよく聞いて自然に会話を続けてください。"
        ),

        "mandarin": (
            "电话刚刚接通。 "
            "请用普通话自然、简洁地打招呼。 "
            "介绍自己是Guzi Stuff的Fabián Guzmán Bravo， "
            "询问对方怎么称呼，并礼貌地询问是否可以转接负责商业合作、 "
            "销售或分销的人员。然后认真倾听并自然地继续对话。"
        ),

        "chinese": (
            "电话刚刚接通。 "
            "请用普通话自然、简洁地打招呼。 "
            "介绍自己是Guzi Stuff的Fabián Guzmán Bravo， "
            "询问对方怎么称呼，并礼貌地询问是否可以转接负责商业合作、 "
            "销售或分销的人员。然后认真倾听并自然地继续对话。"
        ),

        "korean": (
            "통화가 방금 연결되었습니다. "
            "한국어로 자연스럽고 간단하게 인사하세요. "
            "Guzi Stuff의 Fabián Guzmán Bravo라고 소개하고 "
            "통화 상대방의 이름을 확인한 후 영업 또는 유통 담당자와 "
            "연결할 수 있는지 정중하게 물어보세요. "
            "그 후 상대방의 말을 듣고 자연스럽게 대화를 이어가세요."
        ),
    }

    return greetings.get(
        language_code,
        greetings["english"]
    )


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================

@app.get("/", response_class=JSONResponse)
async def index_page():

    return {
        "message": "Guzi Stuff AI Voice Assistant is running.",
        "voice": VOICE,
        "languages": list(LANGUAGES.keys())
    }


# ============================================================
# LLAMADA ENTRANTE
# ============================================================

@app.api_route(
    "/incoming-call",
    methods=["GET", "POST"]
)
async def handle_incoming_call(
    request: Request
):

    response = VoiceResponse()

    host = request.url.hostname

    connect = Connect()

    connect.stream(
        url=f"wss://{host}/media-stream?language=spanish"
    )

    response.append(connect)

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# TWIML DE LLAMADA SALIENTE
# ============================================================

@app.api_route(
    "/outbound-call",
    methods=["GET", "POST"]
)
async def outbound_call(
    request: Request
):

    language = (
        request.query_params.get(
            "language",
            "spanish"
        )
        .lower()
        .strip()
    )

    if language not in LANGUAGES:

        language = "spanish"

    response = VoiceResponse()

    host = request.url.hostname

    connect = Connect()

    connect.stream(
        url=(
            f"wss://{host}/media-stream"
            f"?language={language}"
        )
    )

    response.append(connect)

    return HTMLResponse(
        content=str(response),
        media_type="application/xml"
    )


# ============================================================
# INICIAR LLAMADA
# ============================================================

@app.get("/make-call")
async def make_call(
    request: Request,
    to: str,
    key: str,
    language: str = "spanish"
):

    if key != CALL_SECRET:

        return JSONResponse(
            status_code=403,
            content={
                "error": "Invalid key"
            }
        )

    language = (
        language.lower().strip()
    )

    if language not in LANGUAGES:

        return JSONResponse(
            status_code=400,
            content={
                "error": "Unsupported language",
                "available_languages": list(
                    LANGUAGES.keys()
                )
            }
        )

    host = request.url.hostname

    twiml_url = (
        f"https://{host}"
        f"/outbound-call"
        f"?language={language}"
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
            "from": TWILIO_PHONE_NUMBER,
            "language": language,
            "language_name": LANGUAGES[language]
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
async def handle_media_stream(
    websocket: WebSocket
):

    print("Client connected")

    await websocket.accept()

    language = (
        websocket.query_params.get(
            "language",
            "spanish"
        )
        .lower()
        .strip()
    )

    if language not in LANGUAGES:

        language = "spanish"

    print(
        f"Call language: {language}"
    )

    system_message = build_system_message(
        language
    )

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime"
        "?model=gpt-realtime",
        additional_headers={
            "Authorization": (
                f"Bearer {OPENAI_API_KEY}"
            )
        }
    ) as openai_ws:

        await initialize_session(
            openai_ws,
            system_message
        )

        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None

        mark_queue = []

        response_start_timestamp_twilio = None


        # ====================================================
        # TWILIO -> OPENAI
        # ====================================================

        async def receive_from_twilio():

            nonlocal stream_sid
            nonlocal latest_media_timestamp

            try:

                async for message in websocket.iter_text():

                    data = json.loads(message)

                    if data["event"] == "media":

                        latest_media_timestamp = int(
                            data["media"]["timestamp"]
                        )

                        audio_append = {
                            "type": (
                                "input_audio_buffer.append"
                            ),
                            "audio": data["media"]["payload"]
                        }

                        await openai_ws.send(
                            json.dumps(
                                audio_append
                            )
                        )

                    elif data["event"] == "start":

                        stream_sid = (
                            data["start"]["streamSid"]
                        )

                        print(
                            "Incoming stream started:",
                            stream_sid
                        )

                        latest_media_timestamp = 0

                        mark_queue.clear()

            except WebSocketDisconnect:

                print(
                    "Client disconnected."
                )

                if not openai_ws.closed:

                    await openai_ws.close()


        # ====================================================
        # OPENAI -> TWILIO
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

                    event_type = response.get(
                        "type"
                    )

                    if event_type in [
                        "error",
                        "response.done",
                        "session.created",
                        "session.updated",
                        "input_audio_buffer.speech_started",
                        "input_audio_buffer.speech_stopped",
                    ]:

                        print(
                            f"OpenAI event: {event_type}"
                        )

                    # ----------------------------------------
                    # AUDIO DE OPENAI
                    # ----------------------------------------

                    if (
                        event_type
                        == "response.output_audio.delta"
                        and response.get("delta")
                    ):

                        audio_payload = (
                            base64.b64encode(
                                base64.b64decode(
                                    response["delta"]
                                )
                            )
                            .decode("utf-8")
                        )

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
                    # NUEVA RESPUESTA
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
                        event_type
                        == "input_audio_buffer.speech_started"
                    ):

                        if last_assistant_item:

                            await handle_speech_started_event()


            except Exception as e:

                print(
                    f"Error in send_to_twilio: {e}"
                )


        # ====================================================
        # MANEJAR INTERRUPCIÓN
        # ====================================================

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

                        "type": (
                            "conversation.item.truncate"
                        ),

                        "item_id": (
                            last_assistant_item
                        ),

                        "content_index": 0,

                        "audio_end_ms": elapsed_time
                    }

                    await openai_ws.send(
                        json.dumps(
                            truncate_event
                        )
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
        # MARK
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
        # PRIMER MENSAJE
        # ====================================================

        await send_initial_conversation_item(
            openai_ws,
            language
        )


        # ====================================================
        # EJECUTAR AMBAS DIRECCIONES
        # ====================================================

        await asyncio.gather(
            receive_from_twilio(),
            send_to_twilio()
        )


# ============================================================
# HACER QUE OPENAI HABLE PRIMERO
# ============================================================

async def send_initial_conversation_item(
    openai_ws,
    language
):

    initial_instruction = (
        get_initial_instruction(
            language
        )
    )

    initial_conversation_item = {

        "type": "conversation.item.create",

        "item": {

            "type": "message",

            "role": "user",

            "content": [

                {
                    "type": "input_text",

                    "text": initial_instruction
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
    openai_ws,
    system_message
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

            "instructions": system_message
        }
    }

    print(
        "Sending session update"
    )

    await openai_ws.send(
        json.dumps(
            session_update
        )
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

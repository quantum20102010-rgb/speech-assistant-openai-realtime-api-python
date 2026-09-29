import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

_previous_openai_key = os.environ.get("OPENAI_API_KEY")
os.environ["OPENAI_API_KEY"] = "local-test-placeholder"
import main
if _previous_openai_key is None:
    os.environ.pop("OPENAI_API_KEY", None)
else:
    os.environ["OPENAI_API_KEY"] = _previous_openai_key
from call_results import (
    CallTranscript,
    LocalFileCallResultStorage,
    empty_call_result,
    normalize_model_result,
)
from call_safety import (
    CallAdmissionError,
    CallSafetyConfig,
    InMemoryCallManager,
    SafetyConfigurationError,
    normalize_destination,
)
from starlette.requests import Request
from twilio.request_validator import RequestValidator


class CallSafetyTests(unittest.TestCase):
    def config(self, **changes):
        config = CallSafetyConfig(
            enabled=True,
            max_concurrent=1,
            max_per_hour=5,
            max_per_day=20,
            max_duration_seconds=240,
            allowed_prefixes=("+52", "+1"),
        )
        return replace(config, **changes)

    def test_defaults_fail_closed_and_duration_is_at_most_240(self):
        with patch.dict(os.environ, {}, clear=True):
            config = CallSafetyConfig.from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(config.max_concurrent, 1)
        self.assertEqual(config.max_per_hour, 5)
        self.assertEqual(config.max_per_day, 20)
        self.assertEqual(config.max_duration_seconds, 240)
        self.assertEqual(config.allowed_prefixes, ("+52", "+1"))

        with patch.dict(os.environ, {"MAX_CALL_DURATION_SECONDS": "900"}, clear=True):
            with self.assertRaises(SafetyConfigurationError):
                CallSafetyConfig.from_env()

    def test_destination_is_normalized_and_country_restricted(self):
        self.assertEqual(
            normalize_destination("+1 (212) 555-0100", ("+52", "+1")),
            "+12125550100",
        )
        self.assertEqual(
            normalize_destination("+52 55 1234 5678", ("+52", "+1")),
            "+525512345678",
        )
        for invalid in ("2125550100", "+12125550100 ext 2", "+1212", "+442079460000"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CallAdmissionError):
                    normalize_destination(invalid, ("+52", "+1"))

    def test_concurrency_slot_releases_after_call_completion(self):
        manager = InMemoryCallManager()
        config = self.config()
        first = manager.reserve(config, "spanish")
        with self.assertRaises(CallAdmissionError) as error:
            manager.reserve(config, "english")
        self.assertEqual(error.exception.code, "concurrent_limit")
        manager.release(reservation_id=first.reservation_id)
        self.assertEqual(manager.active_count, 0)
        second = manager.reserve(config, "english")
        self.assertEqual(manager.active_count, 1)
        manager.release(reservation_id=second.reservation_id)

    def test_limits_are_independent_per_process_manager_instance(self):
        config = self.config(max_concurrent=1, max_per_hour=1, max_per_day=1)
        first_instance = InMemoryCallManager()
        second_instance = InMemoryCallManager()
        first = first_instance.reserve(config, "spanish")
        second = second_instance.reserve(config, "english")
        self.assertEqual(first_instance.active_count, 1)
        self.assertEqual(second_instance.active_count, 1)
        first_instance.release(reservation_id=first.reservation_id)
        second_instance.release(reservation_id=second.reservation_id)

    def test_hourly_and_daily_limits_count_attempts(self):
        current = [1000.0]
        manager = InMemoryCallManager(clock=lambda: current[0])
        first = manager.reserve(self.config(max_per_hour=1), "spanish")
        manager.release(reservation_id=first.reservation_id)
        with self.assertRaises(CallAdmissionError) as hourly:
            manager.reserve(self.config(max_per_hour=1), "spanish")
        self.assertEqual(hourly.exception.code, "hourly_limit")

        daily_manager = InMemoryCallManager(clock=lambda: current[0])
        first = daily_manager.reserve(self.config(max_per_day=1), "spanish")
        daily_manager.release(reservation_id=first.reservation_id)
        with self.assertRaises(CallAdmissionError) as daily:
            daily_manager.reserve(self.config(max_per_day=1), "spanish")
        self.assertEqual(daily.exception.code, "daily_limit")


class CallResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_storage = main.call_result_storage
        self.old_manager = main.call_manager
        self.saved_environment = {
            name: os.environ.get(name)
            for name in (
                "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "RAILWAY_PUBLIC_DOMAIN",
                "TWILIO_PHONE_NUMBER", "CALLS_ENABLED", "CALL_SECRET", "OPENAI_API_KEY",
            )
        }
        main.call_result_storage = LocalFileCallResultStorage(self.temp.name)
        main.call_manager = InMemoryCallManager()
        os.environ["TWILIO_ACCOUNT_SID"] = "AC" + ("b" * 32)
        os.environ["TWILIO_AUTH_TOKEN"] = "b" * 32
        os.environ["TWILIO_PHONE_NUMBER"] = "+12125550199"
        os.environ["CALL_SECRET"] = "local-test-call-secret"
        os.environ["OPENAI_API_KEY"] = "local-test-openai-key"

    def tearDown(self):
        main.call_result_storage = self.old_storage
        main.call_manager = self.old_manager
        for name, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    async def _finalize(self, reason):
        config = CallSafetyConfig(
            True, 1, 5, 20, 240, ("+52", "+1")
        )
        record = main.call_manager.reserve(config, "spanish")
        main.call_manager.bind_call_sid(record.reservation_id, "CA_TEST")
        main.call_manager.admit_media_stream("CA_TEST", config, "spanish")
        model_payload = {
            "summary": "El proveedor confirmó interés en distribución.",
            "company": "Proveedor Ejemplo",
            "contact_name": None,
            "mission_findings": {
                "market_and_customer_segment": "Comercios independientes en el centro del país.",
                "customer_needs_and_trends": None,
            },
        }
        mission = main.resolve_mission("market_research")
        fake_openai = AsyncMock()
        fake_openai.recv.return_value = json.dumps({
            "type": "response.done",
            "response": {
                "metadata": {"purpose": "call_report"},
                "output": [{"type": "message", "content": [{
                    "type": "output_text", "text": json.dumps(model_payload)
                }]}],
            },
        })
        fake_twilio_ws = AsyncMock()
        with patch("twilio.rest.Client") as twilio_client:
            await main.finalize_call(
                "CA_TEST", record.reservation_id, fake_openai, fake_twilio_ws,
                reason, "spanish", record.started_at, 12.5,
                "Contacto: Busco información de distribución.\nAsistente: Claro, con gusto.",
                mission,
            )
        files = list(Path(self.temp.name).glob("call-*.json"))
        self.assertEqual(len(files), 1)
        result = json.loads(files[0].read_text(encoding="utf-8"))
        txt_file = files[0].with_suffix(".txt")
        self.assertTrue(txt_file.exists())
        self.assertIn("GUZI STUFF — REPORTE DE LLAMADA", txt_file.read_text(encoding="utf-8"))
        self.assertEqual(result["summary"], model_payload["summary"])
        self.assertEqual(result["company"], model_payload["company"])
        self.assertEqual(result["duration_seconds"], 12.5)
        self.assertEqual(
            result["transcript"],
            "Contacto: Busco información de distribución.\nAsistente: Claro, con gusto.",
        )
        self.assertTrue(result["summary_generated_by_model"])
        self.assertEqual(result["mission_id"], "market_research")
        self.assertEqual(
            result["mission_findings"]["market_and_customer_segment"],
            model_payload["mission_findings"]["market_and_customer_segment"],
        )
        report_text = txt_file.read_text(encoding="utf-8")
        self.assertIn("RESUMEN (GENERADO POR EL MODELO)", report_text)
        self.assertIn("TRANSCRIPCIÓN DE LA CONVERSACIÓN", report_text)
        self.assertIn("Contacto: Busco información de distribución.", report_text)
        report_request = json.loads(fake_openai.send.await_args_list[1].args[0])
        report_prompt = report_request["item"]["content"][0]["text"]
        self.assertIn(mission.objective, report_prompt)
        self.assertIn(mission.relevance_criteria[0], report_prompt)
        self.assertIn(mission.follow_up_actions[0], report_prompt)
        self.assertEqual(main.call_manager.active_count, 0)
        fake_openai.close.assert_awaited_once()
        fake_twilio_ws.close.assert_awaited_once()
        if reason == "twilio_stopped":
            twilio_client.assert_not_called()
            self.assertEqual(result["call_status"], "completed")
        else:
            twilio_client.return_value.calls.return_value.update.assert_called_once_with(
                status="completed"
            )
        return result

    async def test_result_structure_and_safe_txt_files(self):
        started = datetime.now(timezone.utc)
        fallback = empty_call_result("error", "spanish", started)
        self.assertIsNone(fallback["company"])
        self.assertIsNone(fallback["email"])
        self.assertEqual(fallback["call_status"], "error")
        self.assertFalse(fallback["summary_generated_by_model"])
        normalized = normalize_model_result({"company": 42, "summary": " "}, "completed", "spanish", started, 0)
        self.assertIsNone(normalized["company"])
        self.assertIsNone(normalized["summary"])

        result = await self._finalize("twilio_stopped")
        self.assertEqual(result["call_status"], "completed")
        self.assertEqual(result["decision"]["relevance"], "actionable")
        self.assertEqual(result["decision"]["summary"]["generated_by"], "model")
        filenames = [path.name for path in Path(self.temp.name).iterdir()]
        self.assertEqual(len(filenames), 2)
        self.assertTrue(all("+" not in filename and "CA_TEST" not in filename for filename in filenames))

    async def test_hold_duration_and_error_each_generate_reports_and_end_twilio(self):
        for reason, expected in (
            ("hold_detected", "hold_detected"),
            ("max_duration", "max_duration"),
            ("connection_error", "error"),
        ):
            with self.subTest(reason=reason):
                result = await self._finalize(reason)
                self.assertEqual(result["call_status"], expected)
                self.assertIn("Contacto: Busco información", result["transcript"])
                for path in Path(self.temp.name).glob("call-*"):
                    path.unlink()

    async def test_openai_dependency_error_closes_call_without_another_openai_request(self):
        config = CallSafetyConfig(True, 1, 5, 20, 240, ("+52", "+1"))
        record = main.call_manager.reserve(config, "spanish")
        main.call_manager.bind_call_sid(record.reservation_id, "CA_TEST")
        fake_openai = AsyncMock()
        fake_twilio_ws = AsyncMock()
        with patch("twilio.rest.Client") as twilio_client, \
             patch("main.extract_call_report", wraps=main.extract_call_report) as extract:
            await main.finalize_call(
                "CA_TEST", record.reservation_id, fake_openai, fake_twilio_ws,
                "openai_dependency_error", "spanish", record.started_at, 5,
                "Contacto: Hola.", main.resolve_mission(),
            )
        self.assertIsNone(extract.await_args.args[0])
        fake_openai.send.assert_not_awaited()
        fake_openai.recv.assert_not_awaited()
        twilio_client.return_value.calls.return_value.update.assert_called_once_with(
            status="completed"
        )
        saved = next(Path(self.temp.name).glob("call-*.json"))
        result = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(result["call_status"], "error")
        self.assertEqual(result["transcript"], "Contacto: Hola.")

    async def test_transcript_collects_completed_both_speakers_and_deduplicates(self):
        transcript = CallTranscript()
        previous_secret = os.environ.get("CALL_SECRET")
        os.environ["CALL_SECRET"] = "local-transcript-redaction-check"
        self.assertTrue(transcript.add_realtime_event({
            "type": "input_audio_buffer.committed",
            "item_id": "item-contact-1",
        }))
        self.assertTrue(transcript.add_realtime_event({
            "type": "response.output_audio_transcript.done",
            "item_id": "item-assistant-1",
            "transcript": "Con gusto le ayudo.",
        }))
        self.assertTrue(transcript.add_realtime_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-contact-1",
            "transcript": "Necesito una cotización. local-transcript-redaction-check",
        }))
        self.assertFalse(transcript.add_realtime_event({
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "item-contact-1",
            "delta": "ignored partial",
        }))
        self.assertFalse(transcript.add_realtime_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-contact-1",
            "transcript": "duplicated",
        }))
        self.assertEqual(
            transcript.render(),
            "Contacto: Necesito una cotización. [REDACTED]\nAsistente: Con gusto le ayudo.",
        )
        if previous_secret is None:
            os.environ.pop("CALL_SECRET", None)
        else:
            os.environ["CALL_SECRET"] = previous_secret

    async def test_realtime_session_enables_input_transcript_without_changing_audio(self):
        socket = AsyncMock()
        mission = main.resolve_mission("market_research")
        await main.initialize_session(socket, "spanish", mission)
        session_update = json.loads(socket.send.await_args_list[0].args[0])
        session = session_update["session"]
        self.assertEqual(session["output_modalities"], ["audio"])
        self.assertEqual(session["audio"]["input"]["format"]["type"], "audio/pcmu")
        self.assertEqual(
            session["audio"]["input"]["transcription"]["model"],
            "gpt-4o-mini-transcribe",
        )
        self.assertEqual(session["audio"]["input"]["turn_detection"]["type"], "server_vad")
        self.assertIn(mission.objective, session["instructions"])
        self.assertIn("not as a checklist or script", session["instructions"])
        self.assertIn("market_and_customer_segment", session["instructions"])

    async def test_status_callback_signature_releases_slot_and_records_rejection(self):
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "test-domain.up.railway.app"
        manager = main.call_manager
        config = CallSafetyConfig(True, 1, 5, 20, 240, ("+52", "+1"))
        record = manager.reserve(config, "spanish")
        manager.bind_call_sid(record.reservation_id, "CA_TEST")
        params = {"CallSid": "CA_TEST", "CallStatus": "no-answer"}
        url = "https://test-domain.up.railway.app/call-status"
        signature = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"]).compute_signature(url, params)
        body = urlencode(params).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({
            "type": "http", "method": "POST", "path": "/call-status",
            "query_string": b"", "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"x-twilio-signature", signature.encode()),
            ], "server": ("test-domain.up.railway.app", 443), "scheme": "https",
        }, receive=receive)
        response = await main.handle_call_status(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(manager.active_count, 0)
        result_file = next(Path(self.temp.name).glob("call-*.json"))
        self.assertEqual(json.loads(result_file.read_text())["call_status"], "no-answer")

    async def test_disabled_kill_switch_never_opens_realtime(self):
        os.environ["CALLS_ENABLED"] = "false"
        websocket = AsyncMock()
        websocket.receive_text.return_value = json.dumps({
            "event": "start",
            "start": {"streamSid": "MZ_TEST", "callSid": "CA_TEST"},
        })
        with patch("main.websockets.connect") as realtime_connect, \
             patch("twilio.rest.Client") as twilio_client:
            await main.handle_media_stream(websocket, "spanish")
        realtime_connect.assert_not_called()
        twilio_client.assert_not_called()
        websocket.receive_text.assert_not_awaited()
        websocket.accept.assert_not_awaited()
        websocket.close.assert_awaited_once_with(code=1008)

    async def test_media_websocket_valid_twilio_signature_continues_to_admission(self):
        os.environ["CALLS_ENABLED"] = "true"
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "test-domain.up.railway.app"
        public_url = "wss://test-domain.up.railway.app/media-stream/spanish"
        signature = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"]).compute_signature(
            public_url, {}
        )
        websocket = AsyncMock()
        websocket.headers = {"x-twilio-signature": signature}
        websocket.url = type("URL", (), {"path": "/media-stream/spanish", "query": ""})()
        websocket.receive_text.return_value = json.dumps({
            "event": "start",
            "start": {
                "streamSid": "MZ_TEST",
                "callSid": "CA_TEST",
                "customParameters": {"mission": "market_research"},
            },
        })

        with patch.object(
            main.call_manager,
            "admit_media_stream",
            side_effect=CallAdmissionError("test_rejection"),
        ) as admit, patch("main.finish_call", new_callable=AsyncMock), patch(
            "main.write_call_result", new_callable=AsyncMock
        ), patch("main.check_openai_readiness", new_callable=AsyncMock) as readiness, patch(
            "main.websockets.connect"
        ) as realtime_connect:
            await main.handle_media_stream(websocket, "spanish")

        websocket.accept.assert_awaited_once()
        websocket.receive_text.assert_awaited_once()
        admit.assert_called_once()
        readiness.assert_not_awaited()
        realtime_connect.assert_not_called()

    async def test_media_websocket_missing_or_invalid_signature_rejects_before_admission(self):
        os.environ["CALLS_ENABLED"] = "true"
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "test-domain.up.railway.app"

        for supplied_signature in (None, "invalid-signature"):
            with self.subTest(signature_present=supplied_signature is not None):
                websocket = AsyncMock()
                websocket.headers = (
                    {} if supplied_signature is None
                    else {"x-twilio-signature": supplied_signature}
                )
                websocket.url = type(
                    "URL", (), {"path": "/media-stream/spanish", "query": ""}
                )()
                before_reservations = main.call_manager.active_count

                with patch.object(main.call_manager, "admit_media_stream") as admit, patch(
                    "main.check_openai_readiness", new_callable=AsyncMock
                ) as readiness, patch("main.websockets.connect") as realtime_connect:
                    await main.handle_media_stream(websocket, "spanish")

                websocket.accept.assert_not_awaited()
                websocket.close.assert_awaited_once_with(code=1008)
                websocket.receive_text.assert_not_awaited()
                admit.assert_not_called()
                self.assertEqual(main.call_manager.active_count, before_reservations)
                readiness.assert_not_awaited()
                realtime_connect.assert_not_called()

    async def test_model_fields_redact_known_secrets(self):
        previous = os.environ.get("CALL_SECRET")
        os.environ["CALL_SECRET"] = "local-secret-for-redaction"
        try:
            result = normalize_model_result(
                {"summary": "Value local-secret-for-redaction must not persist"},
                "completed", "spanish", datetime.now(timezone.utc), 1,
            )
        finally:
            if previous is None:
                os.environ.pop("CALL_SECRET", None)
            else:
                os.environ["CALL_SECRET"] = previous
        self.assertNotIn("local-secret-for-redaction", result["summary"])
        self.assertIn("[REDACTED]", result["summary"])

    async def test_home_keeps_all_19_languages(self):
        response = await main.index_page()
        self.assertEqual(len(response["languages"]), 19)
        self.assertIn("spanish", response["languages"])
        self.assertIn("russian", response["languages"])

    async def test_incoming_and_outbound_webhooks_honor_disabled_kill_switch(self):
        os.environ["CALLS_ENABLED"] = "false"

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        for path, handler in (
            ("/incoming-call", main.handle_incoming_call),
            ("/outbound-call", main.handle_outbound_call),
        ):
            request = Request({
                "type": "http", "method": "POST", "path": path,
                "query_string": b"", "headers": [],
                "server": ("test-domain.up.railway.app", 443), "scheme": "https",
            }, receive=receive)
            response = await handler(request)
            content = response.body.decode("utf-8")
            self.assertIn("<Hangup", content)
            self.assertNotIn("<Stream", content)

    async def test_outbound_twiml_uses_railway_domain_and_preserves_language(self):
        os.environ["CALLS_ENABLED"] = "true"
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "test-domain.up.railway.app"

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        for language in ("spanish", "french"):
            request = Request({
                "type": "http", "method": "GET", "path": "/outbound-call",
                "query_string": f"language={language}".encode(), "headers": [],
                "server": ("internal.railway", 5050), "scheme": "http",
            }, receive=receive)
            response = await main.handle_outbound_call(request)
            content = response.body.decode("utf-8")
            self.assertIn(
                f"wss://test-domain.up.railway.app/media-stream/{language}",
                content,
            )

        request = Request({
            "type": "http", "method": "GET", "path": "/outbound-call",
            "query_string": b"language=spanish&mission=market_research", "headers": [],
            "server": ("internal.railway", 5050), "scheme": "http",
        }, receive=receive)
        response = await main.handle_outbound_call(request)
        twiml = response.body.decode("utf-8")
        self.assertIn('name="mission"', twiml)
        self.assertIn("market_research", twiml)
        self.assertIn('name="language"', twiml)


if __name__ == "__main__":
    unittest.main()

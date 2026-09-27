import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from requests.exceptions import Timeout
from starlette.requests import Request
from twilio.base.exceptions import TwilioRestException

os.environ["OPENAI_API_KEY"] = "local-test-placeholder"
import main


TEST_SECRET = "local-test-placeholder"
TEST_NUMBER = "+12125550100"


def make_request(query="", headers=()):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/make-call",
            "query_string": query.encode("ascii"),
            "headers": list(headers),
            "server": ("test-domain.up.railway.app", 443),
            "scheme": "https",
        }
    )


class MakeCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["CALL_SECRET"] = TEST_SECRET
        os.environ["TWILIO_ACCOUNT_SID"] = "local-test-account"
        os.environ["TWILIO_AUTH_TOKEN"] = "local-test-token"
        os.environ["TWILIO_PHONE_NUMBER"] = "+12125550199"
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "test-domain.up.railway.app"

    def tearDown(self):
        for key in (
            "CALL_SECRET",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_PHONE_NUMBER",
            "RAILWAY_PUBLIC_DOMAIN",
        ):
            os.environ.pop(key, None)

    async def test_authorization_is_required_before_twilio(self):
        request = make_request(
            urlencode({"to": TEST_NUMBER}),
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        self.assertNotIn(TEST_SECRET.encode(), response.body)
        twilio_client.assert_not_called()

    async def test_missing_destination_returns_400_after_authorization(self):
        request = make_request(headers=[(b"x-call-secret", TEST_SECRET.encode())])
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 400)
        twilio_client.assert_not_called()

    async def test_success_uses_mocked_twilio_and_does_not_expose_number(self):
        request = make_request(
            urlencode({"to": TEST_NUMBER, "language": "spanish"}),
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        loop_thread = threading.get_ident()
        call_thread = []

        def create_call(**kwargs):
            call_thread.append(threading.get_ident())
            return SimpleNamespace(sid="CA_TEST")

        output = StringIO()
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.side_effect = create_call
            with redirect_stdout(output):
                response = await main.make_call(request)

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "call_created")
        self.assertNotIn("to", payload)
        self.assertNotIn(TEST_NUMBER, response.body.decode())
        self.assertNotIn(TEST_SECRET, response.body.decode())
        self.assertNotIn(TEST_NUMBER, output.getvalue())
        self.assertNotIn(TEST_SECRET, output.getvalue())
        self.assertNotEqual(call_thread[0], loop_thread)

        client_kwargs = twilio_client.call_args.kwargs
        self.assertEqual(client_kwargs["http_client"].timeout, main.TWILIO_HTTP_TIMEOUT)
        twilio_client.return_value.calls.create.assert_called_once_with(
            to=TEST_NUMBER,
            from_="+12125550199",
            url="https://test-domain.up.railway.app/outbound-call?language=spanish",
        )

    async def test_http_timeout_returns_sanitized_504(self):
        request = make_request(
            urlencode({"to": TEST_NUMBER}),
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.side_effect = Timeout()
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 504)
        self.assertEqual(json.loads(response.body), {"error": "The request to Twilio timed out."})
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        self.assertNotIn(TEST_SECRET.encode(), response.body)

    async def test_twilio_exception_returns_sanitized_error(self):
        request = make_request(
            urlencode({"to": TEST_NUMBER}),
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        exception = TwilioRestException(
            400,
            "/2010-04-01/Calls.json",
            "invalid destination",
            code=21211,
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.side_effect = exception
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            json.loads(response.body),
            {
                "error": "Twilio rejected the call request.",
                "twilio_error_code": 21211,
            },
        )
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        self.assertNotIn(TEST_SECRET.encode(), response.body)


if __name__ == "__main__":
    unittest.main()

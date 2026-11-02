import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from server import make_handler
from service import PaymentError, TemporaryError


class FakeService:
    def __init__(self):
        self.result = "paid"

    def handle_notification(self, body):
        if self.result == "down":
            raise TemporaryError
        return self.result

    def create_link(self, deal_id):
        if deal_id == 404:
            raise PaymentError("В сделке не указана сумма")
        return f"https://pay.test/{deal_id}"


@pytest.fixture
def call():
    service = FakeService()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, "тайна".encode().hex()))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _call(path, body=b"{}", token=None):
        headers = {"X-Api-Token": token} if token else {}
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    _call.service = service
    yield _call
    server.shutdown()


GOOD = "тайна".encode().hex()


def test_webhook_ok_and_bad_json(call):
    assert call("/yookassa/webhook")[0] == 200
    assert call("/yookassa/webhook", b"{broken")[0] == 400


def test_webhook_asks_for_resend_when_kassa_is_down(call):
    call.service.result = "down"
    assert call("/yookassa/webhook")[0] == 503


def test_pay_requires_token(call):
    assert call("/deals/1/pay")[0] == 403
    assert call("/deals/1/pay", token="неверно".encode().hex())[0] == 403
    assert call("/deals/1/pay", token=GOOD) == (200, {"ok": True, "url": "https://pay.test/1"})


def test_pay_error_and_unknown_path(call):
    assert call("/deals/404/pay", token=GOOD)[0] == 400
    assert call("/deals/abc/pay", token=GOOD)[0] == 404
    assert call("/other", token=GOOD)[0] == 404

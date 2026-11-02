import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from form_handler import BadLead, RateLimiter, create_deal, make_handler, parse_lead


class FakeB24:
    def __init__(self, existing=None, fail=False):
        self.existing, self.fail, self.calls = existing, fail, []

    def result(self, method, params=None):
        self.calls.append((method, params))
        if self.fail:
            raise RuntimeError("https://portal/rest/1/SECRET/ упал")
        if method == "crm.duplicate.findbycomm":
            return {"CONTACT": [self.existing]} if self.existing else []
        return 77

    def added(self, method):
        return [p for m, p in self.calls if m == method]


def test_parse_lead_cleans_phone():
    assert parse_lead({"name": " Анна ", "phone": "8 (900) 123-45-67", "object": "Дом"}) == {"name": "Анна", "phone": "+79001234567", "object": "Дом"}


@pytest.mark.parametrize("data", [{"phone": "89001234567"}, {"name": "Анна", "phone": "123"}, {"name": "", "phone": ""}, ["список"]])
def test_parse_lead_rejects_bad_input(data):
    with pytest.raises(BadLead):
        parse_lead(data)


def test_new_client_gets_contact_and_deal():
    b24 = FakeB24()
    assert create_deal(b24, {"name": "Анна Попова", "phone": "+79001234567", "object": "Дом"}, 2, {"Дом": "9"}) == (77, False)
    deal = b24.added("crm.deal.add")[0]["fields"]
    assert deal["STAGE_ID"] == "C2:NEW" and deal["UF_CRM_OBJECT_TYPE"] == "9" and deal["CONTACT_ID"] == 77
    assert len(b24.added("crm.contact.add")) == 1
    assert b24.added("crm.contact.add")[0]["fields"]["NAME"] == "Анна Попова"


def test_known_phone_does_not_create_second_contact():
    b24 = FakeB24(existing="5")
    assert create_deal(b24, {"name": "Анна", "phone": "+79001234567", "object": ""}, 2, {}) == (77, True)
    assert not b24.added("crm.contact.add")
    assert b24.added("crm.deal.add")[0]["fields"]["CONTACT_ID"] == 5


def test_rate_limiter():
    limit = RateLimiter(limit=2, window=60)
    assert limit.allow("ip", 0) and limit.allow("ip", 1)
    assert not limit.allow("ip", 2)
    assert limit.allow("другой", 2)
    assert limit.allow("ip", 100)


@pytest.fixture
def post():
    servers = []

    def _post(b24, body, limiter=None):
        handler = make_handler(b24, 2, {}, "https://site.test", limiter or RateLimiter())
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/lead", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.load(e), e.headers

    yield _post
    for s in servers:
        s.shutdown()


def test_http_success_and_cors(post):
    code, data, headers = post(FakeB24(), json.dumps({"name": "Анна", "phone": "89001234567"}).encode())
    assert code == 200 and data == {"ok": True}
    assert headers["Access-Control-Allow-Origin"] == "https://site.test"


def test_honeypot_creates_nothing(post):
    b24 = FakeB24()
    code, data, _ = post(b24, json.dumps({"name": "Бот", "phone": "89001234567", "website": "spam"}).encode())
    assert code == 200 and data == {"ok": True} and not b24.calls


def test_bad_json_and_big_body(post):
    assert post(FakeB24(), b"{not json")[0] == 400
    assert post(FakeB24(), b"x" * 5000)[0] == 400


def test_crm_failure_does_not_leak_webhook(post):
    code, data, _ = post(FakeB24(fail=True), json.dumps({"name": "Анна", "phone": "89001234567"}).encode())
    assert code == 502 and "SECRET" not in json.dumps(data)


def test_rate_limit_over_http(post):
    code, _, _ = post(FakeB24(), b'{"name":"A","phone":"89001234567"}', RateLimiter(limit=0))
    assert code == 429

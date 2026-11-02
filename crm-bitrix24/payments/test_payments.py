"""Сценарии отказов: повтор и подделка уведомления, сбой CRM и кассы, неверная сумма, откат стадии."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from service import PaymentError, PaymentService, Store, TemporaryError, export_for_1c, reconcile


class FakeB24:
    def __init__(self, stage="C2:CONTRACT", amount="100000", category=2):
        self.deal = {"ID": "1", "STAGE_ID": stage, "OPPORTUNITY": amount, "CATEGORY_ID": category}
        self.comments, self.fail = [], False

    def result(self, method, params=None):
        if self.fail:
            raise RuntimeError("https://portal/rest/1/SECRET/ упал")
        if method == "crm.deal.get":
            return self.deal
        if method == "crm.deal.update":
            self.deal["STAGE_ID"] = params["fields"]["STAGE_ID"]
            return True
        if method == "crm.timeline.comment.add":
            self.comments.append(params["fields"]["COMMENT"])
            return 1
        raise AssertionError(method)


class FakeKassa:
    def __init__(self):
        self.payments, self.keys, self.down = {}, {}, False

    def create_payment(self, amount, description, return_url, metadata, idempotence_key):
        if idempotence_key in self.keys:
            return self.payments[self.keys[idempotence_key]]
        pid = f"pay-{len(self.payments) + 1}"
        self.keys[idempotence_key] = pid
        self.payments[pid] = {"id": pid, "status": "pending", "paid": False, "amount": {"value": amount, "currency": "RUB"}, "metadata": metadata,
                              "confirmation": {"confirmation_url": f"https://pay.test/{pid}"}}
        return self.payments[pid]

    def get_payment(self, pid):
        if self.down:
            raise ConnectionError
        return self.payments[pid]

    def pay(self, pid):
        self.payments[pid].update(status="succeeded", paid=True)


@pytest.fixture
def env(tmp_path):
    b24, kassa = FakeB24(), FakeKassa()
    store = Store(str(tmp_path / "p.db"))
    return PaymentService(b24, kassa, store, 2, "https://site.test/thanks"), b24, kassa, store


NOTE = {"event": "payment.succeeded", "object": {"id": "pay-1"}}


def test_link_is_30_percent_and_repeat_gives_same_payment(env):
    svc, b24, kassa, store = env
    assert svc.create_link(1) == svc.create_link(1) == "https://pay.test/pay-1"
    assert store.get("pay-1")["amount"] == "30000.00"
    assert len(kassa.payments) == 1 and len(b24.comments) == 1


@pytest.mark.parametrize("deal", [{"amount": "0"}, {"category": 5}])
def test_link_refused_for_bad_deal(tmp_path, deal):
    svc = PaymentService(FakeB24(**deal), FakeKassa(), Store(str(tmp_path / "p.db")), 2, "u")
    with pytest.raises(PaymentError):
        svc.create_link(1)


def test_paid_moves_deal_once_even_on_repeat(env):
    svc, b24, kassa, _ = env
    svc.create_link(1)
    kassa.pay("pay-1")
    assert svc.handle_notification(NOTE) == "paid"
    assert b24.deal["STAGE_ID"] == "C2:PREPAID"
    count = len(b24.comments)
    assert svc.handle_notification(NOTE) == "duplicate"
    assert len(b24.comments) == count


def test_fake_notification_is_rejected(env):
    """Уведомление говорит «оплачено», а касса нет: стадия не меняется."""
    svc, b24, _, store = env
    svc.create_link(1)
    assert svc.handle_notification(NOTE) == "not_confirmed"
    assert b24.deal["STAGE_ID"] == "C2:CONTRACT" and store.get("pay-1")["status"] != "succeeded"


def test_unknown_payment_and_junk_are_ignored(env):
    svc, b24, _, _ = env
    assert svc.handle_notification({"event": "payment.succeeded", "object": {"id": "чужой"}}) == "unknown"
    for junk in ({}, {"event": "payment.canceled", "object": {"id": "pay-1"}}, {"event": "payment.succeeded", "object": "x"}, []):
        assert svc.handle_notification(junk) == "ignored"
    assert not b24.comments


def test_wrong_amount_does_not_move_deal(env):
    svc, b24, kassa, _ = env
    svc.create_link(1)
    kassa.pay("pay-1")
    kassa.payments["pay-1"]["amount"]["value"] = "1.00"
    assert svc.handle_notification(NOTE) == "mismatch"
    assert b24.deal["STAGE_ID"] == "C2:CONTRACT"


def test_kassa_down_asks_for_resend(env):
    svc, _, kassa, _ = env
    svc.create_link(1)
    kassa.down = True
    with pytest.raises(TemporaryError):
        svc.handle_notification(NOTE)


def test_crm_down_keeps_payment_and_retry_delivers_once(env):
    svc, b24, kassa, store = env
    svc.create_link(1)
    kassa.pay("pay-1")
    b24.fail = True
    assert svc.handle_notification(NOTE) == "paid"
    row = store.get("pay-1")
    assert row["status"] == "succeeded" and row["crm_synced"] == 0 and row["attempts"] == 1
    assert "SECRET" not in row["last_error"]
    assert svc.retry_pending() == 0
    b24.fail = False
    assert svc.retry_pending() == 1
    assert b24.deal["STAGE_ID"] == "C2:PREPAID"
    assert svc.retry_pending() == 0


def test_repeat_notification_after_crm_failure_delivers(env):
    svc, b24, kassa, store = env
    svc.create_link(1)
    kassa.pay("pay-1")
    b24.fail = True
    svc.handle_notification(NOTE)
    b24.fail = False
    assert svc.handle_notification(NOTE) == "duplicate"
    assert store.get("pay-1")["crm_synced"] == 1 and b24.deal["STAGE_ID"] == "C2:PREPAID"


def test_stage_is_not_moved_back(env):
    svc, b24, kassa, _ = env
    svc.create_link(1)
    kassa.pay("pay-1")
    b24.deal["STAGE_ID"] = "C2:WORKS"
    svc.handle_notification(NOTE)
    assert b24.deal["STAGE_ID"] == "C2:WORKS"
    assert "Получена оплата" in b24.comments[-1]


def test_export_and_reconcile(env):
    svc, _, kassa, store = env
    svc.create_link(1)
    kassa.pay("pay-1")
    svc.handle_notification(NOTE)
    assert "30000,00;pay-1;1" in export_for_1c(store)
    ok = {"id": "pay-1", "status": "succeeded", "amount": {"value": "30000.00"}}
    assert reconcile(store, [ok]) == {"нет_в_журнале": [], "нет_в_кассе": [], "разная_сумма": [], "не_дошло_до_crm": []}
    extra = {"id": "pay-9", "status": "succeeded", "amount": {"value": "5.00"}}
    other = {"id": "pay-1", "status": "succeeded", "amount": {"value": "1.00"}}
    assert reconcile(store, [other, extra]) == {"нет_в_журнале": ["pay-9"], "нет_в_кассе": [], "разная_сумма": ["pay-1"], "не_дошло_до_crm": []}
    assert reconcile(store, [])["нет_в_кассе"] == ["pay-1"]

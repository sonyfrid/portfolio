"""Оплата сделки Битрикс24 через ЮKassa: ссылка на оплату, уведомления, журнал, выгрузка и сверка.

Главные правила:
- уведомлению не верим: статус и сумму перечитываем из API ЮKassa;
- повторное уведомление ничего не меняет, запись в журнале одна на платёж;
- если CRM недоступна, оплата не теряется: она остаётся в журнале и уходит в CRM при повторе;
- стадия сделки не откатывается назад.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

STAGE_ORDER = ["NEW", "CONTACTED", "VISIT_SET", "VISIT_DONE", "ESTIMATE", "CONTRACT", "PREPAID", "WORKS", "HANDOVER", "WON"]
PAID_STAGE = "PREPAID"
SCHEMA = """
create table if not exists payments (
  id text primary key, deal_id integer not null, amount text not null, status text not null,
  url text, crm_synced integer not null default 0, attempts integer not null default 0,
  last_error text, created_at text not null, paid_at text
);
create table if not exists events (
  at text not null, payment_id text, event text not null, detail text
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(value) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


class PaymentError(Exception):
    """Ошибка, которую можно показать менеджеру."""


class TemporaryError(Exception):
    """Сбой внешнего сервиса: ЮKassa пришлёт уведомление повторно."""


class Store:
    def __init__(self, path: str = "payments.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def log(self, payment_id, event, detail=""):
        with self.db:
            self.db.execute("insert into events values (?,?,?,?)", (now(), payment_id, event, detail))

    def get(self, payment_id):
        return self.db.execute("select * from payments where id=?", (payment_id,)).fetchone()

    def add(self, payment_id, deal_id, amount, status, url):
        with self.db:
            self.db.execute("insert or ignore into payments (id,deal_id,amount,status,url,created_at) values (?,?,?,?,?,?)",
                            (payment_id, deal_id, amount, status, url, now()))

    def mark_paid(self, payment_id) -> bool:
        """True только у первого, кто отметил оплату: так повтор уведомления ничего не делает."""
        with self.db:
            cur = self.db.execute("update payments set status='succeeded', paid_at=? where id=? and status!='succeeded'", (now(), payment_id))
        return cur.rowcount == 1

    def mark_synced(self, payment_id):
        with self.db:
            self.db.execute("update payments set crm_synced=1, last_error=null where id=?", (payment_id,))

    def mark_failed(self, payment_id, error):
        with self.db:
            self.db.execute("update payments set attempts=attempts+1, last_error=? where id=?", (error, payment_id))

    def unsynced(self):
        return self.db.execute("select * from payments where status='succeeded' and crm_synced=0").fetchall()

    def paid(self):
        return self.db.execute("select * from payments where status='succeeded' order by paid_at").fetchall()


class PaymentService:
    def __init__(self, b24, kassa, store: Store, category_id: int, return_url: str, prepay_share: Decimal = Decimal("0.3")):
        self.b24, self.kassa, self.store, self.category_id = b24, kassa, store, category_id
        self.return_url, self.prepay_share = return_url, prepay_share

    def create_link(self, deal_id: int) -> str:
        """Ссылка на предоплату. Повторный вызов с той же суммой вернёт тот же платёж: ключ идемпотентности один."""
        deal = self.b24.result("crm.deal.get", {"id": deal_id})
        if int(deal.get("CATEGORY_ID") or 0) != self.category_id:
            raise PaymentError("Сделка из другой воронки")
        total = Decimal(str(deal.get("OPPORTUNITY") or 0))
        if total <= 0:
            raise PaymentError("В сделке не указана сумма")
        amount = money(total * self.prepay_share)
        payment = self.kassa.create_payment(amount=amount, description=f"Предоплата по сделке {deal_id}", return_url=self.return_url,
                                            metadata={"deal_id": str(deal_id)}, idempotence_key=f"deal-{deal_id}-{amount}")
        url = payment["confirmation"]["confirmation_url"]
        known = self.store.get(payment["id"]) is not None
        self.store.add(payment["id"], deal_id, amount, payment["status"], url)
        self.store.log(payment["id"], "link_created" if not known else "link_repeated", f"сделка {deal_id}, {amount} ₽")
        if not known:
            self._comment(deal_id, f"Ссылка на предоплату {amount} ₽: {url}")
        return url

    def handle_notification(self, body: dict) -> str:
        """Вернёт, что произошло. TemporaryError означает, что надо ответить ошибкой, и ЮKassa повторит уведомление."""
        obj = body.get("object") if isinstance(body, dict) else None
        payment_id = obj.get("id") if isinstance(obj, dict) else None
        if not payment_id or body.get("event") != "payment.succeeded":
            return "ignored"
        local = self.store.get(payment_id)
        if local is None:
            self.store.log(payment_id, "unknown_payment")
            return "unknown"
        try:
            real = self.kassa.get_payment(payment_id)
        except Exception as exc:
            self.store.log(payment_id, "kassa_unavailable", type(exc).__name__)
            raise TemporaryError from exc
        if real.get("status") != "succeeded" or not real.get("paid"):
            self.store.log(payment_id, "not_confirmed", str(real.get("status")))
            return "not_confirmed"
        if money(real["amount"]["value"]) != local["amount"] or str(real.get("metadata", {}).get("deal_id")) != str(local["deal_id"]):
            self.store.log(payment_id, "mismatch", f"ждали {local['amount']}, пришло {real['amount']['value']}")
            return "mismatch"
        first = self.store.mark_paid(payment_id)
        self.store.log(payment_id, "paid" if first else "duplicate")
        if self.store.get(payment_id)["crm_synced"]:
            return "duplicate"
        self._sync(self.store.get(payment_id))
        return "paid" if first else "duplicate"

    def retry_pending(self) -> int:
        """Повтор для оплат, которые не дошли до CRM. Запускается по расписанию."""
        done = 0
        for row in self.store.unsynced():
            done += self._sync(row)
        return done

    def _sync(self, row) -> bool:
        try:
            deal = self.b24.result("crm.deal.get", {"id": row["deal_id"]})
            current = str(deal.get("STAGE_ID", "")).split(":")[-1]
            if current in STAGE_ORDER and STAGE_ORDER.index(current) < STAGE_ORDER.index(PAID_STAGE):
                self.b24.result("crm.deal.update", {"id": row["deal_id"], "fields": {"STAGE_ID": f"C{self.category_id}:{PAID_STAGE}"}})
            self._comment(row["deal_id"], f"Получена оплата {row['amount']} ₽, платёж {row['id']}")
            self.store.mark_synced(row["id"])
            self.store.log(row["id"], "crm_synced")
            return True
        except Exception as exc:  # оплата уже в журнале, повторим позже. Текст ошибки не пишем: в нём может быть адрес вебхука
            self.store.mark_failed(row["id"], type(exc).__name__)
            self.store.log(row["id"], "crm_failed", type(exc).__name__)
            return False

    def _comment(self, deal_id, text):
        self.b24.result("crm.timeline.comment.add", {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": text}})


def export_for_1c(store: Store) -> str:
    """CSV с оплатами для загрузки в 1С: дата, сумма, номер платежа, сделка."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Дата", "Сумма", "Номер платежа", "Сделка", "Назначение"])
    for row in store.paid():
        writer.writerow([row["paid_at"][:10], row["amount"].replace(".", ","), row["id"], row["deal_id"], f"Предоплата по сделке {row['deal_id']}"])
    return out.getvalue()


def reconcile(store: Store, kassa_payments: list[dict]) -> dict:
    """Сверка оплат ЮKassa с журналом: чего не хватает у нас, чего нет в кассе, где не сошлась сумма, что не дошло до CRM."""
    kassa = {p["id"]: money(p["amount"]["value"]) for p in kassa_payments if p.get("status") == "succeeded"}
    ours = {r["id"]: r["amount"] for r in store.paid()}
    return {
        "нет_в_журнале": sorted(set(kassa) - set(ours)),
        "нет_в_кассе": sorted(set(ours) - set(kassa)),
        "разная_сумма": sorted(k for k in set(kassa) & set(ours) if kassa[k] != ours[k]),
        "не_дошло_до_crm": sorted(r["id"] for r in store.unsynced()),
    }

"""Обработчик заявок с сайта: форма шлёт сюда JSON, сервер проверяет данные и создаёт сделку в Битрикс24.
Адрес вебхука остаётся на сервере и не попадает в код сайта.

Запуск: B24_WEBHOOK=... ALLOWED_ORIGIN=https://site.ru python form_handler.py
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from import_clients import clean_phone

MAX_BODY = 4096
RATE_LIMIT, RATE_WINDOW = 5, 60  # не больше 5 заявок в минуту с одного адреса
OBJECTS = {"Новостройка": "Квартира", "Вторичка": "Квартира", "Дом": "Дом"}
log = logging.getLogger("form_handler")


class BadLead(ValueError):
    """Заявка с ошибкой: текст идёт посетителю, поэтому в нём нет внутренних подробностей."""


def parse_lead(data: dict) -> dict:
    if not isinstance(data, dict):
        raise BadLead("Неверный формат заявки")
    name = str(data.get("name") or "").strip()[:100]
    phone = clean_phone(data.get("phone"))
    if not name:
        raise BadLead("Укажите имя")
    if not phone:
        raise BadLead("Проверьте номер телефона")
    return {"name": name, "phone": phone, "object": str(data.get("object") or "").strip()[:40]}


def create_deal(b24, lead: dict, category_id: int, object_ids: dict) -> tuple[int, bool]:
    """Создаёт сделку на первой стадии. Контакт с таким телефоном не дублирует. Возвращает (id сделки, контакт уже был)."""
    found = b24.result("crm.duplicate.findbycomm", {"entity_type": "CONTACT", "type": "PHONE", "values": [lead["phone"]]})
    contacts = found.get("CONTACT") if found else None
    known = bool(contacts)
    if known:
        contact = int(contacts[0])
    else:
        # Порядок имени и фамилии в форме угадать нельзя, поэтому сохраняем как ввели: менеджер уточнит при звонке
        contact = int(b24.result("crm.contact.add", {"fields": {"NAME": lead["name"], "SOURCE_ID": "WEB", "OPENED": "Y", "PHONE": [{"VALUE": lead["phone"], "VALUE_TYPE": "MOBILE"}]}}))
    kind = OBJECTS.get(lead["object"])
    fields = {"TITLE": f"Заявка с сайта · {lead['name']}", "CATEGORY_ID": category_id, "STAGE_ID": f"C{category_id}:NEW", "CONTACT_ID": contact,
              "SOURCE_ID": "WEB", "OPENED": "Y", "COMMENTS": f"Форма «Вызвать замерщика». Тип жилья: {lead['object'] or 'не указан'}" + (". Повторное обращение" if known else "")}
    if kind and kind in object_ids:
        fields["UF_CRM_OBJECT_TYPE"] = object_ids[kind]
    return int(b24.result("crm.deal.add", {"fields": fields})), known


class RateLimiter:
    def __init__(self, limit: int = RATE_LIMIT, window: int = RATE_WINDOW):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def make_handler(b24, category_id: int, object_ids: dict, origin: str, limiter: RateLimiter):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(204, {})

        def do_POST(self):
            if self.path != "/lead":
                return self._send(404, {"ok": False, "error": "Не найдено"})
            if not limiter.allow(self.client_address[0]):
                return self._send(429, {"ok": False, "error": "Слишком много заявок, попробуйте через минуту"})
            size = int(self.headers.get("Content-Length") or 0)
            if size <= 0 or size > MAX_BODY:
                return self._send(400, {"ok": False, "error": "Неверный формат заявки"})
            try:
                data = json.loads(self.rfile.read(size))
                if isinstance(data, dict) and data.get("website"):  # скрытое поле заполняют только боты: отвечаем как обычно и ничего не создаём
                    return self._send(200, {"ok": True})
                lead = parse_lead(data)
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "error": "Неверный формат заявки"})
            except BadLead as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            try:
                deal, known = create_deal(b24, lead, category_id, object_ids)
                log.info("сделка %s, контакт %s", deal, "уже был" if known else "новый")
                return self._send(200, {"ok": True})
            except Exception:  # посетителю без подробностей: в тексте ошибки могут быть внутренние данные
                log.exception("не создалась сделка")
                return self._send(502, {"ok": False, "error": "Не получилось отправить заявку, позвоните нам"})

        def log_message(self, *_):  # телефоны и адреса в журнал не пишем
            pass

    return Handler


def main() -> None:
    from b24 import B24
    from import_clients import FUNNEL_NAME, enum_ids

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    b24 = B24()
    category = next(int(c["id"]) for c in b24.result("crm.category.list", {"entityTypeId": 2})["categories"] if c["name"] == FUNNEL_NAME)
    objects = enum_ids(b24).get("UF_CRM_OBJECT_TYPE", {})
    origin = os.environ.get("ALLOWED_ORIGIN", "http://127.0.0.1:8771")
    port = int(os.environ.get("PORT", "8770"))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(b24, category, objects, origin, RateLimiter()))
    log.info("обработчик заявок на 127.0.0.1:%s, разрешён сайт %s", port, origin)
    server.serve_forever()


if __name__ == "__main__":
    main()

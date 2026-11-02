"""HTTP-сервер оплат. POST /yookassa/webhook принимает уведомления, POST /deals/<id>/pay создаёт ссылку (нужен заголовок X-Api-Token).

Запуск: B24_WEBHOOK=... YOOKASSA_SHOP_ID=... YOOKASSA_SECRET=... PAY_API_TOKEN=... RETURN_URL=https://site.ru/thanks python server.py
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from service import PaymentError, PaymentService, Store, TemporaryError

MAX_BODY = 65536
log = logging.getLogger("payments")


def make_handler(service: PaymentService, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                return self._send(413, {"ok": False})
            raw = self.rfile.read(size) if size else b""
            if self.path == "/yookassa/webhook":
                return self._webhook(raw)
            match = re.fullmatch(r"/deals/(\d+)/pay", self.path)
            if not match:
                return self._send(404, {"ok": False})
            if not token or not hmac.compare_digest(self.headers.get("X-Api-Token", ""), token):
                return self._send(403, {"ok": False})
            try:
                return self._send(200, {"ok": True, "url": service.create_link(int(match.group(1)))})
            except PaymentError as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            except Exception:
                log.exception("ссылка не создана")
                return self._send(502, {"ok": False, "error": "Сервис оплаты недоступен"})

        def _webhook(self, raw: bytes):
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return self._send(400, {"ok": False})
            try:
                log.info("уведомление: %s", service.handle_notification(body))
                return self._send(200, {"ok": True})
            except TemporaryError:  # ответ с ошибкой: ЮKassa пришлёт уведомление ещё раз
                return self._send(503, {"ok": False})

        def log_message(self, *_):
            pass

    return Handler


def main() -> None:
    from b24 import B24
    from yookassa import YooKassa

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    b24 = B24()
    category = next(int(c["id"]) for c in b24.result("crm.category.list", {"entityTypeId": 2})["categories"] if c["name"] == "Ремонт под ключ")
    service = PaymentService(b24, YooKassa(), Store(os.environ.get("PAY_DB", "payments.db")), category, os.environ["RETURN_URL"])

    def retry_loop():
        while True:
            time.sleep(300)
            try:
                service.retry_pending()
            except Exception:
                log.exception("повтор не прошёл")

    threading.Thread(target=retry_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8772"))), make_handler(service, os.environ["PAY_API_TOKEN"])).serve_forever()


if __name__ == "__main__":
    main()

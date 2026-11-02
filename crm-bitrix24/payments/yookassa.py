"""Клиент API ЮKassa v3. Ключи берутся из окружения, для проверки нужен тестовый магазин."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.yookassa.ru/v3"


class KassaError(RuntimeError):
    pass


class YooKassa:
    def __init__(self, shop_id: str | None = None, secret: str | None = None, api: str = API, retries: int = 3):
        shop_id = shop_id or os.environ["YOOKASSA_SHOP_ID"]
        secret = secret or os.environ["YOOKASSA_SECRET"]
        self.auth = base64.b64encode(f"{shop_id}:{secret}".encode()).decode()
        self.api, self.retries = api, retries

    def _call(self, method: str, path: str, body: dict | None = None, key: str | None = None) -> dict:
        headers = {"Authorization": f"Basic {self.auth}", "Content-Type": "application/json"}
        if key:
            headers["Idempotence-Key"] = key
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.retries):
            req = urllib.request.Request(self.api + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code < 500 and exc.code != 429:
                    raise KassaError(f"{method} {path}: {exc.code}") from None
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(2 ** attempt)
        raise KassaError(f"{method} {path}: нет ответа")

    def create_payment(self, amount: str, description: str, return_url: str, metadata: dict, idempotence_key: str) -> dict:
        return self._call("POST", "/payments", {"amount": {"value": amount, "currency": "RUB"}, "capture": True, "description": description,
                                                "metadata": metadata, "confirmation": {"type": "redirect", "return_url": return_url}}, idempotence_key)

    def get_payment(self, payment_id: str) -> dict:
        return self._call("GET", f"/payments/{payment_id}")

    def list_payments(self, status: str = "succeeded") -> list[dict]:
        return self._call("GET", f"/payments?status={status}&limit=100").get("items", [])

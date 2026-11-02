"""Минимальный клиент REST API Битрикс24: повторы при сбоях и соблюдение лимита запросов.
Адрес вебхука берётся из переменной B24_WEBHOOK или файла .env, в репозиторий он не попадает."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILES = (Path(__file__).with_name(".env"), Path(__file__).parent.parent.parent / ".env")
RETRY_ERRORS = {"QUERY_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR", "OPERATION_TIME_LIMIT"}


class B24Error(RuntimeError):
    pass


def webhook_url() -> str:
    url = os.environ.get("B24_WEBHOOK", "")
    for env in ENV_FILES:
        if url:
            break
        if env.exists():
            m = re.search(r"^B24_WEBHOOK=(.*)$", env.read_text(encoding="utf-8-sig"), re.M)
            url = m.group(1).strip() if m else ""
    if not url:
        raise SystemExit("Не задан B24_WEBHOOK")
    return url.rstrip("/") + "/"


class B24:
    def __init__(self, url: str | None = None, pause: float = 0.5, retries: int = 4):
        self.url, self.pause, self.retries = url or webhook_url(), pause, retries

    def call(self, method: str, params: dict | None = None) -> dict:
        body = json.dumps(params or {}).encode()
        for attempt in range(self.retries + 1):
            time.sleep(self.pause)  # лимит портала: около 2 запросов в секунду
            req = urllib.request.Request(self.url + method + ".json", data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                data = json.loads(exc.read() or b"{}")
                if data.get("error") not in RETRY_ERRORS and exc.code < 500:
                    # В текст ошибки адрес вебхука не включаем: в нём ключ доступа
                    raise B24Error(f"{method}: {data.get('error')}: {data.get('error_description')}") from None
            except (urllib.error.URLError, TimeoutError):
                pass
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise B24Error(f"{method}: нет ответа после {self.retries + 1} попыток")

    def result(self, method: str, params: dict | None = None):
        return self.call(method, params)["result"]

    def all(self, method: str, params: dict | None = None) -> list:
        """Постраничная выборка: API отдаёт по 50 записей."""
        out, start = [], 0
        while True:
            data = self.call(method, {**(params or {}), "start": start})
            out += data["result"]
            if "next" not in data:
                return out
            start = data["next"]

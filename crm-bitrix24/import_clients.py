"""Перенос клиентов из Excel в Битрикс24: чистка, поиск дублей, контакты и сделки, отчёт о переносе.
python import_clients.py clients_old.xlsx            проверка, в CRM ничего не пишется
python import_clients.py clients_old.xlsx --apply    перенос. Повторный запуск не создаёт дублей"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter

from openpyxl import load_workbook

FUNNEL_NAME = "Ремонт под ключ"
OBJECTS = [("кварт", "Квартира"), ("кв", "Квартира"), ("дом", "Дом"), ("офис", "Офис"), ("помещ", "Коммерческое помещение")]
REPAIRS = [("косм", "Косметический"), ("кап", "Капитальный"), ("диз", "Дизайнерский"), ("черн", "Черновая отделка")]
STAGES = [("нов", "NEW"), ("звон", "CONTACTED"), ("был замер", "VISIT_DONE"), ("замер", "VISIT_SET"), ("смет", "ESTIMATE"), ("договор", "CONTRACT"),
          ("предопл", "PREPAID"), ("в работе", "WORKS"), ("сдан", "WON"), ("отказ", "LOSE"), ("дорог", "LOSE")]
SOURCES = [("сайт", "WEB"), ("авито", "ADVERTISING"), ("яндекс", "ADVERTISING"), ("реком", "RECOMMENDATION"), ("сараф", "RECOMMENDATION")]
KNOWN_FIRST = {"Анна", "Ирина", "Мария", "Елена", "Ольга", "Сергей", "Андрей", "Дмитрий", "Алексей", "Павел", "Наталья", "Виктор"}


def clean_phone(raw) -> str | None:
    """+7XXXXXXXXXX или None. Из нескольких номеров берём первый."""
    first = re.split(r"[;,/]", str(raw or ""))[0]
    d = re.sub(r"\D", "", first)
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    if len(d) != 10 or d[0] not in "3489" or d == "9000000000":
        return None
    return "+7" + d


def clean_email(raw) -> str | None:
    mail = str(raw or "").strip().lower()
    return mail if re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[a-z]{2,}", mail) else None


def clean_name(raw) -> tuple[str, str]:
    """(имя, фамилия). Порядок в таблице разный: имя определяем по списку частых имён."""
    text = " ".join(str(raw or "").split())
    if re.match(r"(ооо|ип|ао|зао)\s", text, re.I):  # компанию не разбираем как ФИО и не меняем регистр
        return text, ""
    parts = [w.capitalize() for w in text.split()]
    if len(parts) == 2:
        return (parts[0], parts[1]) if parts[0] in KNOWN_FIRST else (parts[1], parts[0])
    return " ".join(parts), ""


def clean_amount(raw) -> int | None:
    s = str(raw or "").lower().replace(" ", "").replace("\xa0", "").replace(",", ".")
    m = re.match(r"\d+(\.\d+)?", s)
    if not m:
        return None
    n = float(m.group())
    if "млн" in s:
        n *= 1_000_000
    elif "т.р" in s or "тыс" in s:
        n *= 1000
    return int(n)


def clean_area(raw) -> float | None:
    m = re.match(r"\d+([.,]\d+)?", str(raw or "").strip())
    return float(m.group().replace(",", ".")) if m else None


def match(raw, table, default=None):
    low = str(raw or "").strip().lower()
    return next((value for key, value in table if key in low), default) if low else default


def prepare(rows: list) -> tuple[list[dict], list[dict]]:
    """Чистые клиенты (повторы слиты по телефону или почте) и отклонённые строки с причиной."""
    clients, rejected, index = [], [], {}
    for n, row in rows:
        if not any(str(v or "").strip() for v in row.values()):
            continue
        phone, email = clean_phone(row["Телефон"]), clean_email(row["Email"])
        name = str(row["ФИО"] or "").strip()
        reason = None
        if "тест" in f"{name} {row['Комментарий'] or ''}".lower():
            reason = "тестовая запись"
        elif not name:
            reason = "нет ФИО"
        elif not phone and not email:
            reason = "неверный телефон" if str(row["Телефон"] or "").strip() else "нет телефона и почты"
        if reason:
            rejected.append({"строка": n, "ФИО": row["ФИО"], "телефон": row["Телефон"], "причина": reason})
            continue
        first, last = clean_name(name)
        item = {"rows": [n], "first": first, "last": last, "phone": phone, "email": email, "address": row["Адрес"],
                "object": match(row["Объект"], OBJECTS), "repair": match(row["Ремонт"], REPAIRS), "area": clean_area(row["Площадь"]),
                "stage": match(row["Статус"], STAGES, "NEW"), "amount": clean_amount(row["Сумма"]),
                "source": match(row["Откуда"], SOURCES, "OTHER"), "lost": "Дорого" if "дорог" in str(row["Статус"] or "").lower() else None,
                "comment": str(row["Комментарий"] or "").strip()}
        key = phone or email
        if key in index:  # повтор: дополняем пустые поля, новые данные не затирают старые
            base = index[key]
            base["rows"].append(n)
            for field in ("email", "address", "object", "repair", "area", "amount", "comment"):
                base[field] = base[field] or item[field]
            continue
        index[key] = item
        clients.append(item)
    return clients, rejected


def read_rows(path: str) -> list:
    ws = load_workbook(path, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    return [(i, dict(zip(rows[0], r))) for i, r in enumerate(rows[1:], 2)]


def find_contact(b24, client) -> int | None:
    for kind, value in (("PHONE", client["phone"]), ("EMAIL", client["email"])):
        if value:
            found = b24.result("crm.duplicate.findbycomm", {"entity_type": "CONTACT", "type": kind, "values": [value]})
            if found and found.get("CONTACT"):
                return int(found["CONTACT"][0])
    return None


def enum_ids(b24) -> dict:
    """Значения списков в Битрикс24 задаются по id: {поле: {текст: id}}."""
    out = {}
    for f in b24.all("crm.deal.userfield.list", {"filter": {"USER_TYPE_ID": "enumeration"}}):
        full = b24.result("crm.deal.userfield.get", {"id": f["ID"]})
        out[full["FIELD_NAME"]] = {x["VALUE"]: x["ID"] for x in full.get("LIST", [])}
    return out


def upload(b24, clients: list[dict]) -> Counter:
    cat = next(int(c["id"]) for c in b24.result("crm.category.list", {"entityTypeId": 2})["categories"] if c["name"] == FUNNEL_NAME)
    enums = enum_ids(b24)
    stats = Counter()
    for c in clients:
        contact = find_contact(b24, c)
        if contact is None:
            fields = {"NAME": c["first"], "LAST_NAME": c["last"], "ADDRESS": c["address"] or "", "SOURCE_ID": c["source"], "OPENED": "Y"}
            if c["phone"]:
                fields["PHONE"] = [{"VALUE": c["phone"], "VALUE_TYPE": "MOBILE"}]
            if c["email"]:
                fields["EMAIL"] = [{"VALUE": c["email"], "VALUE_TYPE": "WORK"}]
            contact = int(b24.result("crm.contact.add", {"fields": fields}))
            stats["контактов создано"] += 1
        else:
            stats["контактов уже было"] += 1
        origin = c["phone"] or c["email"]
        if b24.result("crm.deal.list", {"filter": {"ORIGINATOR_ID": "clients_xlsx", "ORIGIN_ID": origin}, "select": ["ID"]}):
            stats["сделок уже было"] += 1
            continue
        title = " ".join(x for x in [c["object"] or "Объект", c["address"] or "", "·", c["last"] or c["first"]] if x)
        deal = {"TITLE": title, "CATEGORY_ID": cat, "STAGE_ID": f"C{cat}:{c['stage']}", "CONTACT_ID": contact, "SOURCE_ID": c["source"],
                "CURRENCY_ID": "RUB", "ORIGINATOR_ID": "clients_xlsx", "ORIGIN_ID": origin, "COMMENTS": c["comment"], "OPENED": "Y",
                "UF_CRM_OBJECT_ADDRESS": c["address"] or "", "UF_CRM_AREA_M2": c["area"]}
        if c["amount"]:
            deal["OPPORTUNITY"] = c["amount"]
        for field, value in (("UF_CRM_OBJECT_TYPE", c["object"]), ("UF_CRM_REPAIR_TYPE", c["repair"]), ("UF_CRM_LOST_REASON", c["lost"])):
            if value:
                deal[field] = enums[field][value]
        b24.result("crm.deal.add", {"fields": deal})
        stats["сделок создано"] += 1
    return stats


def report(rows, clients, rejected, stats=None) -> None:
    merged = sum(len(c["rows"]) - 1 for c in clients)
    with open("import_rejected.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, ["строка", "ФИО", "телефон", "причина"], delimiter=";")
        w.writeheader()
        w.writerows(rejected)
    filled = sum(1 for _, r in rows if any(str(v or "").strip() for v in r.values()))
    print(f"Строк с данными: {filled}\nКлиентов к переносу: {len(clients)}\nПовторов слито: {merged}\nОтклонено: {len(rejected)} (import_rejected.csv)")
    for reason, n in Counter(r["причина"] for r in rejected).items():
        print(f"  {reason}: {n}")
    print("Стадии:", dict(Counter(c["stage"] for c in clients)))
    for k, v in (stats or {}).items():
        print(f"{k}: {v}")


def main() -> None:
    rows = read_rows(sys.argv[1])
    clients, rejected = prepare(rows)
    stats = None
    if "--apply" in sys.argv:
        from b24 import B24
        stats = upload(B24(), clients)
    report(rows, clients, rejected, stats)


if __name__ == "__main__":
    main()

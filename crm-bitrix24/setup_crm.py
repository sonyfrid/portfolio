"""Настройка CRM под ремонтную компанию: воронка, стадии, поля сделки. Повторный запуск ничего не дублирует."""
from __future__ import annotations

from b24 import B24

FUNNEL = "Ремонт под ключ"
# (код стадии, название, семантика: P идёт, S успех, F провал)
STAGES = [
    ("NEW", "Новая заявка", "P"), ("CONTACTED", "Связались", "P"), ("VISIT_SET", "Замер назначен", "P"),
    ("VISIT_DONE", "Замер проведён", "P"), ("ESTIMATE", "Смета на согласовании", "P"), ("CONTRACT", "Договор", "P"),
    ("PREPAID", "Предоплата получена", "P"), ("WORKS", "Работы идут", "P"), ("HANDOVER", "Сдача объекта", "P"),
    ("WON", "Объект сдан", "S"), ("LOSE", "Отказ", "F"),
]
FIELDS = [
    ("OBJECT_TYPE", "Тип объекта", "enumeration", ["Квартира", "Дом", "Офис", "Коммерческое помещение"]),
    ("REPAIR_TYPE", "Вид ремонта", "enumeration", ["Косметический", "Капитальный", "Дизайнерский", "Черновая отделка"]),
    ("AREA_M2", "Площадь, м²", "double", None),
    ("OBJECT_ADDRESS", "Адрес объекта", "string", None),
    ("VISIT_AT", "Дата замера", "datetime", None),
    ("LOST_REASON", "Причина отказа", "enumeration", ["Дорого", "Выбрали другую компанию", "Перенесли ремонт", "Не вышли на связь", "Не наш объём работ"]),
]


def ensure_funnel(b24: B24) -> int:
    for cat in b24.result("crm.category.list", {"entityTypeId": 2})["categories"]:
        if cat["name"] == FUNNEL:
            return int(cat["id"])
    return int(b24.result("crm.category.add", {"entityTypeId": 2, "fields": {"name": FUNNEL}})["category"]["id"])


def ensure_stages(b24: B24, cat_id: int) -> None:
    entity = f"DEAL_STAGE_{cat_id}"
    current = {s["STATUS_ID"].split(":")[-1]: s for s in b24.all("crm.status.list", {"filter": {"ENTITY_ID": entity}})}
    wanted = {code for code, _, _ in STAGES}
    # Сначала финальные стадии: Битрикс не даёт создать промежуточную стадию после успешной
    for i, (code, name, _) in sorted(enumerate(STAGES, 1), key=lambda x: x[1][2] == "P"):
        sort = i * 10
        if code in current:
            if current[code]["NAME"] != name or int(current[code]["SORT"]) != sort:
                b24.result("crm.status.update", {"id": current[code]["ID"], "fields": {"NAME": name, "SORT": sort}})
        else:
            b24.result("crm.status.add", {"fields": {"ENTITY_ID": entity, "STATUS_ID": f"C{cat_id}:{code}", "NAME": name, "SORT": sort}})
    for code, stage in current.items():
        if code not in wanted and stage.get("SYSTEM") != "Y":
            b24.result("crm.status.delete", {"id": stage["ID"]})


def ensure_fields(b24: B24) -> None:
    have = {f["FIELD_NAME"] for f in b24.all("crm.deal.userfield.list")}
    for name, label, kind, values in FIELDS:
        if f"UF_CRM_{name}" in have:
            continue
        fields = {"FIELD_NAME": name, "USER_TYPE_ID": kind, "EDIT_FORM_LABEL": label, "LIST_COLUMN_LABEL": label, "LIST_FILTER_LABEL": label}
        if values:
            fields["LIST"] = [{"VALUE": v, "SORT": (i + 1) * 10} for i, v in enumerate(values)]
        b24.result("crm.deal.userfield.add", {"fields": fields})


def main() -> None:
    b24 = B24()
    cat_id = ensure_funnel(b24)
    ensure_stages(b24, cat_id)
    ensure_fields(b24)
    print(f"Воронка «{FUNNEL}», id {cat_id}")
    for s in sorted(b24.all("crm.status.list", {"filter": {"ENTITY_ID": f"DEAL_STAGE_{cat_id}"}}), key=lambda x: int(x["SORT"])):
        print(f"  {s['SORT']:>4} {s['STATUS_ID']:<16} {s['NAME']} {'[системная]' if s.get('SYSTEM') == 'Y' else ''}")
    print("Поля:", ", ".join(f["FIELD_NAME"] for f in b24.all("crm.deal.userfield.list")))


if __name__ == "__main__":
    main()

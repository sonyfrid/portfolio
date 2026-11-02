"""Таблица клиентов ремонтной компании в том виде, в каком её обычно ведут вручную: clients_old.xlsx."""
import random

from openpyxl import Workbook

random.seed(20261102)
FIRST = ["Анна", "Ирина", "Мария", "Елена", "Ольга", "Сергей", "Андрей", "Дмитрий", "Алексей", "Павел", "Наталья", "Виктор"]
LAST = ["Смирнов", "Кузнецов", "Попов", "Соколов", "Лебедев", "Новиков", "Морозов", "Волков", "Орлов", "Зайцев"]
FEMALE = {"Анна", "Ирина", "Мария", "Елена", "Ольга", "Наталья"}
STREETS = ["Ленина", "Мира", "Садовая", "Парковая", "Заречная", "Лесная"]
OBJECTS = ["квартира", "Квартира", "кв.", "дом", "Дом", "офис", "помещение"]
REPAIRS = ["косметика", "Косметический", "капиталка", "Капитальный", "дизайнерский", "черновая"]
STATUSES = ["новый", "Новая заявка", "звонили", "замер", "был замер", "смета", "договор", "предоплата", "в работе", "сдан", "отказ", "дорого", ""]
SOURCES = ["сайт", "Сайт", "авито", "Авито", "рекомендация", "сарафан", "яндекс", ""]


def phone(i: int) -> str:
    digits = f"900{1000000 + i * 137:07d}"
    a, b, c, d = digits[:3], digits[3:6], digits[6:8], digits[8:]
    return random.choice([f"+7{digits}", f"8{digits}", f"8 ({a}) {b}-{c}-{d}", f"+7 {a} {b} {c} {d}", digits, f"8-{a}-{b}-{c}{d}"])


def name() -> str:
    first, last = random.choice(FIRST), random.choice(LAST)
    last += "а" if first in FEMALE else ""
    return random.choice([f"{last} {first}", f"{first} {last}", f"{last.upper()} {first}", f"  {last.lower()}  {first.lower()} "])


def amount():
    n = random.randrange(150, 2500, 10) * 1000
    return random.choice([n, f"{n // 1000} 000", f"{n // 1000} т.р.", f"{n / 1_000_000:.1f} млн".replace(".", ","), f"{n} руб", ""])


rows = []
for i in range(84):
    mail = random.choice(["", "", f"client{i}@example.com", f"CLIENT{i}@EXAMPLE.COM ", f"client{i}@example,com"])
    rows.append([name(), phone(i), mail, f"ул. {random.choice(STREETS)}, {random.randint(1, 90)}", random.choice(OBJECTS),
                 random.choice([random.randint(25, 180), f"{random.randint(25, 180)} м2", f"{random.randint(25, 95)},5", ""]),
                 random.choice(REPAIRS), random.choice(STATUSES), amount(), random.choice(SOURCES), ""])
# Что встречается в живых таблицах: повторы, пустые контакты, мусор
for src in random.sample(range(84), 9):
    dup = list(rows[src])
    dup[0], dup[10] = dup[0].strip().title(), "повторно обратился"
    rows.insert(random.randrange(len(rows)), dup)
rows += [["Тест Тестович", "+79000000000", "test@test.ru", "", "", "", "", "", "", "", "тест менеджера"],
         ["Петрова Ольга", "", "", "ул. Мира, 4", "квартира", 54, "косметика", "звонили", "", "сайт", "телефон потеряли"],
         ["Иванов", "12345", "", "", "дом", "", "", "новый", "", "", ""],
         ["Орлов Виктор", "8 900 123", "", "", "офис", "", "", "", "", "", "номер не дописан"],
         ["", "", "", "", "", "", "", "", "", "", ""],
         ["ООО Ромашка (Елена)", "+7 900 777-12-34; +7 900 777-12-35", "", "", "офис", 210, "капиталка", "смета", "2,4 млн", "яндекс", "два номера"]]
wb = Workbook()
ws = wb.active
ws.title = "Клиенты"
ws.append(["ФИО", "Телефон", "Email", "Адрес", "Объект", "Площадь", "Ремонт", "Статус", "Сумма", "Откуда", "Комментарий"])
for row in rows:
    ws.append(row)
wb.save("clients_old.xlsx")
print("строк:", len(rows))

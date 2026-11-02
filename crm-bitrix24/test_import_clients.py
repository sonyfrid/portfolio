from import_clients import clean_amount, clean_area, clean_email, clean_name, clean_phone, prepare


def test_phone_formats_become_one():
    for raw in ("+79001234567", "89001234567", "8 (900) 123-45-67", "9001234567", "+7 900 123 45 67; +7 900 123 45 68"):
        assert clean_phone(raw) == "+79001234567"


def test_bad_phones_rejected():
    for raw in ("12345", "8 900 123", "", None, "+79000000000"):
        assert clean_phone(raw) is None


def test_email():
    assert clean_email(" CLIENT@EXAMPLE.COM ") == "client@example.com"
    assert clean_email("client@example,com") is None


def test_name_order():
    assert clean_name("СМИРНОВА Анна") == ("Анна", "Смирнова")
    assert clean_name("Сергей Волков") == ("Сергей", "Волков")
    assert clean_name("ООО Ромашка (Елена)") == ("ООО Ромашка (Елена)", "")


def test_amount():
    assert clean_amount("2,4 млн") == 2_400_000
    assert clean_amount("350 т.р.") == 350_000
    assert clean_amount("350000 руб") == 350_000
    assert clean_amount("") is None


def test_area():
    assert clean_area("54,5") == 54.5
    assert clean_area("120 м2") == 120.0


def _row(**kw):
    base = dict.fromkeys(["ФИО", "Телефон", "Email", "Адрес", "Объект", "Площадь", "Ремонт", "Статус", "Сумма", "Откуда", "Комментарий"])
    return {**base, **kw}


def test_duplicates_merge_and_fill_blanks():
    rows = [(2, _row(ФИО="Анна Попова", Телефон="89001112233")), (3, _row(ФИО="Попова Анна", Телефон="+7 900 111-22-33", Сумма="500 т.р."))]
    clients, rejected = prepare(rows)
    assert len(clients) == 1 and not rejected
    assert clients[0]["rows"] == [2, 3] and clients[0]["amount"] == 500_000


def test_rejected_have_reasons():
    rows = [(2, _row(ФИО="Тест Тестович", Телефон="+79001112233")), (3, _row(ФИО="Иванов", Телефон="12345")), (4, _row(ФИО="Петрова Ольга"))]
    _, rejected = prepare(rows)
    assert [r["причина"] for r in rejected] == ["тестовая запись", "неверный телефон", "нет телефона и почты"]

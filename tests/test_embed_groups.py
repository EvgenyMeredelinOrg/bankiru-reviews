from types import SimpleNamespace

from bankiru.embedder import passages_for_new_rows


def test_sibling_products_share_one_passage() -> None:
    rows = [
        SimpleNamespace(
            id=1, url="https://www.banki.ru/services/responses/bank/response/1",
            bankName="B", product="Дебетовая карта", location="Город", reviewBody="text",
        ),
        SimpleNamespace(
            id=2, url="https://www.banki.ru/services/responses/bank/response/1",
            bankName="B", product="Ипотека", location="Город", reviewBody="text",
        ),
    ]
    texts, groups = passages_for_new_rows(rows)
    assert len(texts) == 1
    assert "Дебетовая карта" in texts[0]
    assert groups == [[1, 2]]


def test_different_location_is_a_second_passage() -> None:
    rows = [
        SimpleNamespace(
            id=1, url="https://www.banki.ru/services/responses/bank/response/1",
            bankName="B", product="Дебетовая карта", location="Город", reviewBody="text",
        ),
        SimpleNamespace(
            id=2, url="https://www.banki.ru/services/responses/bank/response/1",
            bankName="B", product="Дебетовая карта", location="Другой", reviewBody="text",
        ),
    ]
    texts, groups = passages_for_new_rows(rows)
    assert len(texts) == 2
    assert groups == [[1], [2]]

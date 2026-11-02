from bankiru.api.summarizer import _budgets
from bankiru.config import get_settings


def test_output_budget_is_8192(monkeypatch) -> None:
    monkeypatch.setenv("OUTPUT_TOKENS_LIMIT", "8192")
    get_settings.cache_clear()
    try:
        input_budget, output_budget = _budgets(200_000, 100)
    finally:
        get_settings.cache_clear()
    assert output_budget == 8192
    assert input_budget == 200_000 - 100 - 8192 - 512

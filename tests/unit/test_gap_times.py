"""知识缺口首末时间：query_runs 倒序返回时也不能把 first/last 弄反。"""
from app.core.knowledge_gaps import aggregate_gaps


def test_first_and_last_are_min_max():
    runs = [
        {"question": "HAK180 能连 WiFi 吗", "outcome": "no_match",
         "datetime": "2026-11-03 12:00:00"},
        {"question": "HAK180 能连 WiFi 吗", "outcome": "no_match",
         "datetime": "2026-11-01 10:00:00"},
        {"question": "HAK180 能连 WiFi 吗", "outcome": "no_match",
         "datetime": "2026-11-02 11:00:00"},
    ]

    gaps = aggregate_gaps(runs)

    assert len(gaps) == 1
    assert gaps[0]["first_at"] == "2026-11-01 10:00:00"
    assert gaps[0]["last_at"] == "2026-11-03 12:00:00"

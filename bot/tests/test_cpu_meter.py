from bot.cpu_meter import busy_between, report


def test_busy_and_held_back_between_two_readings():
    prev = (1_000_000, 0, 100, 0)
    cur = (1_500_000, 200_000, 200, 25)
    out = busy_between(prev, cur, 1.0, 1.0)
    assert out["busy_percent"] == 50.0
    assert out["held_back_percent"] == 25.0


def test_unknown_counters_say_nothing():
    out = busy_between((None, None, None, None), (None, None, None, None), 5.0, None)
    assert out["busy_percent"] is None and out["held_back_percent"] is None


def test_report_never_raises():
    first = report()
    second = report()
    assert isinstance(first, dict) and isinstance(second, dict)

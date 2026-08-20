from app.engine.priority import choose_source


def test_manual_queue_has_highest_priority():
    src = choose_source(
        manual_count=1, ad_due=False, schedule_ready=True, fallback_ready=True
    )
    assert src == "manual"


def test_fallback_used_when_nothing_else_available():
    src = choose_source(
        manual_count=0, ad_due=False, schedule_ready=False, fallback_ready=True
    )
    assert src == "fallback"


def test_host_queue_has_highest_priority():
    src = choose_source(
        manual_count=1, ad_due=True, schedule_ready=True, fallback_ready=True,
        host_count=1,
    )
    assert src == "host"


def test_host_queue_zero_falls_through():
    src = choose_source(
        manual_count=1, ad_due=False, schedule_ready=False, fallback_ready=False,
        host_count=0,
    )
    assert src == "manual"

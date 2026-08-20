from app.engine.recovery_policy import decide_recovery_action


def test_supervisor_prefers_degrade_before_restart():
    action = decide_recovery_action(component_error=True, recoverable=True)
    assert action == "degrade"


def test_supervisor_uses_restart_only_for_unrecoverable():
    action = decide_recovery_action(component_error=True, recoverable=False)
    assert action == "restart_last_resort"

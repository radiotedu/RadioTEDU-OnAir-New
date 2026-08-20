from app.engine.recovery_policy import decide_recovery_action


def test_component_failure_chooses_degrade_not_restart():
    action = decide_recovery_action(component_error=True, recoverable=True)
    assert action == "degrade"
    assert action != "restart_last_resort"

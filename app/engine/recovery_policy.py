def decide_recovery_action(component_error: bool, recoverable: bool) -> str:
    if not component_error:
        return "none"
    if recoverable:
        return "degrade"
    return "restart_last_resort"

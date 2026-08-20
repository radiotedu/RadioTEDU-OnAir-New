from app.audio.output_health_router import OutputHealthRouter


def test_local_branch_failure_does_not_disable_icecast_branch():
    router = OutputHealthRouter()
    router.set_branch_health("icecast", True)
    router.set_branch_health("local", False)
    assert router.is_output_active("icecast") is True
    assert router.is_output_active("local") is False

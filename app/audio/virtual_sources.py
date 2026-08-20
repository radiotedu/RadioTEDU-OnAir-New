DEFAULT_CONTINUITY_INPUT_URI = "silence://continuous"


def is_silence_input_uri(raw: str) -> bool:
    return str(raw or "").strip().lower().startswith("silence://")

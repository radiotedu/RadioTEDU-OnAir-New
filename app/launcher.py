import time
import webbrowser
from urllib.request import urlopen


def probe_url(url: str, timeout: float = 1.5):
    return urlopen(url, timeout=timeout)


def wait_for_health(url: str, retries: int = 30, delay_sec: float = 0.5) -> bool:
    for _ in range(retries):
        try:
            response = probe_url(url, 1.5)
            if getattr(response, "status", 0) == 200:
                return True
        except OSError:
            pass
        time.sleep(delay_sec)
    return False


def open_panel(url: str) -> None:
    webbrowser.open(url)

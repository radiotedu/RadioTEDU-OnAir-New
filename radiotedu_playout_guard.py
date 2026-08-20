"""Compatibility entry point for the packaged RadioTEDU playout supervisor."""

import sys

from tools import legacy_playout_guard as _implementation

sys.modules[__name__] = _implementation

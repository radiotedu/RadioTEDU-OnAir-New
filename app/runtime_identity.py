from __future__ import annotations

import os
import time
import uuid


BACKEND_INSTANCE_ID = uuid.uuid4().hex
BACKEND_PROCESS_ID = os.getpid()
BACKEND_STARTED_AT_EPOCH = time.time()

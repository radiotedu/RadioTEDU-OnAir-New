"""Minimal third-voter service for RadioTEDU OnAir high availability.

Run on a third host with CLEANROOM_HA_SHARED_SECRET and an isolated
CLEANROOM_DB_PATH, for example: py -m uvicorn app.witness:app --host 0.0.0.0 --port 8110
"""

import os
from contextlib import asynccontextmanager

os.environ.setdefault("CLEANROOM_HA_ENABLED", "1")
os.environ.setdefault("CLEANROOM_HA_WITNESS_ONLY", "1")

from fastapi import FastAPI

from app.api.ha import router as ha_router
from app.db import init_db
from app.version import PRODUCT_VERSION


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="RadioTEDU OnAir HA Witness",
    version=PRODUCT_VERSION,
    lifespan=lifespan,
)
app.include_router(ha_router)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "service": "radiotedu-onair-witness"}

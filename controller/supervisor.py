"""
Supervisor view — a lightweight HTTP API that surfaces the loop's state.
One screen, not fifty alerts. (C11 — minimal implementation)
"""
from __future__ import annotations

import logging
from fastapi import FastAPI

log = logging.getLogger(__name__)

app = FastAPI(title="Controller supervisor view")
_state: dict = {}


def set_state(state: dict) -> None:
    global _state
    _state = state


@app.get("/status")
def status():
    return _state


@app.get("/health")
def health():
    return {"ok": True}

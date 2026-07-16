import threading

_lock = threading.Lock()
_state = {
    "phase": "idle",  # idle | scan | fetch | scan_and_fetch | verify
    "running": False,
    "current": None,
    "processed": 0,
    "total": 0,
    "extra": {},
    "error": None,
}


def get_state() -> dict:
    with _lock:
        return dict(_state)


def start(phase: str):
    with _lock:
        _state.update(
            phase=phase, running=True, current=None, processed=0, total=0, extra={}, error=None
        )


def update(current: str = None, processed: int = None, total: int = None):
    with _lock:
        if current is not None:
            _state["current"] = current
        if processed is not None:
            _state["processed"] = processed
        if total is not None:
            _state["total"] = total


def finish(extra: dict = None):
    with _lock:
        _state["running"] = False
        _state["current"] = None
        if extra is not None:
            _state["extra"] = extra


def fail(error: str):
    with _lock:
        _state["running"] = False
        _state["current"] = None
        _state["error"] = error

# -*- coding: utf-8 -*-
"""Run lifecycle views derived from the ledger plus live controller/lock evidence."""
from pathlib import Path

from atlas.engine import (RunConflictError, acquire_run_lock,
                          release_run_lock)
from atlas.events import fold_events


def derive_run_status(records: list[dict], *, run_id: str, runs_root: Path,
                      active_controller: bool = False) -> str:
    """Return the dynamic run status without persisting synthetic events.

    A persisted ``running`` status becomes ``interrupted`` only when the caller
    has no active local controller and the stable per-run OS lock is provably
    free. Held locks and all probe errors fail closed as ``running``.
    """
    persisted = fold_events(records)["status"]
    if persisted != "running" or active_controller:
        return persisted

    acquired = False
    try:
        acquire_run_lock(run_id, runs_root=Path(runs_root))
        acquired = True
    except (RunConflictError, OSError):
        return "running"
    except Exception:
        return "running"
    finally:
        if acquired:
            release_run_lock(run_id, runs_root=Path(runs_root))
    return "interrupted"

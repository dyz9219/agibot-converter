from __future__ import annotations

import subprocess
import threading

_LOCK = threading.Lock()
_PIDS: set[int] = set()


def register_child(pid: int) -> None:
    if pid <= 0:
        return
    with _LOCK:
        _PIDS.add(pid)


def unregister_child(pid: int) -> None:
    with _LOCK:
        _PIDS.discard(pid)


def snapshot_children() -> list[int]:
    with _LOCK:
        return sorted(_PIDS)


def terminate_all_children(timeout_sec: float = 5.0) -> list[int]:
    pids = snapshot_children()
    if not pids:
        return []
    terminated: list[int] = []
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None

    if psutil is not None:
        procs = []
        for pid in pids:
            try:
                procs.append(psutil.Process(pid))
            except Exception:
                unregister_child(pid)
        for p in procs:
            try:
                for c in p.children(recursive=True):
                    c.terminate()
                p.terminate()
            except Exception:
                pass
        try:
            gone, alive = psutil.wait_procs(procs, timeout=timeout_sec)
            for p in gone:
                terminated.append(p.pid)
                unregister_child(p.pid)
            for p in alive:
                try:
                    p.kill()
                    terminated.append(p.pid)
                    unregister_child(p.pid)
                except Exception:
                    pass
        except Exception:
            pass
        return sorted(set(terminated))

    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            terminated.append(pid)
            unregister_child(pid)
        except Exception:
            pass
    return sorted(set(terminated))


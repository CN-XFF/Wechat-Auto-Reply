import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))

from wechatauto.db import WeChatDB  # noqa: E402


def test_open_serializes_snapshot_builds_for_same_database():
    db = WeChatDB.__new__(WeChatDB)
    db._snapshot_locks = {}
    db._snapshot_locks_guard = threading.Lock()

    state_lock = threading.Lock()
    release_first = threading.Event()
    first_inside = threading.Event()
    second_started = threading.Event()
    active = 0
    peak_active = 0
    results = {}
    errors = []

    def fake_snapshot_build(_rel):
        nonlocal active, peak_active
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
            call_number = active
        if call_number == 1:
            first_inside.set()
        if not release_first.wait(2):
            raise RuntimeError("test did not release snapshot build")
        with state_lock:
            active -= 1
        return "snapshot"

    db._open_with_snapshot_lock = fake_snapshot_build

    def open_snapshot(result_key, started=None):
        try:
            if started:
                started.set()
            results[result_key] = db._open("message_2.db")
        except BaseException as exc:  # 交给主测试线程断言，避免后台异常被吞掉
            errors.append(exc)

    first = threading.Thread(target=open_snapshot, args=("first",))
    second = threading.Thread(
        target=open_snapshot, args=("second", second_started)
    )
    first.start()
    assert first_inside.wait(1)
    second.start()
    assert second_started.wait(1)
    time.sleep(0.05)
    with state_lock:
        assert peak_active == 1

    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert results == {"first": "snapshot", "second": "snapshot"}
    assert peak_active == 1


def test_invalidate_cache_waits_for_inflight_snapshot_build(tmp_path):
    db = WeChatDB.__new__(WeChatDB)
    db.workdir = str(tmp_path)
    snapshot_lock = threading.RLock()
    db._snapshot_locks = {"message_2.db": snapshot_lock}
    db._snapshot_locks_guard = threading.Lock()
    temp_file = tmp_path / "message__message_2.db.tmp"
    temp_file.write_bytes(b"snapshot in progress")

    build_started = threading.Event()
    release_build = threading.Event()
    invalidation_done = threading.Event()
    errors = []

    def build_snapshot():
        with snapshot_lock:
            build_started.set()
            if not release_build.wait(2):
                errors.append("snapshot build was not released")
            if not temp_file.exists():
                errors.append("cache invalidation deleted an in-flight temp file")

    def invalidate_cache():
        db._invalidate_cache()
        invalidation_done.set()

    builder = threading.Thread(target=build_snapshot)
    invalidator = threading.Thread(target=invalidate_cache)
    builder.start()
    assert build_started.wait(1)
    invalidator.start()
    time.sleep(0.05)
    assert not invalidation_done.is_set()
    assert temp_file.exists()

    release_build.set()
    builder.join(2)
    invalidator.join(2)

    assert not builder.is_alive()
    assert not invalidator.is_alive()
    assert not errors
    assert invalidation_done.is_set()
    assert not temp_file.exists()

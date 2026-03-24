# tests/test_file_lock.py
import threading
import pytest
from pathlib import Path
from oci_logan_mcp.file_lock import locked_file, atomic_yaml_write, atomic_yaml_read

class TestLockedFile:
    def test_locked_file_creates_lock_file(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        thread_lock = threading.RLock()
        with locked_file(lock_path, thread_lock):
            assert lock_path.exists()

    def test_locked_file_is_reentrant(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        thread_lock = threading.RLock()
        with locked_file(lock_path, thread_lock):
            with locked_file(lock_path, thread_lock):
                assert True  # No deadlock

    def test_concurrent_writes_are_serialized(self, tmp_path):
        data_file = tmp_path / "data.yaml"
        results = []
        lock_path = tmp_path / "data.lock"
        thread_lock = threading.RLock()

        def writer(value):
            with locked_file(lock_path, thread_lock):
                results.append(value)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == list(range(10))

class TestAtomicYaml:
    def test_write_then_read(self, tmp_path):
        path = tmp_path / "test.yaml"
        data = {"key": "value", "list": [1, 2, 3]}
        atomic_yaml_write(path, data)
        loaded = atomic_yaml_read(path, default={})
        assert loaded == data

    def test_read_missing_file_returns_default(self, tmp_path):
        path = tmp_path / "missing.yaml"
        result = atomic_yaml_read(path, default={"empty": True})
        assert result == {"empty": True}

    def test_write_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "sub" / "dir" / "test.yaml"
        atomic_yaml_write(path, {"nested": True})
        assert path.exists()
        assert atomic_yaml_read(path, default={}) == {"nested": True}

    def test_write_is_atomic_no_partial_reads(self, tmp_path):
        path = tmp_path / "atomic.yaml"
        # Write initial data
        atomic_yaml_write(path, {"version": 1})
        # Overwrite — reader should never see partial
        atomic_yaml_write(path, {"version": 2, "data": "x" * 1000})
        assert atomic_yaml_read(path, default={})["version"] == 2

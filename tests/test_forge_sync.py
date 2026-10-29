"""Unit and integration tests for forge-sync."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from forge_sync.config import RemoteEndpoint, SyncConfig, _parse_simple_yaml_or_json
from forge_sync.engine import SyncEngine
from forge_sync.lock import FileLock, LockTimeoutError


class TestFileLock(unittest.TestCase):
    """Test atomic file locking behavior and stale lock recovery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.temp_dir.name) / "test.lock"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_acquire_and_release(self):
        lock = FileLock(self.lock_path, timeout=1.0)
        self.assertFalse(lock.is_locked)
        lock.acquire(reason="testing")
        self.assertTrue(lock.is_locked)

        meta = lock.read_metadata()
        self.assertIsNotNone(meta)
        self.assertEqual(meta.pid, os.getpid())
        self.assertEqual(meta.reason, "testing")

        lock.release()
        self.assertFalse(lock.is_locked)

    def test_context_manager(self):
        with FileLock(self.lock_path) as lock:
            self.assertTrue(lock.is_locked)
        self.assertFalse(self.lock_path.exists())

    def test_lock_timeout(self):
        lock1 = FileLock(self.lock_path, timeout=5.0)
        lock1.acquire()

        lock2 = FileLock(self.lock_path, timeout=0.2, poll_interval=0.05)
        with self.assertRaises(LockTimeoutError):
            lock2.acquire()

        lock1.release()

    def test_stale_lock_recovery(self):
        # Create an artificial stale lock file with an old timestamp
        stale_meta = {
            "pid": 999999,
            "hostname": "old-host",
            "acquired_at": time.time() - 500.0,
            "reason": "abandoned",
        }
        self.lock_path.write_text(json.dumps(stale_meta), encoding="utf-8")

        # New lock with stale_threshold = 10s should break the lock and succeed
        lock = FileLock(self.lock_path, timeout=1.0, stale_threshold=10.0)
        lock.acquire(reason="fresh")
        self.assertTrue(lock.is_locked)

        fresh_meta = lock.read_metadata()
        self.assertEqual(fresh_meta.pid, os.getpid())
        lock.release()


class TestSyncConfig(unittest.TestCase):
    """Test configuration loading and resolution."""

    def test_from_dict(self):
        data = {
            "repo_name": "hummbl-test",
            "primary": {
                "name": "github",
                "url": "https://github.com/hummbl-dev/test.git",
                "branch_filter": ["main", "v*"],
            },
            "mirrors": [
                {
                    "name": "forgejo",
                    "url": "https://git.hummbl.io/hummbl/test.git",
                }
            ],
            "sync_tags": True,
            "prune": False,
        }
        config = SyncConfig.from_dict(data)
        self.assertEqual(config.repo_name, "hummbl-test")
        self.assertEqual(config.primary.name, "github")
        self.assertEqual(config.primary.branch_filter, ["main", "v*"])
        self.assertEqual(len(config.mirrors), 1)
        self.assertFalse(config.prune)

    def test_auth_env_resolution(self):
        os.environ["TEST_FORGE_TOKEN"] = "secret123"
        try:
            endpoint = RemoteEndpoint(
                name="mirror",
                url="https://forgejo.local/repo.git",
                auth_env="TEST_FORGE_TOKEN",
            )
            self.assertEqual(endpoint.resolved_url(), "https://secret123@forgejo.local/repo.git")
        finally:
            del os.environ["TEST_FORGE_TOKEN"]

    def test_simple_yaml_parsing(self):
        yaml_content = """
        repo_name: sample-repo
        sync_tags: true
        interval_seconds: 60
        """
        parsed = _parse_simple_yaml_or_json(yaml_content)
        self.assertEqual(parsed.get("repo_name"), "sample-repo")
        self.assertEqual(parsed.get("sync_tags"), True)
        self.assertEqual(parsed.get("interval_seconds"), 60)


class TestSyncEngine(unittest.TestCase):
    """Integration test against isolated local bare git repositories."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # 1. Initialize bare primary and mirror remotes
        self.primary_dir = self.root / "primary.git"
        self.mirror_dir = self.root / "mirror.git"
        self.local_workdir = self.root / "local"

        subprocess.run(["git", "init", "--bare", str(self.primary_dir)], check=True, capture_output=True)
        subprocess.run(["git", "init", "--bare", str(self.mirror_dir)], check=True, capture_output=True)

        # 2. Seed primary remote with initial commit
        seed_dir = self.root / "seed"
        seed_dir.mkdir()
        self._run_cmd(["git", "init", "-b", "main"], cwd=seed_dir)
        self._run_cmd(["git", "config", "core.hooksPath", ""], cwd=seed_dir)
        self._run_cmd(["git", "config", "user.name", "Tester"], cwd=seed_dir)
        self._run_cmd(["git", "config", "user.email", "tester@example.com"], cwd=seed_dir)
        (seed_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        self._run_cmd(["git", "add", "README.md"], cwd=seed_dir)
        self._run_cmd(["git", "commit", "-m", "chore(test): initial commit"], cwd=seed_dir)
        self._run_cmd(["git", "remote", "add", "origin", str(self.primary_dir)], cwd=seed_dir)
        self._run_cmd(["git", "push", "origin", "main"], cwd=seed_dir)

        # 3. Initialize local workdir for sync engine
        self.local_workdir.mkdir()
        self._run_cmd(["git", "init", "-b", "main"], cwd=self.local_workdir)
        self._run_cmd(["git", "config", "core.hooksPath", ""], cwd=self.local_workdir)
        self._run_cmd(["git", "config", "user.name", "SyncEngine"], cwd=self.local_workdir)
        self._run_cmd(["git", "config", "user.email", "engine@example.com"], cwd=self.local_workdir)

        self.config = SyncConfig(
            repo_name="test-integration",
            primary=RemoteEndpoint(name="primary", url=str(self.primary_dir), role="primary"),
            mirrors=[RemoteEndpoint(name="mirror", url=str(self.mirror_dir), role="mirror")],
            lock_file=".git/forge-sync.lock",
        )
        self.engine = SyncEngine(self.config, repo_path=self.local_workdir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_cmd(self, cmd: list, cwd: Path):
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def test_drift_detection_and_sync(self):
        # Initial check: primary has 'main', mirror has nothing
        parity = self.engine.check_parity()
        self.assertEqual(len(parity), 1)
        self.assertFalse(parity[0].in_sync)
        self.assertEqual(parity[0].mirror_shas.get("mirror"), "")

        # Run sync
        result = self.engine.sync()
        self.assertTrue(result.success)
        self.assertEqual(result.refs_checked, 1)
        self.assertEqual(len(result.drifted_refs), 1)

        # Re-check parity: should now be in sync
        parity_after = self.engine.check_parity()
        self.assertEqual(len(parity_after), 1)
        self.assertTrue(parity_after[0].in_sync)
        self.assertEqual(parity_after[0].primary_sha, parity_after[0].mirror_shas["mirror"])

    def test_dry_run_preserves_drift(self):
        result = self.engine.sync(dry_run=True)
        self.assertTrue(result.success)
        self.assertEqual(len(result.drifted_refs), 1)

        # Mirror should still have no refs
        mirror_refs = self.engine.get_remote_refs(self.config.mirrors[0])
        self.assertEqual(len(mirror_refs), 0)


if __name__ == "__main__":
    unittest.main()

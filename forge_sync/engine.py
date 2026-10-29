"""Core git mirror reconciliation engine."""

from __future__ import annotations

import fnmatch
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from forge_sync.config import RemoteEndpoint, SyncConfig
from forge_sync.lock import FileLock


@dataclass
class RefParity:
    ref: str
    primary_sha: str
    mirror_shas: Dict[str, str] = field(default_factory=dict)
    in_sync: bool = False


@dataclass
class SyncResult:
    repo_name: str
    timestamp: float
    success: bool
    refs_checked: int
    refs_synced: int
    drifted_refs: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "repo_name": self.repo_name,
            "timestamp": self.timestamp,
            "success": self.success,
            "refs_checked": self.refs_checked,
            "refs_synced": self.refs_synced,
            "drifted_refs": self.drifted_refs,
            "errors": self.errors,
            "duration_seconds": round(self.duration_seconds, 3),
        }


class SyncEngine:
    """Manages git synchronization between a primary remote and multiple mirrors."""

    def __init__(self, config: SyncConfig, repo_path: Path | str = ".") -> None:
        self.config = config
        self.repo_path = Path(repo_path).resolve()
        self.lock = FileLock(self.repo_path / self.config.lock_file)

    def _run_git(self, args: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
        """Execute git command returning (code, stdout, stderr)."""
        workdir = cwd or self.repo_path
        cmd = ["git"] + args
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def get_remote_refs(self, endpoint: RemoteEndpoint) -> Dict[str, str]:
        """Query refs on a remote repository via git ls-remote."""
        url = endpoint.resolved_url()
        code, out, err = self._run_git(["ls-remote", url])
        if code != 0:
            raise RuntimeError(f"git ls-remote failed for {endpoint.name} ({url}): {err}")

        refs: Dict[str, str] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            sha, ref = line.split("\t", 1)
            # Filter peeled tags like refs/tags/v1.0.0^{}
            if ref.endswith("^{}"):
                continue
            refs[ref] = sha
        return refs

    def check_parity(self) -> List[RefParity]:
        """Compare all refs between primary and all configured mirrors."""
        primary_refs = self.get_remote_refs(self.config.primary)
        mirror_refs_map = {m.name: self.get_remote_refs(m) for m in self.config.mirrors}

        parity_list: List[RefParity] = []
        for ref, p_sha in primary_refs.items():
            # Check branch filter
            if not self._ref_matches_filter(ref, self.config.primary.branch_filter):
                continue

            ref_obj = RefParity(ref=ref, primary_sha=p_sha)
            in_sync = True
            for m in self.config.mirrors:
                m_sha = mirror_refs_map.get(m.name, {}).get(ref, "")
                ref_obj.mirror_shas[m.name] = m_sha
                if m_sha != p_sha:
                    in_sync = False
            ref_obj.in_sync = in_sync
            parity_list.append(ref_obj)

        return parity_list

    def _ref_matches_filter(self, ref: str, filters: List[str]) -> bool:
        if "*" in filters:
            return True
        short_ref = ref.replace("refs/heads/", "").replace("refs/tags/", "")
        for pat in filters:
            if fnmatch.fnmatch(ref, pat) or fnmatch.fnmatch(short_ref, pat):
                return True
        return False

    def sync(self, dry_run: bool = False) -> SyncResult:
        """Perform full mirror sync with atomic lock protection."""
        start_time = time.time()
        result = SyncResult(
            repo_name=self.config.repo_name,
            timestamp=start_time,
            success=False,
            refs_checked=0,
            refs_synced=0,
        )

        with self.lock:
            try:
                # 1. Fetch latest refs from primary into local repository
                p_url = self.config.primary.resolved_url()
                fetch_args = ["fetch", p_url, "+refs/heads/*:refs/remotes/primary/*"]
                if self.config.sync_tags:
                    fetch_args.append("+refs/tags/*:refs/tags/*")
                if self.config.prune:
                    fetch_args.append("--prune")

                code, _, err = self._run_git(fetch_args)
                if code != 0:
                    result.errors.append(f"Fetch from primary failed: {err}")
                    result.duration_seconds = time.time() - start_time
                    return result

                # 2. Compare parity
                parity = self.check_parity()
                result.refs_checked = len(parity)

                drifted = [p for p in parity if not p.in_sync]
                result.drifted_refs = [d.ref for d in drifted]

                if not drifted:
                    result.success = True
                    result.duration_seconds = time.time() - start_time
                    return result

                if dry_run:
                    result.success = True
                    result.refs_synced = len(drifted)
                    result.duration_seconds = time.time() - start_time
                    return result

                # 3. Synchronize drifted refs to each mirror
                for mirror in self.config.mirrors:
                    m_url = mirror.resolved_url()
                    # Mirror push of branches and tags
                    push_args = ["push", m_url]
                    if self.config.prune:
                        push_args.append("--prune")

                    # Push the local tracking refs directly to mirror branches
                    for p in drifted:
                        if p.ref.startswith("refs/heads/"):
                            branch = p.ref[len("refs/heads/") :]
                            push_args.append(f"refs/remotes/primary/{branch}:refs/heads/{branch}")
                        elif p.ref.startswith("refs/tags/") and self.config.sync_tags:
                            tag = p.ref[len("refs/tags/") :]
                            push_args.append(f"refs/tags/{tag}:refs/tags/{tag}")

                    code, _, err = self._run_git(push_args)
                    if code != 0:
                        result.errors.append(f"Push to mirror '{mirror.name}' failed: {err}")
                    else:
                        result.refs_synced += len(drifted)

                result.success = len(result.errors) == 0
            except Exception as e:
                result.errors.append(str(e))
                result.success = False

        result.duration_seconds = time.time() - start_time
        return result

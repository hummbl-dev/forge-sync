"""forge-sync: Zero-dependency bidirectional git mirror and failover orchestrator.

Supports GitHub, Forgejo, GitLab, and Gitea with atomic file locking and drift verification.
"""

__version__ = "0.1.0"
__all__ = ["__version__", "SyncEngine", "SyncConfig", "FileLock", "SyncResult"]

from forge_sync.config import SyncConfig
from forge_sync.lock import FileLock
from forge_sync.engine import SyncEngine, SyncResult

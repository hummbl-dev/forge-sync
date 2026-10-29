"""Configuration parsing and validation for forge-sync topology manifests."""

from __future__ import annotations

import json
import os
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class RemoteEndpoint:
    name: str
    url: str
    role: str = "mirror"  # "primary" or "mirror"
    auth_env: Optional[str] = None
    branch_filter: List[str] = field(default_factory=lambda: ["*"])

    def resolved_url(self) -> str:
        """Resolve any embedded environment variables in URL or inject token."""
        url = os.path.expandvars(self.url)
        if self.auth_env and self.auth_env in os.environ:
            token = os.environ[self.auth_env]
            # If HTTPS URL without auth, inject token
            if url.startswith("https://") and "@" not in url:
                url = f"https://{token}@{url[8:]}"
        return url


@dataclass
class SyncConfig:
    repo_name: str
    primary: RemoteEndpoint
    mirrors: List[RemoteEndpoint] = field(default_factory=list)
    lock_file: str = ".git/forge-sync.lock"
    sync_tags: bool = True
    prune: bool = True
    interval_seconds: int = 300

    @classmethod
    def from_dict(cls, data: dict) -> SyncConfig:
        if not isinstance(data, dict):
            raise ValueError("Configuration root must be a dictionary")

        repo_name = data.get("repo_name", "unnamed-repo")
        primary_data = data.get("primary")
        if not primary_data or not isinstance(primary_data, dict):
            raise ValueError("Configuration must specify a 'primary' remote endpoint")

        primary = RemoteEndpoint(
            name=primary_data.get("name", "primary"),
            url=primary_data["url"],
            role="primary",
            auth_env=primary_data.get("auth_env"),
            branch_filter=primary_data.get("branch_filter", ["*"]),
        )

        mirrors = []
        for m in data.get("mirrors", []):
            mirrors.append(
                RemoteEndpoint(
                    name=m.get("name", "mirror"),
                    url=m["url"],
                    role="mirror",
                    auth_env=m.get("auth_env"),
                    branch_filter=m.get("branch_filter", ["*"]),
                )
            )

        return cls(
            repo_name=repo_name,
            primary=primary,
            mirrors=mirrors,
            lock_file=data.get("lock_file", ".git/forge-sync.lock"),
            sync_tags=data.get("sync_tags", True),
            prune=data.get("prune", True),
            interval_seconds=data.get("interval_seconds", 300),
        )

    @classmethod
    def load(cls, path: Path | str) -> SyncConfig:
        filepath = Path(path).resolve()
        if not filepath.exists():
            raise FileNotFoundError(f"Configuration file not found: {filepath}")

        raw = filepath.read_text(encoding="utf-8")
        if filepath.suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            # Simple minimal YAML subset parser or json fallback
            data = _parse_simple_yaml_or_json(raw)
        return cls.from_dict(data)


def _parse_simple_yaml_or_json(content: str) -> dict:
    """Parse JSON or a clean subset of YAML without requiring pyyaml."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Normalize leading indentation across multiline strings
    dedented = textwrap.dedent(content)
    result: dict = {}
    current_section = None
    current_item: dict = {}

    for line in dedented.splitlines():
        line = line.rstrip()
        if not line or line.strip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if stripped.startswith("- "):
            # Item in list
            item_str = stripped[2:].strip()
            if ":" in item_str:
                k, v = item_str.split(":", 1)
                current_item = {k.strip(): _coerce(v.strip())}
                if current_section:
                    result.setdefault(current_section, []).append(current_item)
            else:
                if current_section:
                    result.setdefault(current_section, []).append(_coerce(item_str))
        elif indent > 0 and current_item is not None and current_section and ":" in stripped:
            k, v = stripped.split(":", 1)
            current_item[k.strip()] = _coerce(v.strip())
        elif ":" in stripped:
            k, v = stripped.split(":", 1)
            v = v.strip()
            if not v:
                current_section = k.strip()
                current_item = {}
            else:
                result[k.strip()] = _coerce(v)
                current_section = None
                current_item = {}

    return result


def _coerce(val: str):
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    try:
        return int(val)
    except ValueError:
        pass
    return val

# forge-sync

> Zero-dependency bidirectional git mirror and failover orchestrator across GitHub, Forgejo, GitLab, and Gitea.

[![CI](https://github.com/hummbl-dev/forge-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/hummbl-dev/forge-sync/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

`forge-sync` solves centralized git forge lock-in by providing automated, drift-verified, atomic git mirroring between primary repositories and secondary/sovereign forges.

---

## Key Features

- **Zero Runtime Dependencies**: Built entirely with Python 3.11+ standard library (`subprocess`, `hashlib`, `json`, `dataclasses`).
- **Atomic Concurrency Locking**: Built-in `.git/forge-sync.lock` engine with PID recording and stale-lock recovery prevents split-brain race conditions across multi-agent environments.
- **Drift & Parity Inspection**: Fast `ls-remote` comparison detects desynchronized branches or tags before touching local disk.
- **Dry-Run & JSON Reporting**: Audit-ready JSON receipts for CI/CD gates and coordination logs.
- **Continuous Daemon Mode**: Run as a background service or cron job with configurable sync intervals.

---

## Installation

```bash
pip install git+https://github.com/hummbl-dev/forge-sync.git
```

Or run directly without installation:

```bash
python -m forge_sync.cli --help
```

---

## Configuration

Create a topology manifest (`forge-sync.yaml` or `forge-sync.json`):

```yaml
repo_name: my-project
interval_seconds: 300
sync_tags: true
prune: true

primary:
  name: github
  url: https://github.com/hummbl-dev/my-project.git
  branch_filter:
    - main
    - v*

mirrors:
  - name: forgejo
    url: https://git.hummbl.io/hummbl/my-project.git
    auth_env: FORGEJO_TOKEN
  - name: gitlab
    url: https://gitlab.com/hummbl/my-project.git
    auth_env: GITLAB_TOKEN
```

---

## Usage

### 1. Check Drift & Parity
```bash
forge-sync status --config forge-sync.yaml
```

Output:
```text
Status for 'my-project': 3 refs checked
  [✓] refs/heads/main (a1b2c3d4)
      -> forgejo: a1b2c3d4
      -> gitlab: a1b2c3d4
  [✗ DRIFT] refs/tags/v1.0.0 (e5f6g7h8)
      -> forgejo: MISSING
      -> gitlab: e5f6g7h8
```

### 2. Synchronize Mirrored Refs
```bash
forge-sync sync --config forge-sync.yaml
```

Dry-run preview:
```bash
forge-sync sync --config forge-sync.yaml --dry-run --json
```

### 3. Continuous Daemon Mode
```bash
forge-sync daemon --config forge-sync.yaml --interval 120
```

---

## Architecture & Security

`forge-sync` leverages native `git` CLI subprocessing to guarantee complete fidelity to git wire protocols, packfiles, and transport semantics.

```
                    ┌────────────────────────┐
                    │    Primary Remote      │
                    │   (GitHub / GitLab)    │
                    └───────────┬────────────┘
                                │
                        fetch   │
                                ▼
                    ┌────────────────────────┐
                    │   forge-sync Daemon    │
                    │  (Atomic File Lock)    │
                    └───────────┬────────────┘
                                │
                        push    │ (mirrored refs)
                                ▼
                    ┌────────────────────────┐
                    │    Sovereign Mirror    │
                    │  (Forgejo / Gitea)     │
                    └────────────────────────┘
```

- **Credential Hygiene**: Credentials are never written to disk or configuration files; they are resolved at runtime via environment variable names (`auth_env`).
- **Fail-Closed Locking**: If another agent or sync worker is currently updating refs, `forge-sync` waits up to timeout before exiting safely without corrupting the working tree.

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE).

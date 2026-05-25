# Agents Guidelines

This repository is public and should stay limited to the LXD snapshotting setup.

## Scope

Public contents should only cover:

- the snapshot script
- example configuration
- systemd service and timer units
- snapshotting-focused documentation

Do not add broader LXD platform planning docs, private host inventory, monitoring exports, captured UI state, local repo paths, private domains, private IP addresses, client certificates, trust tokens, or credentials. Keep private notes under `.private/`, which is git ignored.

## Safety Rules

- Treat snapshot deletion/pruning as destructive.
- Only prune snapshots created by this setup's configured `SNAPSHOT_PREFIX`.
- Keep dry-run behavior working for both create and prune paths.
- Do not assume a specific LXD remote, project, pool, bridge, host name, or instance name.
- Prefer explicit config over hardcoded local values.
- Preserve cancellation/error behavior by allowing failed `lxc` commands to stop the run.

## File Map

- `README.md`: public setup and operating notes.
- `config/lxd-snapshot.conf.example`: host-agnostic example config.
- `scripts/lxd-snapshot`: LXD snapshot and retention script.
- `systemd/lxd-snapshot.service`: oneshot service.
- `systemd/lxd-snapshot.timer`: scheduled timer.

Ignored local directories such as `docs/`, `monitoring/`, `.private/`, `.agents/`, `.codex/`, and `.playwright-mcp/` may exist in this working tree but are not part of the public repository.

## Verification

For script/config changes, run:

```bash
bash -n scripts/lxd-snapshot
```

For public-readiness checks, scan the tracked candidate files for private hostnames, addresses, cert/key material, tokens, and local absolute paths before pushing.

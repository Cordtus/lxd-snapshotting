# Agents Guidelines

This repository is public and should stay limited to the Cosmos SDK / Tendermint / CometBFT snapshot publication setup.

## Scope

Public contents should cover:

- source-side rsync upload helper
- builder-side finalizer
- example configuration
- tests and snapshotting-focused documentation

Do not add unrelated LXD platform planning docs, private host inventory, monitoring exports, captured UI state, local repo paths, private domains, private IP addresses, client certificates, trust tokens, validator keys, node keys, keyrings, or credentials. Keep private notes under `.private/`, which is git ignored.

## Architecture Rules

- Source nodes must rsync only from a consistent DB view: stopped node, filesystem snapshot, or otherwise frozen source.
- The builder packages only what it receives; it cannot repair inconsistent source data.
- The builder writes public artifacts through staged files and atomic renames.
- The FTP/public-serving container must only receive the public output volume read-only.
- The ingest volume must never be mounted into the FTP/public-serving container.
- Publication must not update `latest` symlinks until archive validation and checksum generation succeed.

## Safety Rules

- Treat archive rotation/pruning as destructive.
- Only prune timestamped archives matching the configured chain prefix.
- Keep dry-run behavior working for upload and finalizer paths.
- Do not assume a specific VPN address, SSH port, LXD bridge, pool, chain, or container name.
- Prefer explicit config over hardcoded local values.
- Exclude validator state, keys, and keyrings by default.

## File Map

- `README.md`: public setup and operating notes.
- `config/source-upload.env.example`: source-side rsync helper config.
- `config/finalizer.env.example`: builder-side finalizer config.
- `scripts/cosmos-snapshot-upload`: rsync-over-SSH helper for consistent DB views.
- `scripts/cosmos-snapshot-finalize`: builder-side package/publish/rotate tool.
- `tests/test_snapshot_tools.py`: focused behavior tests.

Ignored local directories such as `docs/`, `monitoring/`, `.private/`, `.agents/`, `.codex/`, and `.playwright-mcp/` may exist in this working tree but are not part of the public repository.

## Verification

For tool changes, run:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/cosmos-snapshot-finalize scripts/cosmos-snapshot-upload
```

For public-readiness checks, scan the tracked candidate files for private hostnames, addresses, cert/key material, tokens, local absolute paths, and validator material before pushing.

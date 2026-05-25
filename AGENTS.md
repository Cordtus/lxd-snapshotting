# Agents Guidelines

This repository is public and should stay limited to the Cosmos SDK / Tendermint / CometBFT snapshot publication setup.

## Scope

Public contents should cover:

- source-side rsync upload helper
- builder-side restricted remote command wrapper
- builder-side finalizer
- example configuration
- tests and snapshotting-focused documentation

Do not add unrelated LXD platform planning docs, private host inventory, monitoring exports, captured UI state, local repo paths, private domains, private IP addresses, client certificates, trust tokens, validator keys, node keys, keyrings, or credentials. Keep private notes under `.private/`, which is git ignored.

## Architecture Rules

- Source upload must stop the configured local systemd service, validate the configured local data path, stage only allowed database directories into a separate local path, restart the service, then rsync the staging path.
- The builder packages only what it receives; it cannot repair inconsistent source data.
- The client initiates the full happy path: prepare remote ingest, rsync, and finalize.
- Client-side config must not accept builder-side file locations; the remote wrapper owns and enforces builder-side ingest paths.
- The builder-side SSH wrapper must enforce client-to-chain access control before prepare, rsync, finalize, or status operations.
- The builder writes public artifacts through staged files and atomic renames.
- The finalizer must validate that uploaded content looks like a Tendermint/CometBFT node database before publishing.
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
- Do not stage `snapshots`, `cs.wal`, or other non-database top-level data directories by default.
- Do not trust client-supplied client IDs in production. Use forced-command SSH key configuration to set `COSMOS_SNAPSHOT_CLIENT_ID`.

## File Map

- `README.md`: public setup and operating notes.
- `config/source-upload.env.example`: source-side rsync helper config.
- `config/remote.env.example`: builder-side restricted SSH wrapper config.
- `config/access-control.json.example`: client-to-chain allowlist example.
- `config/finalizer.env.example`: builder-side finalizer config.
- `scripts/cosmos-snapshot-upload`: rsync-over-SSH helper for consistent DB views.
- `scripts/cosmos-snapshot-remote`: restricted builder-side command for prepare, rsync, status, and finalize.
- `scripts/cosmos-snapshot-finalize`: builder-side package/publish/rotate tool.
- `tests/test_snapshot_tools.py`: focused behavior tests.

Ignored local directories such as `docs/`, `monitoring/`, `.private/`, `.agents/`, `.codex/`, and `.playwright-mcp/` may exist in this working tree but are not part of the public repository.

## Verification

For tool changes, run:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/cosmos-snapshot-finalize scripts/cosmos-snapshot-upload scripts/cosmos-snapshot-remote
```

For public-readiness checks, scan the tracked candidate files for private hostnames, addresses, cert/key material, tokens, local absolute paths, and validator material before pushing.

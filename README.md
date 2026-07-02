# Cosmos Snapshot Uploader

Tools for publishing Cosmos SDK / Tendermint / CometBFT database snapshots.

Client side stops the node, validates `NODE_HOME/DATA_SUBDIR`, stages allowed DB
dirs, restarts, uploads over VPN/SSH, and asks the builder to finalize.

Snapshot host side is operated by us. The client gets a VPN endpoint,
forced-command SSH, and a prefilled config.

This is not a validator-key backup tool.

## Flow

```text
source node:
  stop -> validate -> stage allowed DB dirs -> restart
  rsync staging copy, or bootstrap with one tar stream
  request finalize

snapshot host:
  authorize client -> receive ingest data
  validate DB -> create archive/checksum/manifest
  publish latest atomically -> prune old managed archive
```

The client never configures server storage paths.

## Files

- `scripts/cosmos-snapshot-upload`: source-node stage/upload command.
- `config/source-upload.env.example`: source-node config template.
- `CLIENT_SETUP.md`: concise source-node setup guide for clients.
- `index.html`: local guide index.
- `guides/lxd-snapshotter-operator-setup.html`: server/operator runbook.
- `guides/lxd-snapshotter-client-setup.html`: client runbook.
- `tests/test_snapshot_tools.py`: behavior coverage.

## Client Side

Use `CLIENT_SETUP.md` for the concise GitHub setup flow, or
`guides/lxd-snapshotter-client-setup.html` for the interactive handoff runbook.
The source operator needs only:

- router VPN access and the WireGuard config file
- the upload SSH key
- `scripts/cosmos-snapshot-upload`
- a filled `/etc/cosmos-snapshot/source-upload.env`

The client config sets `CHAIN`, `NODE_HOME`, `DATA_SUBDIR`, `SERVICE_NAME`,
`STAGING_PATH`, and upload SSH options. It never sets builder storage paths.

First run:

```bash
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env --dry-run
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env --bootstrap
```

Routine refresh:

```bash
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env
```

The helper stops the node, validates `NODE_HOME/DATA_SUBDIR`, stages only public
DB directories, restarts the node, rsyncs the staging copy, verifies remote
drift, and asks the builder to publish. Default staged directories are
`application.db`, `blockstore.db`, `state.db`, `evidence.db`, and `tx_index.db`.
Validator state, keys, keyrings, WALs, state-sync snapshots, and arbitrary
top-level paths are excluded.

## Snapshot Host Boundary

The source node does not set host storage, public paths, retention,
publication, or ingest dirs. The restricted remote command owns them.

The client may request only allowed chain operations: `prepare`, `rsync`,
`bootstrap`, `status`, `finalize`.

## Validation

Both sides validate before the next risky step.

Required DB dirs:

- `application.db`
- `blockstore.db`
- `state.db`

Validation catches wrong paths and incomplete uploads. Service stop creates
the consistent staging source.

## Verification

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/cosmos-snapshot-finalize scripts/cosmos-snapshot-upload scripts/cosmos-snapshot-remote
```

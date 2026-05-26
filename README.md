# Cosmos Snapshot Uploader

Utilities for publishing Cosmos SDK / Tendermint / CometBFT node database snapshots.

The project is split into two sides:

- **Client side**: runs on the source node, briefly stops the node service, stages a clean database copy, uploads it, and triggers publication.
- **Server side**: receives authorized uploads, packages the staged database, publishes the completed archive, and exposes only completed artifacts through whatever public file service you choose.

This is for public/client-node database snapshots. It is not a validator-key backup system.

## Flow

```text
source node
  stop service -> validate data dir -> stage allowed DB dirs -> restart service
  rsync staged copy over the private transport
  trigger remote finalizer

snapshot server
  authorize client -> receive rsync into managed ingest storage
  validate uploaded DB -> create archive/checksum/manifest
  atomically publish latest snapshot -> prune old managed archives
```

The client does not configure server-side storage paths. Server paths live in server-side config only.

## Files

- `scripts/cosmos-snapshot-upload`: client-side source/stage/upload command.
- `scripts/cosmos-snapshot-remote`: server-side restricted SSH command.
- `scripts/cosmos-snapshot-finalize`: server-side package/publish command.
- `config/source-upload.env.example`: client-side config template.
- `config/remote.env.example`: server-side restricted command config template.
- `config/finalizer.env.example`: server-side finalizer config template.
- `config/access-control.json.example`: server-side client-to-chain allowlist template.

## Client Side

The client config should describe only the source node and the upload target:

- `CHAIN`
- `DATA_PATH`
- `SERVICE_NAME`
- `STAGING_PATH`
- SSH target/options for the authorized upload account
- optional rsync tuning

`cosmos-snapshot-upload` performs the local consistency step:

```text
systemctl stop <service>
validate <data path>
rsync selected database directories to <staging path>
systemctl start <service>
rsync <staging path> to the server
verify remote copy with a checksum dry-run
request finalization
```

Only known public database directories are staged by default:

- `application.db`
- `blockstore.db`
- `state.db`
- `evidence.db`
- `tx_index.db`

The uploader rejects arbitrary top-level paths and does not stage validator state, state-sync snapshots, WAL directories, keys, or keyrings.

Useful client checks:

```bash
cosmos-snapshot-upload --config ./source-upload.env --dry-run
cosmos-snapshot-upload --config ./source-upload.env
```

## Server Side

The server owns:

- upload authorization
- ingest storage location
- public artifact storage location
- archive format
- publication and retention

Use a dedicated upload account and force its SSH keys through `cosmos-snapshot-remote`. The forced command should set `COSMOS_SNAPSHOT_CLIENT_ID` and pass the trusted remote config path as command arguments.

Example key shape:

```text
command="/usr/bin/env COSMOS_SNAPSHOT_CLIENT_ID=example-source /usr/local/sbin/cosmos-snapshot-remote --config /etc/cosmos-snapshot-remote.env",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... example-source
```

The remote wrapper allows only:

- `prepare --chain <chain>`
- `rsync --chain <chain>`
- `finalize --chain <chain>`
- `status --chain <chain>`

It checks the client allowlist before every operation and rewrites the rsync destination to the server-configured ingest path. A client cannot select the server-side destination path.

The finalizer publishes:

```text
<public-root>/<chain>/
  <chain>-YYYYMMDDTHHMMSSZ.tar.zst
  <chain>-YYYYMMDDTHHMMSSZ.tar.zst.sha256
  <chain>-YYYYMMDDTHHMMSSZ.tar.zst.json
  latest.tar.zst
  latest.sha256
  latest.json
```

Retention is fixed to one completed managed archive per chain. Older managed archives are pruned only after the new archive validates and publishes.

## Validation

Both sides validate the data layout before doing the next risky step.

By default, a valid upload must include recognizable Tendermint database markers in:

- `application.db`
- `blockstore.db`
- `state.db`

This validation catches wrong paths and incomplete uploads. It does not prove a live database copy is internally consistent; the client-side service stop is what creates the consistent staging source.

## Public Serving

Public serving is intentionally separate from upload/build. The public file service should receive read-only access to completed artifacts only. It should not receive:

- ingest storage
- upload credentials
- SSH access
- write access to published snapshots

The file service can be FTP, HTTP, object storage sync, or another deployment-specific serving layer.

## Verification

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/cosmos-snapshot-finalize scripts/cosmos-snapshot-upload scripts/cosmos-snapshot-remote
```

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
- `index.html`: local guide index.
- `guides/lxd-snapshotter-operator-setup.html`: server/operator runbook.
- `guides/lxd-snapshotter-client-setup.html`: client runbook.
- `tests/test_snapshot_tools.py`: behavior coverage.

## Client Side

Open `index.html` locally. It links the server/operator and client/source
runbooks. Each runbook has a print action.

Transport uses the router VPN. The builder is VPN/LAN-only.

Client config covers:

- `CHAIN`
- `NODE_HOME`
- `DATA_SUBDIR` (usually `data`)
- `SERVICE_NAME`
- `STAGING_PATH` on local disk or a mounted share
- upload SSH target/options
- optional rsync tuning

Install the prefilled config at `/etc/cosmos-snapshot/source-upload.env`. Verify
with one dry run and one bootstrap seed. Then run manually or by optional timer.

Local settings UI:

```bash
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env --serve-ui --ui-host 127.0.0.1 --ui-port 8766
```

Real upload:

```text
systemctl stop <service>
validate <node home>/<data subdir>
rsync selected DB dirs to <staging path>
systemctl start <service>
rsync <staging path> to the server
checksum dry-run verify
request finalization
```

Set `NODE_HOME` to the dir containing `config/`, `data/`, and keyrings, for
example `~/.genesisd`. The uploader reads only `NODE_HOME/DATA_SUBDIR`.

`STAGING_PATH` is a persistent rsync copy. Use a mounted share if local disk
cannot hold a second DB copy.

First large seed:

```bash
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env --bootstrap
```

Bootstrap still runs stop/stage/restart, then streams a tar archive from
`STAGING_PATH`. Later runs use normal rsync.

Optional timer:

```ini
[Timer]
OnCalendar=Mon,Thu *-*-* 03:15:00
RandomizedDelaySec=2h
Persistent=true
Unit=cosmos-snapshot-upload.service
```

Handshake: `prepare`, transfer, checksum dry-run verify, `finalize`. Server
validates before writing archive, checksum, manifest, and `latest.*`.

Default staged directories:

- `application.db`
- `blockstore.db`
- `state.db`
- `evidence.db`
- `tx_index.db`

The uploader excludes validator state, state-sync snapshots, WALs, keys,
keyrings, and arbitrary top-level paths.

Useful checks:

```bash
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env --dry-run
systemctl list-timers cosmos-snapshot-upload.timer
```

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
python3 -m py_compile scripts/cosmos-snapshot-upload
```

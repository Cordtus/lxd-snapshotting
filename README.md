# Cosmos Snapshot Uploader

Tools for publishing Cosmos SDK / Tendermint / CometBFT database snapshots through an isolated builder container.

The intended deployment shape is:

```text
source node consistent DB view
  -> rsync over SSH over VPN
  -> snap-builder ingest volume
  -> builder finalizer packages and publishes
  -> public volume mounted read-only into FTP container
```

This is a public/client-node database snapshot publication setup. It is not a validator key backup system.

## Contents

- `scripts/cosmos-snapshot-upload`: source-side rsync helper that sends a consistent DB view to the builder and optionally triggers finalization.
- `scripts/cosmos-snapshot-finalize`: builder-side finalizer that packages `/ingest/<chain>/data`, validates the archive, writes checksum/manifest sidecars, atomically updates `latest` symlinks, and rotates old archives.
- `config/source-upload.env.example`: source-side upload config.
- `config/finalizer.env.example`: builder-side finalizer config.
- `tests/test_snapshot_tools.py`: focused tests for packaging, secret exclusion, partial-transfer refusal, and dry-run upload commands.

## Architecture

The public FTP container should not communicate directly with the builder container. The boundary is storage-based:

```text
snap-builder writes completed artifacts
ftp container reads completed artifacts
```

Recommended LXD resources:

- isolated builder bridge, not the shared public bridge
- `snap-ingest` custom volume mounted read-write into the builder only
- `snap-public` custom volume mounted read-write into the builder and read-only into the FTP container
- host LXD proxy bound to the VPN address only, forwarding SSH to the builder

The source node should reach the builder as an SSH target over a private VPN path. The FTP container only sees completed files from the public volume.

## Source Consistency

Do not rsync directly from a live, actively mutating Tendermint database directory.

The source path must be one of:

- a stopped node's database directory
- a filesystem snapshot of the node database
- another frozen/consistent view of the node database

For large chains, the preferred source flow is:

```text
stop or pause briefly
create filesystem snapshot
restart node
rsync from the filesystem snapshot
remove filesystem snapshot after publication succeeds
```

## Public Layout

The finalizer publishes one chain directory:

```text
/snapshots/<chain>/
  <chain>-YYYYMMDDTHHMMSSZ.tar.zst
  <chain>-YYYYMMDDTHHMMSSZ.tar.zst.sha256
  <chain>-YYYYMMDDTHHMMSSZ.tar.zst.json
  latest.tar.zst -> <chain>-YYYYMMDDTHHMMSSZ.tar.zst
  latest.sha256 -> <chain>-YYYYMMDDTHHMMSSZ.tar.zst.sha256
  latest.json -> <chain>-YYYYMMDDTHHMMSSZ.tar.zst.json
```

If finalization fails, the previous `latest` symlinks remain unchanged.

## Install

On the builder container:

```bash
sudo install -m 0755 scripts/cosmos-snapshot-finalize /usr/local/sbin/cosmos-snapshot-finalize
sudo install -m 0644 config/finalizer.env.example /etc/cosmos-snapshot-finalizer.env
sudoedit /etc/cosmos-snapshot-finalizer.env
```

On the source node or operator machine:

```bash
sudo install -m 0755 scripts/cosmos-snapshot-upload /usr/local/bin/cosmos-snapshot-upload
install -m 0644 config/source-upload.env.example ./source-upload.env
```

## Example Flow

Dry-run the source-side transfer:

```bash
cosmos-snapshot-upload --config ./source-upload.env --dry-run
```

Run the transfer and trigger finalization:

```bash
cosmos-snapshot-upload --config ./source-upload.env
```

Run finalization directly on the builder:

```bash
sudo cosmos-snapshot-finalize --config /etc/cosmos-snapshot-finalizer.env --chain examplechain
```

## Security Defaults

- The upload user should be non-root and reachable only over the VPN-bound SSH proxy.
- Password login, TCP forwarding, X11 forwarding, and root login should be disabled for the upload user.
- The finalizer refuses to package while transfer marker files exist.
- The finalizer excludes `priv_validator_state.json` by default.
- Public FTP should mount only the completed snapshot volume read-only.
- Validator keys and keyrings are non-goals for this repository and should be handled by a separate private backup process.

## Verification

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/cosmos-snapshot-finalize scripts/cosmos-snapshot-upload
```

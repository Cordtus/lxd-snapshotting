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

- `scripts/cosmos-snapshot-upload`: source-side helper that initializes the whole upload, rsync, and finalization flow.
- `scripts/cosmos-snapshot-remote`: builder-side restricted SSH command for prepare, rsync, status, and finalize operations.
- `scripts/cosmos-snapshot-finalize`: builder-side finalizer that packages `/ingest/<chain>/data`, validates the archive, writes checksum/manifest sidecars, atomically updates `latest` symlinks, and rotates old archives.
- `config/source-upload.env.example`: source-side upload config.
- `config/remote.env.example`: builder-side restricted SSH command config.
- `config/access-control.json.example`: client-to-chain allowlist.
- `config/finalizer.env.example`: builder-side finalizer config.
- `tests/test_snapshot_tools.py`: focused tests for packaging, access control, Tendermint validation, secret exclusion, partial-transfer refusal, and dry-run upload commands.

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

## Client-Initiated Flow

The source-side client initializes the whole process:

```text
1. client runs cosmos-snapshot-upload
2. client asks builder wrapper to prepare the chain ingest path
3. client rsyncs the consistent DB view using the restricted rsync path
4. client asks builder wrapper to finalize
5. builder validates, packages, publishes, and rotates archives
```

The builder does not need a scheduler for normal operation. It only responds to the restricted SSH command surface exposed to allowed upload clients.

## Access Control

Use a dedicated upload user and force every SSH key through `cosmos-snapshot-remote`.

The wrapper reads `COSMOS_SNAPSHOT_CLIENT_ID` from the forced-command environment and checks `config/access-control.json.example` style allowlists before doing anything. A client can only prepare, rsync to, or finalize chains explicitly assigned to that client.

Example `authorized_keys` shape:

```text
command="/usr/bin/env COSMOS_SNAPSHOT_CLIENT_ID=example-source /usr/local/sbin/cosmos-snapshot-remote --config /etc/cosmos-snapshot-remote.env",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... example-source
```

Pass the wrapper config path as trusted forced-command argv. Do not allow the client side to select it through environment variables.

The remote wrapper accepts only:

- `prepare --chain <chain>`
- `rsync --chain <chain>` as an rsync server command
- `finalize --chain <chain>`
- `status --chain <chain>`

The rsync path is checked so the remote rsync server can only write inside the allowed chain ingest directory.

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

## Upload Validation

Before packaging, the finalizer validates that the upload looks like a Tendermint/CometBFT node database. This validation is mandatory. By default it requires:

- `application.db`
- `blockstore.db`
- `state.db`

Each required database directory must contain at least one recognizable database marker such as `CURRENT`, `MANIFEST-*`, `*.sst`, or `*.ldb`.

The finalizer still cannot prove the database was copied from a consistent source. That remains the source operator's responsibility.

## Install

On the builder container:

```bash
sudo install -m 0755 scripts/cosmos-snapshot-remote /usr/local/sbin/cosmos-snapshot-remote
sudo install -m 0755 scripts/cosmos-snapshot-finalize /usr/local/sbin/cosmos-snapshot-finalize
sudo install -m 0644 config/remote.env.example /etc/cosmos-snapshot-remote.env
sudo install -m 0644 config/access-control.json.example /etc/cosmos-snapshot-access.json
sudo install -m 0644 config/finalizer.env.example /etc/cosmos-snapshot-finalizer.env
sudoedit /etc/cosmos-snapshot-remote.env
sudoedit /etc/cosmos-snapshot-access.json
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
- The upload user's SSH keys should be forced through `cosmos-snapshot-remote`.
- Client IDs should be mapped to allowed chains in the builder-side access-control file.
- The finalizer refuses to package while transfer marker files exist.
- The finalizer rejects uploads that do not look like a Tendermint/CometBFT database.
- The finalizer excludes `priv_validator_state.json`, validator keys, node keys, and keyrings by default.
- Public FTP should mount only the completed snapshot volume read-only.
- Validator keys and keyrings are non-goals for this repository and should be handled by a separate private backup process.

## Verification

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/cosmos-snapshot-finalize scripts/cosmos-snapshot-upload scripts/cosmos-snapshot-remote
```

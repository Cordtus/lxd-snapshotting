# Client Setup

This is the source-node setup flow. The snapshot host operator provides the
WireGuard config, upload SSH key, builder host, SSH port, remote command, chain
name, and upload account.

## 1. Install transport tools

Run as root, or prefix commands with `sudo`.

```bash
apt-get update
apt-get install -y wireguard-tools openssh-client rsync python3 tar
```

## 2. Install VPN config

Place the provided WireGuard config at `/etc/wireguard/cosmos-snapshot.conf`.

```bash
install -d -m 0700 /etc/wireguard
install -m 0600 ./cosmos-snapshot.conf /etc/wireguard/cosmos-snapshot.conf
wg-quick up cosmos-snapshot
wg show cosmos-snapshot
systemctl enable wg-quick@cosmos-snapshot
```

The router VPN should allow this peer to reach only the upload SSH service.

## 3. Install SSH key

```bash
install -d -m 0750 /etc/cosmos-snapshot
install -m 0600 ./upload_key /etc/cosmos-snapshot/upload_key
ssh -i /etc/cosmos-snapshot/upload_key -o IdentitiesOnly=yes -o BatchMode=yes \
  -p <ssh-port> <upload-user>@<builder-host> \
  <remote-command> status --chain <chain>
```

## 4. Install uploader

```bash
install -m 0755 scripts/cosmos-snapshot-upload /usr/local/bin/cosmos-snapshot-upload
```

## 5. Write environment

Install the filled config at `/etc/cosmos-snapshot/source-upload.env`.

```bash
CHAIN="<chain>"

NODE_HOME="<node-home>"
DATA_SUBDIR="data"
SERVICE_NAME="<node-service>.service"
SYSTEMCTL_COMMAND="systemctl"

STAGING_PATH="/var/tmp/<chain>-stage"

REQUIRED_DB_DIRS="application.db blockstore.db state.db"
STAGE_DB_DIRS="application.db blockstore.db state.db evidence.db tx_index.db"
DB_MARKER_PATTERNS="CURRENT MANIFEST-* *.sst *.ldb"
LOCAL_RSYNC_EXTRA_ARGS="--numeric-ids"

BUILDER_SSH="<upload-user>@<builder-host>"
BUILDER_SSH_PORT=<ssh-port>
SSH_OPTIONS="-i /etc/cosmos-snapshot/upload_key -o IdentitiesOnly=yes -o BatchMode=yes"
REMOTE_COMMAND="<remote-command>"
CLIENT_ID=""

RSYNC_DELETE=1
RSYNC_EXTRA_ARGS="--numeric-ids"
VERIFY_REMOTE_RSYNC=1
RSYNC_CHECKSUM=0
BOOTSTRAP_UPLOAD=0
RUN_FINALIZER=1
DRY_RUN=0
UPLOAD_SCHEDULE="Mon,Thu *-*-* 03:15:00"
```

`STAGING_PATH` must be outside the live node data tree and large enough for one
extra copy of the selected database directories.

## 6. Verify, seed, refresh

```bash
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env --dry-run
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env --bootstrap
cosmos-snapshot-upload --config /etc/cosmos-snapshot/source-upload.env
```

Use `--bootstrap` once for an empty remote ingest path. Routine runs use rsync.

The uploader stops the node, stages only public DB directories, restarts the
node, uploads the staging copy, verifies remote drift, and asks the builder to
publish. It excludes validator state, keys, keyrings, WALs, state-sync
snapshots, and arbitrary top-level paths.

The client never sets builder storage, retention, publication, or ingest paths.

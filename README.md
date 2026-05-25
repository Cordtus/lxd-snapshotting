# LXD Snapshotting Setup

Small, host-agnostic setup for scheduled LXD instance and custom-volume snapshots.

This repository intentionally contains only the public snapshotting setup. Private host inventories, captured UI notes, monitoring dashboards, and broader platform planning docs are kept out of Git with `.gitignore`.

## Contents

- `scripts/lxd-snapshot`: snapshot and retention script built around the `lxc` CLI.
- `config/lxd-snapshot.conf.example`: environment-style configuration example.
- `systemd/lxd-snapshot.service`: oneshot unit for running the snapshot job.
- `systemd/lxd-snapshot.timer`: daily timer with randomized delay.

## Requirements

- Linux host with `systemd` for the provided timer units.
- LXD/LXC client configured for the target daemon.
- `lxc`, `jq`, `bash`, and GNU `date`.
- A client identity with permission to snapshot and delete snapshots for the selected project/resources.

## Quick Start

Install the script and configuration:

```bash
sudo install -m 0755 scripts/lxd-snapshot /usr/local/sbin/lxd-snapshot
sudo install -m 0644 config/lxd-snapshot.conf.example /etc/lxd-snapshot.conf
sudoedit /etc/lxd-snapshot.conf
```

Install and enable the timer:

```bash
sudo install -m 0644 systemd/lxd-snapshot.service /etc/systemd/system/lxd-snapshot.service
sudo install -m 0644 systemd/lxd-snapshot.timer /etc/systemd/system/lxd-snapshot.timer
sudo systemctl daemon-reload
sudo systemctl enable --now lxd-snapshot.timer
```

Run a dry run before enabling destructive pruning:

```bash
sudo lxd-snapshot --config /etc/lxd-snapshot.conf --dry-run
```

Run once immediately:

```bash
sudo systemctl start lxd-snapshot.service
```

## Configuration

The config file is sourced as a shell environment file. Keep values simple and quote whitespace-separated lists.

Key settings:

- `LXD_REMOTE`: optional LXC remote name without a trailing colon. Leave empty for the default local connection.
- `LXD_PROJECT`: project to snapshot.
- `INSTANCE_NAMES`: `all` or a whitespace-separated list of instances.
- `SNAPSHOT_CUSTOM_VOLUMES`: set to `1` to snapshot custom volumes too.
- `CUSTOM_VOLUME_POOLS`: storage pools to scan when volume snapshots are enabled.
- `CUSTOM_VOLUME_NAMES`: `all` or a whitespace-separated list of custom volumes.
- `SNAPSHOT_PREFIX`: prefix used for snapshots managed by this job.
- `RETENTION_DAYS`: snapshots older than this many days are pruned when `PRUNE_OLD=1`.
- `DRY_RUN`: set to `1` to print commands without applying changes.

## Safety Model

- The script only prunes snapshots whose names start with `SNAPSHOT_PREFIX-`.
- Snapshot creation and pruning are separate toggles.
- Instance snapshots default to root disk snapshots. Set `SNAPSHOT_DISK_VOLUMES=all-exclusive` only when you also want exclusively attached instance volumes included.
- No hostnames, remotes, credentials, client certificates, or private inventory belong in this repository.

## Verification

For local changes:

```bash
bash -n scripts/lxd-snapshot
jq empty monitoring/*.json 2>/dev/null || true
```

The `monitoring/` check is optional and only useful if private ignored dashboards exist in the working tree.

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / "scripts" / "cosmos-snapshot-finalize"
UPLOADER = ROOT / "scripts" / "cosmos-snapshot-upload"
REMOTE = ROOT / "scripts" / "cosmos-snapshot-remote"


class SnapshotToolTests(unittest.TestCase):
    def run_cmd(self, args, **kwargs):
        return subprocess.run(
            [sys.executable, *map(str, args)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )

    def make_ingest(self, root, chain="examplechain"):
        chain_root = root / "ingest" / chain
        data = chain_root / "data"
        (data / "application.db").mkdir(parents=True)
        (data / "blockstore.db").mkdir()
        (data / "state.db").mkdir()
        (data / "application.db" / "CURRENT").write_text("db-current")
        (data / "blockstore.db" / "CURRENT").write_text("block-current")
        (data / "state.db" / "CURRENT").write_text("state-current")
        (data / "priv_validator_state.json").write_text('{"height":"1"}')
        (chain_root / "config").mkdir()
        (chain_root / "config" / "priv_validator_key.json").write_text('{"secret":"validator"}')
        (chain_root / "config" / "node_key.json").write_text('{"secret":"node"}')
        (chain_root / "keyring-test").mkdir()
        (chain_root / "keyring-test" / "key").write_text("secret")
        return chain_root

    def make_source_node_home(self, root, name="node-home", data_subdir="data"):
        node_home = root / name
        data = node_home / data_subdir
        for db_dir in ["application.db", "blockstore.db", "state.db", "evidence.db", "tx_index.db"]:
            (data / db_dir).mkdir(parents=True)
            (data / db_dir / "CURRENT").write_text(f"{db_dir}-current")
        (data / "cs.wal").mkdir()
        (data / "cs.wal" / "wal").write_text("private wal")
        (data / "snapshots").mkdir()
        (data / "snapshots" / "chunk").write_text("state sync chunk")
        (data / "priv_validator_state.json").write_text('{"height":"1"}')
        (node_home / "config").mkdir()
        (node_home / "config" / "priv_validator_key.json").write_text('{"secret":"validator"}')
        (node_home / "keyring-test").mkdir()
        (node_home / "keyring-test" / "key").write_text("secret")
        (node_home / "ibc_08-wasm_client_data").mkdir()
        (node_home / "ibc_08-wasm_client_data" / "client").write_text("private client data")
        return node_home

    def make_source_data(self, root):
        return self.make_source_node_home(root) / "data"

    def upload_config_lines(self, node_home, staging, extra_lines=None, data_subdir="data"):
        lines = [
            "CHAIN=\"examplechain\"",
            f"NODE_HOME=\"{node_home}\"",
            f"DATA_SUBDIR=\"{data_subdir}\"",
            f"STAGING_PATH=\"{staging}\"",
            "SERVICE_NAME=\"examplechaind.service\"",
            "BUILDER_SSH=\"snapupload@snapshot-builder.vpn\"",
        ]
        if extra_lines:
            lines.extend(extra_lines)
        return lines

    def write_fake_command(self, directory, name, log_path, exit_code=0):
        command = directory / name
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"with open({str(log_path)!r}, 'a') as handle:\n"
            f"    handle.write(json.dumps([{name!r}, sys.argv[1:]]) + '\\n')\n"
            f"raise SystemExit({exit_code})\n"
        )
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
        return command

    def write_fake_ssh_receiving_stdin(self, directory, log_path):
        command = directory / "ssh"
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "payload = sys.stdin.buffer.read()\n"
            f"with open({str(log_path)!r}, 'a') as handle:\n"
            "    handle.write(json.dumps(['ssh', sys.argv[1:], len(payload)]) + '\\n')\n"
        )
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
        return command

    def write_fake_ssh_capturing_stdin(self, directory, log_path, payload_path):
        command = directory / "ssh"
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "payload = sys.stdin.buffer.read()\n"
            "if payload:\n"
            f"    open({str(payload_path)!r}, 'wb').write(payload)\n"
            f"with open({str(log_path)!r}, 'a') as handle:\n"
            "    handle.write(json.dumps(['ssh', sys.argv[1:], len(payload)]) + '\\n')\n"
        )
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
        return command

    def write_fake_rsync(self, directory, log_path):
        command = directory / "rsync"
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, shutil, sys\n"
            "args = sys.argv[1:]\n"
            f"with open({str(log_path)!r}, 'a') as handle:\n"
            "    handle.write(json.dumps(['rsync', args]) + '\\n')\n"
            "if '--dry-run' in args:\n"
            "    noisy_short = any(arg.startswith('-') and not arg.startswith('--') and any(flag in arg[1:] for flag in ['v', 'P']) for arg in args)\n"
            "    if (any(arg == '--info=progress2' for arg in args) or noisy_short or '--stats' in args) and os.environ.get('FAKE_RSYNC_VERIFY_PROGRESS'):\n"
            "        print('          0 100%    0.00kB/s    0:00:00')\n"
            "    if os.environ.get('FAKE_RSYNC_VERIFY_CHANGES'):\n"
            "        print('>f+++++++++ application.db/CURRENT')\n"
            "    raise SystemExit(0)\n"
            "if any(arg.startswith('--rsync-path=') for arg in args):\n"
            "    raise SystemExit(int(os.environ.get('FAKE_RSYNC_REMOTE_EXIT', '0')))\n"
            "src = args[-2].rstrip('/')\n"
            "dst = args[-1].rstrip('/')\n"
            "if '--delete' in args and os.path.exists(dst):\n"
            "    shutil.rmtree(dst)\n"
            "os.makedirs(os.path.dirname(dst), exist_ok=True)\n"
            "shutil.copytree(src, dst, dirs_exist_ok=True)\n"
        )
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
        return command

    def read_json_lines(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def load_uploader_module(self):
        loader = importlib.machinery.SourceFileLoader("cosmos_snapshot_upload", str(UPLOADER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def run_uploader_ui_server(self, module, config_path):
        server = module.http.server.ThreadingHTTPServer(("127.0.0.1", 0), module.make_config_ui_handler(config_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_uploader_config_ui_saves_only_client_exposed_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            module = self.load_uploader_module()
            config = tmp_path / "source-upload.env"
            module.write_source_config(config, {
                "CHAIN": "serverchain",
                "BUILDER_SSH": "snapupload@snapshot-builder.vpn",
                "NODE_HOME": "~/.genesisd",
                "STAGING_PATH": "/var/tmp/original-stage",
            })
            server, base_url = self.run_uploader_ui_server(module, config)
            try:
                page = urllib.request.urlopen(base_url, timeout=5).read().decode()
                self.assertIn("Snapshot upload settings", page)
                self.assertIn("Server-managed values", page)

                payload = urllib.parse.urlencode({
                    "CHAIN": "attackerchain",
                    "BUILDER_SSH": "attacker@example.invalid",
                    "BUILDER_SSH_PORT": "22",
                    "REMOTE_COMMAND": "/tmp/attacker-command",
                    "NODE_HOME": "/mnt/node/.genesisd",
                    "DATA_SUBDIR": "custom/data",
                    "SERVICE_NAME": "genesisd@sync.service",
                    "SYSTEMCTL_COMMAND": "sudo systemctl",
                    "STAGING_PATH": "/mnt/snapshot-stage/genesisd",
                    "UPLOAD_SCHEDULE": "Sun *-*-* 04:00:00",
                }).encode()
                request = urllib.request.Request(base_url, data=payload, method="POST")
                saved_page = urllib.request.urlopen(request, timeout=5).read().decode()
                self.assertIn("Saved client settings", saved_page)

                saved = module.parse_env_file(config)
                self.assertEqual(saved["CHAIN"], "serverchain")
                self.assertEqual(saved["BUILDER_SSH"], "snapupload@snapshot-builder.vpn")
                self.assertEqual(saved["BUILDER_SSH_PORT"], "2222")
                self.assertEqual(saved["REMOTE_COMMAND"], "cosmos-snapshot-remote")
                self.assertEqual(saved["NODE_HOME"], "/mnt/node/.genesisd")
                self.assertEqual(saved["DATA_SUBDIR"], "custom/data")
                self.assertEqual(saved["SERVICE_NAME"], "genesisd@sync.service")
                self.assertEqual(saved["SYSTEMCTL_COMMAND"], "sudo systemctl")
                self.assertEqual(saved["STAGING_PATH"], "/mnt/snapshot-stage/genesisd")
                self.assertEqual(saved["UPLOAD_SCHEDULE"], "Sun *-*-* 04:00:00")
            finally:
                server.shutdown()
                server.server_close()

    def test_uploader_config_ui_rejects_invalid_client_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            module = self.load_uploader_module()
            config = tmp_path / "source-upload.env"
            module.write_source_config(config, {"CHAIN": "serverchain", "STAGING_PATH": "/var/tmp/original-stage"})
            before = config.read_text()
            server, base_url = self.run_uploader_ui_server(module, config)
            try:
                valid_payload = {
                    "NODE_HOME": "~/.genesisd",
                    "DATA_SUBDIR": "data",
                    "SERVICE_NAME": "genesisd.service",
                    "SYSTEMCTL_COMMAND": "systemctl",
                    "STAGING_PATH": "/tmp/stage",
                    "UPLOAD_SCHEDULE": "Mon *-*-* 03:15:00",
                }
                invalid_cases = {
                    "NODE_HOME": "/mnt/node home/.genesisd",
                    "DATA_SUBDIR": "../data",
                    "SERVICE_NAME": "genesisd service",
                    "SYSTEMCTL_COMMAND": "service genesisd stop",
                    "STAGING_PATH": "/tmp/stage;rm-rf",
                    "UPLOAD_SCHEDULE": "Mon *-*-* 03:15:00; rm -rf /",
                }
                for field, invalid_value in invalid_cases.items():
                    with self.subTest(field=field):
                        payload_values = {**valid_payload, field: invalid_value}
                        payload = urllib.parse.urlencode(payload_values).encode()
                        request = urllib.request.Request(base_url, data=payload, method="POST")
                        with self.assertRaises(urllib.error.HTTPError) as ctx:
                            urllib.request.urlopen(request, timeout=5)
                        self.assertEqual(ctx.exception.code, 400)
                        self.assertIn("Fix the highlighted values", ctx.exception.read().decode())
                        self.assertEqual(config.read_text(), before)
            finally:
                server.shutdown()
                server.server_close()

    def test_finalizer_packages_data_excludes_validator_state_and_updates_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.make_ingest(tmp_path)
            public = tmp_path / "public"

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(public),
                "SOURCE_SUBDIR": ".",
                "VALIDATION_DATA_SUBDIR": "data",
                "COMPRESSION": "gzip",
                "KEEP_ARCHIVES": "3",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(result.stdout)
            archive = public / "examplechain" / manifest["archive"]
            checksum = public / "examplechain" / f"{manifest['archive']}.sha256"

            self.assertTrue(archive.exists())
            self.assertTrue(checksum.exists())
            self.assertEqual((public / "examplechain" / "latest.tar.gz").resolve(), archive)
            self.assertEqual((public / "examplechain" / "latest.sha256").resolve(), checksum)

            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(manifest["sha256"], digest)
            self.assertIn(digest, checksum.read_text())

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())
            self.assertIn("data/application.db/CURRENT", names)
            self.assertIn("data/state.db/CURRENT", names)
            self.assertNotIn("data/priv_validator_state.json", names)
            self.assertNotIn("config/priv_validator_key.json", names)
            self.assertNotIn("config/node_key.json", names)
            self.assertFalse(any(name.startswith("keyring-test/") for name in names))

    def test_finalizer_dry_run_does_not_write_public_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.make_ingest(tmp_path)
            public = tmp_path / "public"

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
                "--dry-run",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(public),
                "COMPRESSION": "gzip",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would package", result.stdout)
            self.assertIn("would exclude", result.stdout)
            self.assertFalse(public.exists())

    def test_finalizer_refuses_default_partial_transfer_markers(self):
        for marker in [".transfer-in-progress", ".rsync-partial", ".uploading", ".incomplete"]:
            with self.subTest(marker=marker):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    chain_root = self.make_ingest(tmp_path)
                    marker_path = chain_root / marker
                    if marker == ".rsync-partial":
                        marker_path = chain_root / "data" / marker
                    marker_path.write_text("")

                    result = self.run_cmd([
                        FINALIZER,
                        "--chain",
                        "examplechain",
                        "--config",
                        "/does/not/exist",
                    ], env={
                        **os.environ,
                        "INGEST_ROOT": str(tmp_path / "ingest"),
                        "PUBLIC_ROOT": str(tmp_path / "public"),
                        "COMPRESSION": "gzip",
                    })

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("refusing to package", result.stderr)

    def test_finalizer_retains_only_new_archive_after_successful_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.make_ingest(tmp_path)
            public_chain = tmp_path / "public" / "examplechain"
            public_chain.mkdir(parents=True)

            for stamp in ["20200101T000000Z", "20210101T000000Z"]:
                archive = public_chain / f"examplechain-{stamp}.tar.gz"
                archive.write_text("old archive")
                archive.with_name(f"{archive.name}.sha256").write_text("old checksum")
                archive.with_name(f"{archive.name}.json").write_text("{}")
            manual_archive = public_chain / "examplechain-manual.tar.gz"
            manual_archive.write_text("manual archive")

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(tmp_path / "public"),
                "COMPRESSION": "gzip",
                "KEEP_ARCHIVES": "3",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            archives = sorted(
                path.name
                for path in public_chain.glob("examplechain-*.tar.gz")
                if re.match(r"examplechain-\d{8}T\d{6}Z\.tar\.gz$", path.name)
            )
            self.assertEqual(len(archives), 1)
            self.assertNotIn("examplechain-20210101T000000Z.tar.gz", archives)
            self.assertNotIn("examplechain-20200101T000000Z.tar.gz", archives)
            self.assertTrue(manual_archive.exists())

    def test_finalizer_honors_nested_source_subdir_with_zstd(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chain_root = tmp_path / "ingest" / "examplechain"
            nested = chain_root / "snapshot" / "data"
            (nested / "application.db").mkdir(parents=True)
            (nested / "blockstore.db").mkdir()
            (nested / "state.db").mkdir()
            (nested / "application.db" / "CURRENT").write_text("db-current")
            (nested / "blockstore.db" / "CURRENT").write_text("block-current")
            (nested / "state.db" / "CURRENT").write_text("state-current")
            (nested / "priv_validator_state.json").write_text("{}")

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(tmp_path / "public"),
                "SOURCE_SUBDIR": "snapshot/data",
                "COMPRESSION": "zstd",
                "KEEP_ARCHIVES": "3",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(result.stdout)
            archive = tmp_path / "public" / "examplechain" / manifest["archive"]
            listing = subprocess.run(
                ["tar", "--use-compress-program=zstd", "-tf", archive],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout.splitlines()
            self.assertIn("snapshot/data/application.db/CURRENT", listing)
            self.assertNotIn("snapshot/data/priv_validator_state.json", listing)

    def test_upload_dry_run_marks_transfer_rsyncs_and_triggers_finalizer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = tmp_path / "node-home"
            data = node_home / "data"
            staging = tmp_path / "staging-data"
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "BUILDER_SSH_PORT=2222",
                    "SSH_OPTIONS=\"-o BatchMode=yes\"",
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "RSYNC_DELETE=1",
                    "RSYNC_EXTRA_ARGS=\"--numeric-ids --stats --info=progress2 -v -vv -P -avz\"",
                    "RUN_FINALIZER=1",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config, "--dry-run"])

            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout
            self.assertIn("systemctl stop examplechaind.service", output)
            self.assertIn(f"would validate Tendermint database at {data}", output)
            self.assertIn(f"would create staging directory {staging}", output)
            self.assertIn(f"{data}/application.db/ {staging}/application.db/", output)
            self.assertIn("cosmos-snapshot-remote prepare --chain examplechain", output)
            self.assertIn("rsync -a --partial", output)
            self.assertIn("--info=progress2", output)
            self.assertIn("--dry-run --itemize-changes", output)
            self.assertIn("--rsync-path=cosmos-snapshot-remote rsync --chain examplechain", output)
            self.assertIn("ssh -p 2222 -o BatchMode=yes", output)
            self.assertIn("snapupload@snapshot-builder.vpn:snapshot-upload-examplechain/", output)
            self.assertIn("cosmos-snapshot-remote finalize --chain examplechain", output)
            self.assertIn("systemctl start examplechaind.service", output)
            self.assertNotIn("/ingest", output)

    def test_upload_bootstrap_dry_run_streams_archive_then_verifies_and_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = tmp_path / "node-home"
            staging = tmp_path / "staging-data"
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "BUILDER_SSH_PORT=2222",
                    "SSH_OPTIONS=\"-o BatchMode=yes\"",
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "BOOTSTRAP_UPLOAD=1",
                    "RUN_FINALIZER=1",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config, "--dry-run"])

            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout
            self.assertIn("cosmos-snapshot-remote prepare --chain examplechain", output)
            self.assertIn(f"tar -C {staging} -cf - . | ssh -p 2222 -o BatchMode=yes", output)
            self.assertIn("cosmos-snapshot-remote bootstrap --chain examplechain", output)
            self.assertIn("--dry-run --itemize-changes", output)
            self.assertIn("cosmos-snapshot-remote finalize --chain examplechain", output)
            self.assertNotIn("/ingest", output)

    def test_upload_stages_public_db_dirs_then_rsyncs_and_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            source = node_home / "data"
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "rsync", log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "BUILDER_SSH_PORT=2222",
                    "SSH_OPTIONS=\"-o BatchMode=yes\"",
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "RSYNC_DELETE=1",
                    "RSYNC_EXTRA_ARGS=\"--numeric-ids --stats --info=progress2 -v -vv -P -avz\"",
                    "RUN_FINALIZER=1",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_RSYNC_VERIFY_PROGRESS": "1",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            entries = self.read_json_lines(log_path)
            self.assertEqual(entries[0], ["systemctl", ["stop", "examplechaind.service"]])
            self.assertEqual(entries[6], ["systemctl", ["start", "examplechaind.service"]])
            self.assertEqual(entries[7][0], "ssh")
            self.assertEqual(entries[8][0], "rsync")
            self.assertEqual(entries[9][0], "rsync")
            self.assertEqual(entries[10][0], "ssh")
            self.assertIn("cosmos-snapshot-remote prepare --chain examplechain", entries[7][1][-1])
            self.assertTrue(any(arg.startswith("--rsync-path=cosmos-snapshot-remote rsync --chain examplechain") for arg in entries[8][1]))
            self.assertEqual(entries[8][1][-2], f"{staging}/")
            self.assertIn("--stats", entries[8][1])
            self.assertIn("-v", entries[8][1])
            self.assertIn("-vv", entries[8][1])
            self.assertIn("-P", entries[8][1])
            self.assertIn("-avz", entries[8][1])
            self.assertIn("snapupload@snapshot-builder.vpn:snapshot-upload-examplechain/", entries[8][1][-1])
            self.assertIn("--dry-run", entries[9][1])
            self.assertIn("--itemize-changes", entries[9][1])
            self.assertIn("--checksum", entries[9][1])
            self.assertNotIn("--info=progress2", entries[9][1])
            self.assertNotIn("--stats", entries[9][1])
            self.assertNotIn("-v", entries[9][1])
            self.assertNotIn("-vv", entries[9][1])
            self.assertNotIn("-P", entries[9][1])
            self.assertNotIn("-avz", entries[9][1])
            self.assertIn("-az", entries[9][1])
            self.assertEqual(entries[9][1][-2], f"{staging}/")
            self.assertIn("cosmos-snapshot-remote finalize --chain examplechain", entries[10][1][-1])
            self.assertTrue((staging / "application.db" / "CURRENT").exists())
            self.assertTrue((staging / "blockstore.db" / "CURRENT").exists())
            self.assertTrue((staging / "state.db" / "CURRENT").exists())
            self.assertTrue((staging / "evidence.db" / "CURRENT").exists())
            self.assertTrue((staging / "tx_index.db" / "CURRENT").exists())
            self.assertFalse((staging / "priv_validator_state.json").exists())
            self.assertFalse((staging / "snapshots").exists())
            self.assertFalse((staging / "cs.wal").exists())
            self.assertFalse((staging / "config").exists())
            self.assertFalse((staging / "keyring-test").exists())
            self.assertFalse((staging / "ibc_08-wasm_client_data").exists())

    def test_upload_expands_tilde_node_home_and_staging_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            self.make_source_node_home(home, ".genesisd")
            staging = home / "snapshot-stage"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "ssh", log_path)
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join([
                    "CHAIN=\"examplechain\"",
                    "NODE_HOME=\"~/.genesisd\"",
                    "DATA_SUBDIR=\"data\"",
                    "STAGING_PATH=\"~/snapshot-stage\"",
                    "SERVICE_NAME=\"examplechaind.service\"",
                    "BUILDER_SSH=\"snapupload@snapshot-builder.vpn\"",
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "RUN_FINALIZER=0",
                ])
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "HOME": str(home),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((staging / "application.db" / "CURRENT").exists())
            self.assertFalse((staging / "config").exists())

    def test_upload_honors_custom_data_subdir_under_node_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path, data_subdir="custom-data")
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "ssh", log_path)
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(
                    node_home,
                    staging,
                    ["REMOTE_COMMAND=\"cosmos-snapshot-remote\"", "RUN_FINALIZER=0"],
                    data_subdir="custom-data",
                ))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            entries = self.read_json_lines(log_path)
            self.assertIn(f"{node_home}/custom-data/application.db/", entries[1][1][-2])
            self.assertTrue((staging / "application.db" / "CURRENT").exists())

    def test_upload_rejects_escaping_data_subdir_before_stopping_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(
                    node_home,
                    staging,
                    ["REMOTE_COMMAND=\"cosmos-snapshot-remote\""],
                    data_subdir="../other-data",
                ))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DATA_SUBDIR must be a relative path under NODE_HOME", result.stderr)
            self.assertFalse(log_path.exists())

    def test_upload_rejects_symlinked_data_subdir_escape_before_stopping_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            shutil.rmtree(node_home / "data")
            outside = self.make_source_node_home(tmp_path, name="outside-node") / "data"
            (node_home / "data").symlink_to(outside, target_is_directory=True)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text("\n".join(self.upload_config_lines(node_home, staging)))

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DATA_SUBDIR must resolve under NODE_HOME", result.stderr)
            self.assertFalse(log_path.exists())

    def test_upload_rejects_source_db_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            application_db = node_home / "data" / "application.db"
            shutil.rmtree(application_db)
            outside = tmp_path / "outside-application.db"
            outside.mkdir()
            (outside / "CURRENT").write_text("outside-current")
            application_db.symlink_to(outside, target_is_directory=True)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text("\n".join(self.upload_config_lines(node_home, staging)))

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source DB directory must not be a symlink", result.stderr)
            self.assertFalse((staging / "application.db").exists())
            self.assertEqual(self.read_json_lines(log_path), [
                ["systemctl", ["stop", "examplechaind.service"]],
                ["systemctl", ["start", "examplechaind.service"]],
            ])

    def test_upload_rejects_optional_source_db_symlink_before_staging_any_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            evidence_db = node_home / "data" / "evidence.db"
            shutil.rmtree(evidence_db)
            outside = tmp_path / "outside-evidence.db"
            outside.mkdir()
            (outside / "CURRENT").write_text("outside-current")
            evidence_db.symlink_to(outside, target_is_directory=True)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text("\n".join(self.upload_config_lines(node_home, staging)))

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source DB directory must not be a symlink", result.stderr)
            self.assertFalse((staging / "application.db").exists())
            self.assertEqual(self.read_json_lines(log_path), [
                ["systemctl", ["stop", "examplechaind.service"]],
                ["systemctl", ["start", "examplechaind.service"]],
            ])

    def test_upload_rejects_broken_optional_source_db_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            evidence_db = node_home / "data" / "evidence.db"
            shutil.rmtree(evidence_db)
            evidence_db.symlink_to(tmp_path / "missing-evidence.db", target_is_directory=True)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text("\n".join(self.upload_config_lines(node_home, staging)))

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source DB directory must not be a symlink", result.stderr)
            self.assertFalse((staging / "application.db").exists())
            self.assertEqual(self.read_json_lines(log_path), [
                ["systemctl", ["stop", "examplechaind.service"]],
                ["systemctl", ["start", "examplechaind.service"]],
            ])

    def test_upload_rejects_existing_staging_db_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            staging = tmp_path / "staging-data"
            outside = tmp_path / "outside-staging"
            outside.mkdir()
            staging.mkdir()
            (staging / "application.db").symlink_to(outside, target_is_directory=True)
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text("\n".join(self.upload_config_lines(node_home, staging)))

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("staging DB directory must not be a symlink", result.stderr)
            self.assertFalse((outside / "CURRENT").exists())
            self.assertEqual(self.read_json_lines(log_path), [
                ["systemctl", ["stop", "examplechaind.service"]],
                ["systemctl", ["start", "examplechaind.service"]],
            ])

    def test_upload_rejects_broken_staging_db_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            staging = tmp_path / "staging-data"
            staging.mkdir()
            (staging / "application.db").symlink_to(tmp_path / "missing-staging-target", target_is_directory=True)
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text("\n".join(self.upload_config_lines(node_home, staging)))

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("staging DB directory must not be a symlink", result.stderr)
            self.assertEqual(self.read_json_lines(log_path), [
                ["systemctl", ["stop", "examplechaind.service"]],
                ["systemctl", ["start", "examplechaind.service"]],
            ])

    def test_upload_bootstrap_flag_streams_staged_archive_then_verifies_and_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            source = node_home / "data"
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            payload_path = tmp_path / "bootstrap.tar"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_ssh_capturing_stdin(bin_dir, log_path, payload_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "BUILDER_SSH_PORT=2222",
                    "SSH_OPTIONS=\"-o BatchMode=yes\"",
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "RUN_FINALIZER=1",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config, "--bootstrap"], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            entries = self.read_json_lines(log_path)
            self.assertEqual(entries[0], ["systemctl", ["stop", "examplechaind.service"]])
            self.assertEqual(entries[6], ["systemctl", ["start", "examplechaind.service"]])
            self.assertIn("cosmos-snapshot-remote prepare --chain examplechain", entries[7][1][-1])
            self.assertIn("cosmos-snapshot-remote bootstrap --chain examplechain", entries[8][1][-1])
            self.assertGreater(entries[8][2], 0)
            self.assertEqual(entries[9][0], "rsync")
            self.assertIn("--dry-run", entries[9][1])
            self.assertIn("--checksum", entries[9][1])
            self.assertNotIn("--info=progress2", entries[9][1])
            self.assertEqual(entries[9][1][-2], f"{staging}/")
            self.assertIn("cosmos-snapshot-remote finalize --chain examplechain", entries[10][1][-1])
            self.assertTrue((staging / "application.db" / "CURRENT").exists())
            self.assertFalse((staging / "priv_validator_state.json").exists())
            self.assertFalse((staging / "snapshots").exists())
            with tarfile.open(payload_path, "r:") as archive:
                names = set(archive.getnames())
            self.assertIn("./application.db/CURRENT", names)
            self.assertIn("./blockstore.db/CURRENT", names)
            self.assertIn("./state.db/CURRENT", names)
            self.assertNotIn("./priv_validator_state.json", names)
            self.assertFalse(any("snapshots" in name for name in names))

    def test_upload_bootstrap_config_streams_staged_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_ssh_receiving_stdin(bin_dir, log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "BOOTSTRAP_UPLOAD=1",
                    "RUN_FINALIZER=0",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            entries = self.read_json_lines(log_path)
            self.assertIn("cosmos-snapshot-remote bootstrap --chain examplechain", entries[8][1][-1])
            self.assertGreater(entries[8][2], 0)
            self.assertFalse(any(entry[0] == "ssh" and "finalize" in entry[1][-1] for entry in entries))

    def test_upload_does_not_finalize_after_rsync_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "ssh", log_path)
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "VERIFY_REMOTE_RSYNC=0",
                    "RUN_FINALIZER=1",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_RSYNC_REMOTE_EXIT": "23",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("transfer failed", result.stderr)
            entries = self.read_json_lines(log_path)
            self.assertEqual(entries[0], ["systemctl", ["stop", "examplechaind.service"]])
            self.assertEqual(entries[6], ["systemctl", ["start", "examplechaind.service"]])
            self.assertIn("cosmos-snapshot-remote prepare --chain examplechain", entries[7][1][-1])
            self.assertEqual(entries[-1][0], "rsync")
            self.assertFalse(any(entry[0] == "ssh" and "finalize" in entry[1][-1] for entry in entries))

    def test_upload_restarts_service_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = tmp_path / "node-home"
            data = node_home / "data"
            (data / "application.db").mkdir(parents=True)
            (data / "application.db" / "CURRENT").write_text("db-current")
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing required Tendermint DB directory", result.stderr)
            entries = self.read_json_lines(log_path)
            self.assertEqual(entries, [
                ["systemctl", ["stop", "examplechaind.service"]],
                ["systemctl", ["start", "examplechaind.service"]],
            ])

    def test_upload_refuses_to_stage_non_database_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            self.write_fake_command(bin_dir, "ssh", log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "STAGE_DB_DIRS=\"application.db snapshots\"",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("staged DB directory is not allowed: snapshots", result.stderr)
            self.assertFalse((staging / "snapshots").exists())
            entries = self.read_json_lines(log_path)
            self.assertEqual(entries, [
                ["systemctl", ["stop", "examplechaind.service"]],
                ["systemctl", ["start", "examplechaind.service"]],
            ])

    def test_upload_remote_verification_failure_prevents_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            node_home = self.make_source_node_home(tmp_path)
            staging = tmp_path / "staging-data"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "commands.jsonl"
            bin_dir.mkdir()
            self.write_fake_command(bin_dir, "ssh", log_path)
            self.write_fake_command(bin_dir, "systemctl", log_path)
            self.write_fake_rsync(bin_dir, log_path)
            config = tmp_path / "upload.env"
            config.write_text(
                "\n".join(self.upload_config_lines(node_home, staging, [
                    "REMOTE_COMMAND=\"cosmos-snapshot-remote\"",
                    "VERIFY_REMOTE_RSYNC=1",
                    "RUN_FINALIZER=1",
                ]))
            )

            result = self.run_cmd([UPLOADER, "--config", config], env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_RSYNC_VERIFY_CHANGES": "1",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("remote rsync verification found pending changes", result.stderr)
            entries = self.read_json_lines(log_path)
            self.assertIn("--dry-run", entries[-1][1])
            self.assertIn("--checksum", entries[-1][1])
            self.assertNotIn("--info=progress2", entries[-1][1])
            self.assertFalse(any(entry[0] == "ssh" and "finalize" in entry[1][-1] for entry in entries))

    def test_finalizer_rejects_non_tendermint_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data = tmp_path / "ingest" / "examplechain" / "data"
            data.mkdir(parents=True)
            (data / "random.txt").write_text("not a node database")

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(tmp_path / "public"),
                "COMPRESSION": "gzip",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing required Tendermint DB directory", result.stderr)

    def test_finalizer_rejects_empty_required_db_dirs_and_keeps_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data = tmp_path / "ingest" / "examplechain" / "data"
            (data / "application.db").mkdir(parents=True)
            (data / "blockstore.db").mkdir()
            (data / "state.db").mkdir()
            public_chain = tmp_path / "public" / "examplechain"
            public_chain.mkdir(parents=True)
            previous_archive = public_chain / "examplechain-20210101T000000Z.tar.gz"
            previous_archive.write_text("previous")
            previous_sha = public_chain / f"{previous_archive.name}.sha256"
            previous_json = public_chain / f"{previous_archive.name}.json"
            previous_sha.write_text("sha")
            previous_json.write_text("{}")
            (public_chain / "latest.tar.gz").symlink_to(previous_archive.name)
            (public_chain / "latest.sha256").symlink_to(previous_sha.name)
            (public_chain / "latest.json").symlink_to(previous_json.name)

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(tmp_path / "public"),
                "COMPRESSION": "gzip",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no recognizable DB marker", result.stderr)
            self.assertEqual(os.readlink(public_chain / "latest.tar.gz"), previous_archive.name)
            self.assertEqual(os.readlink(public_chain / "latest.sha256"), previous_sha.name)
            self.assertEqual(os.readlink(public_chain / "latest.json"), previous_json.name)

    def test_finalizer_refuses_empty_required_db_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data = tmp_path / "ingest" / "examplechain" / "data"
            data.mkdir(parents=True)
            (data / "random.txt").write_text("not a node database")

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(tmp_path / "public"),
                "COMPRESSION": "gzip",
                "REQUIRED_DB_DIRS": "",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("REQUIRED_DB_DIRS must list", result.stderr)
            self.assertFalse((tmp_path / "public").exists())

    def test_finalizer_rejects_chain_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "../examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(tmp_path / "public"),
                "COMPRESSION": "gzip",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid chain name", result.stderr)

    def test_finalizer_rejects_source_subdir_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.make_ingest(tmp_path)

            result = self.run_cmd([
                FINALIZER,
                "--chain",
                "examplechain",
                "--config",
                "/does/not/exist",
            ], env={
                **os.environ,
                "INGEST_ROOT": str(tmp_path / "ingest"),
                "PUBLIC_ROOT": str(tmp_path / "public"),
                "SOURCE_SUBDIR": "..",
                "COMPRESSION": "gzip",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source path escapes chain ingest path", result.stderr)

    def test_remote_wrapper_enforces_client_chain_access_for_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            allowed = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "prepare",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
            ])
            denied = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "prepare",
                "--chain",
                "otherchain",
                "--client-id",
                "example-source",
            ])

            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertTrue((ingest / "examplechain" / ".transfer-in-progress").exists())
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("not allowed", denied.stderr)

    def test_remote_wrapper_rejects_client_id_argument_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                ])
            )

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "prepare",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
            ], env={
                **os.environ,
                "ALLOW_CLIENT_ID_ARG": "1",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--client-id is disabled", result.stderr)
            self.assertFalse((ingest / "examplechain" / ".transfer-in-progress").exists())

    def test_remote_wrapper_ignores_ambient_security_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            trusted_access = tmp_path / "trusted-access.json"
            attacker_access = tmp_path / "attacker-access.json"
            trusted_config = tmp_path / "trusted.env"
            trusted_ingest = tmp_path / "trusted-ingest"
            attacker_ingest = tmp_path / "attacker-ingest"
            trusted_access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            attacker_access.write_text(json.dumps({"clients": {"example-source": {"chains": ["otherchain"]}}}))
            trusted_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{trusted_ingest}\"",
                    f"ACCESS_CONFIG=\"{trusted_access}\"",
                    "ALLOW_CLIENT_ID_ARG=0",
                ])
            )

            result = self.run_cmd([
                REMOTE,
                "--config",
                trusted_config,
                "prepare",
                "--chain",
                "otherchain",
            ], env={
                **os.environ,
                "COSMOS_SNAPSHOT_CLIENT_ID": "example-source",
                "ACCESS_CONFIG": str(attacker_access),
                "INGEST_ROOT": str(attacker_ingest),
                "ALLOW_CLIENT_ID_ARG": "1",
                "FINALIZER_COMMAND": "/bin/false",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not allowed", result.stderr)
            self.assertFalse((trusted_ingest / "otherchain" / ".transfer-in-progress").exists())
            self.assertFalse((attacker_ingest / "otherchain" / ".transfer-in-progress").exists())

            allowed = self.run_cmd([
                REMOTE,
                "--config",
                trusted_config,
                "prepare",
                "--chain",
                "examplechain",
            ], env={
                **os.environ,
                "COSMOS_SNAPSHOT_CLIENT_ID": "example-source",
                "ACCESS_CONFIG": str(attacker_access),
                "INGEST_ROOT": str(attacker_ingest),
            })

            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertTrue((trusted_ingest / "examplechain" / ".transfer-in-progress").exists())
            self.assertFalse((attacker_ingest / "examplechain" / ".transfer-in-progress").exists())

    def test_remote_config_path_must_come_from_trusted_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            attacker_access = tmp_path / "attacker-access.json"
            attacker_config = tmp_path / "attacker.env"
            attacker_ingest = tmp_path / "attacker-ingest"
            attacker_access.write_text(json.dumps({"clients": {"example-source": {"chains": ["otherchain"]}}}))
            attacker_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{attacker_ingest}\"",
                    f"ACCESS_CONFIG=\"{attacker_access}\"",
                ])
            )

            result = self.run_cmd([
                REMOTE,
            ], env={
                **os.environ,
                "COSMOS_SNAPSHOT_CLIENT_ID": "example-source",
                "COSMOS_SNAPSHOT_REMOTE_CONFIG": str(attacker_config),
                "SSH_ORIGINAL_COMMAND": "cosmos-snapshot-remote prepare --chain otherchain",
            })

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--config is required in trusted wrapper arguments", result.stderr)
            self.assertFalse((attacker_ingest / "otherchain" / ".transfer-in-progress").exists())

    def test_remote_wrapper_denies_unallowed_rsync_finalize_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            cases = [
                ["rsync", "--chain", "otherchain", "--client-id", "example-source", "--server", ".", str(ingest / "otherchain" / "data")],
                ["bootstrap", "--chain", "otherchain", "--client-id", "example-source"],
                ["finalize", "--chain", "otherchain", "--client-id", "example-source"],
                ["status", "--chain", "otherchain", "--client-id", "example-source"],
            ]
            for args in cases:
                with self.subTest(operation=args[0]):
                    result = self.run_cmd([REMOTE, "--config", remote_config, *args])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("not allowed", result.stderr)

    def test_remote_wrapper_accepts_forced_command_original_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                ])
            )

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
            ], env={
                **os.environ,
                "COSMOS_SNAPSHOT_CLIENT_ID": "example-source",
                "SSH_ORIGINAL_COMMAND": "cosmos-snapshot-remote prepare --chain examplechain",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((ingest / "examplechain" / ".transfer-in-progress").exists())

    def test_remote_wrapper_uses_configured_host_rsync_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "rsync-args.json"
            bin_dir.mkdir()
            fake_rsync = bin_dir / "rsync"
            fake_rsync.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"open({str(log_path)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            )
            fake_rsync.chmod(fake_rsync.stat().st_mode | stat.S_IXUSR)
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    f"RSYNC_COMMAND=\"{fake_rsync}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )
            marker = ingest / "examplechain" / ".transfer-in-progress"
            marker.parent.mkdir(parents=True)
            marker.write_text("")

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "rsync",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
                "--server",
                ".",
                str(tmp_path / "outside"),
            ])

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(log_path.read_text()), ["--server", ".", str(ingest / "examplechain" / "data")])

    def test_remote_rsync_requires_prepare_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "rsync",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
                "--server",
                ".",
                str(tmp_path / "outside"),
            ])

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing transfer marker", result.stderr)

    def test_remote_wrapper_allows_rsync_under_chain_data_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            bin_dir = tmp_path / "bin"
            log_path = tmp_path / "rsync-args.json"
            bin_dir.mkdir()
            fake_rsync = bin_dir / "rsync"
            fake_rsync.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                f"open({str(log_path)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            )
            fake_rsync.chmod(fake_rsync.stat().st_mode | stat.S_IXUSR)
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    f"RSYNC_COMMAND=\"{fake_rsync}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            destination = ingest / "examplechain" / "data"
            destination.mkdir(parents=True)
            (ingest / "examplechain" / ".transfer-in-progress").write_text("")
            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "rsync",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
                "--server",
                "--sender",
                ".",
                str(destination),
            ])

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(log_path.read_text()), ["--server", "--sender", ".", str(destination)])

    def test_remote_rejects_data_symlink_escape_for_rsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            outside = tmp_path / "outside-data"
            outside.mkdir()
            chain_root = ingest / "examplechain"
            chain_root.mkdir(parents=True)
            (chain_root / "data").symlink_to(outside, target_is_directory=True)
            (chain_root / ".transfer-in-progress").write_text("")
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "rsync",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
                "--server",
                ".",
                str(tmp_path / "outside"),
            ])

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("data path must not be a symlink", result.stderr)

    def test_remote_rejects_data_symlink_escape_for_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            outside = tmp_path / "outside-data"
            outside.mkdir()
            chain_root = ingest / "examplechain"
            chain_root.mkdir(parents=True)
            (chain_root / "data").symlink_to(outside, target_is_directory=True)
            (chain_root / ".transfer-in-progress").write_text("")
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE),
                    "--config",
                    str(remote_config),
                    "bootstrap",
                    "--chain",
                    "examplechain",
                    "--client-id",
                    "example-source",
                ],
                input=b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("data path must not be a symlink", result.stderr.decode())

    def test_remote_bootstrap_requires_prepare_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            archive_buffer = io.BytesIO()
            with tarfile.open(fileobj=archive_buffer, mode="w") as tar:
                member = tarfile.TarInfo("application.db/CURRENT")
                payload = b"db-current"
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE),
                    "--config",
                    str(remote_config),
                    "bootstrap",
                    "--chain",
                    "examplechain",
                    "--client-id",
                    "example-source",
                ],
                input=archive_buffer.getvalue(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing transfer marker", result.stderr.decode())
            self.assertFalse((ingest / "examplechain" / "data").exists())

    def test_remote_bootstrap_extracts_archive_to_configured_ingest_data_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            archive = tmp_path / "bootstrap.tar"
            source = tmp_path / "source"
            for db_dir in ["application.db", "blockstore.db", "state.db"]:
                (source / db_dir).mkdir(parents=True)
                (source / db_dir / "CURRENT").write_text(f"{db_dir}-current")
            with tarfile.open(archive, "w") as tar:
                tar.add(source, arcname=".")
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )
            stale = ingest / "examplechain" / "data" / "stale.db"
            stale.mkdir(parents=True)
            (stale / "CURRENT").write_text("old")
            marker = ingest / "examplechain" / ".transfer-in-progress"
            marker.write_text("")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE),
                    "--config",
                    str(remote_config),
                    "bootstrap",
                    "--chain",
                    "examplechain",
                    "--client-id",
                    "example-source",
                ],
                input=archive.read_bytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            data = ingest / "examplechain" / "data"
            self.assertTrue((data / "application.db" / "CURRENT").exists())
            self.assertTrue((data / "blockstore.db" / "CURRENT").exists())
            self.assertTrue((data / "state.db" / "CURRENT").exists())
            self.assertFalse(stale.exists())
            self.assertTrue(marker.exists())

    def test_remote_bootstrap_rejects_unsafe_archive_members(self):
        cases = [
            ("parent escape", "../escape.txt", tarfile.REGTYPE, "", "escapes data root"),
            ("absolute path", "/escape.txt", tarfile.REGTYPE, "", "escapes data root"),
            ("symlink", "link", tarfile.SYMTYPE, "/etc/passwd", "member type is not allowed"),
            ("hardlink", "hardlink", tarfile.LNKTYPE, "application.db/CURRENT", "member type is not allowed"),
        ]
        for name, member_name, member_type, linkname, expected_error in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    access = tmp_path / "access.json"
                    remote_config = tmp_path / "remote.env"
                    ingest = tmp_path / "ingest"
                    archive_buffer = io.BytesIO()
                    with tarfile.open(fileobj=archive_buffer, mode="w") as tar:
                        member = tarfile.TarInfo(member_name)
                        member.type = member_type
                        if member_type == tarfile.REGTYPE:
                            payload = b"escape"
                            member.size = len(payload)
                            tar.addfile(member, io.BytesIO(payload))
                        else:
                            member.linkname = linkname
                            tar.addfile(member)
                    access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
                    remote_config.write_text(
                        "\n".join([
                            f"INGEST_ROOT=\"{ingest}\"",
                            f"ACCESS_CONFIG=\"{access}\"",
                            "ALLOW_CLIENT_ID_ARG=1",
                        ])
                    )
                    marker = ingest / "examplechain" / ".transfer-in-progress"
                    marker.parent.mkdir(parents=True)
                    marker.write_text("")

                    result = subprocess.run(
                        [
                            sys.executable,
                            str(REMOTE),
                            "--config",
                            str(remote_config),
                            "bootstrap",
                            "--chain",
                            "examplechain",
                            "--client-id",
                            "example-source",
                        ],
                        input=archive_buffer.getvalue(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected_error, result.stderr.decode())
                    self.assertFalse((ingest / "escape.txt").exists())
                    self.assertFalse((ingest / "examplechain" / "data").exists())

    def test_remote_bootstrap_failed_extraction_preserves_existing_data_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            archive_buffer = io.BytesIO()
            with tarfile.open(fileobj=archive_buffer, mode="w") as tar:
                safe = tarfile.TarInfo("application.db/CURRENT")
                safe_payload = b"new"
                safe.size = len(safe_payload)
                tar.addfile(safe, io.BytesIO(safe_payload))
                unsafe = tarfile.TarInfo("../escape.txt")
                unsafe_payload = b"escape"
                unsafe.size = len(unsafe_payload)
                tar.addfile(unsafe, io.BytesIO(unsafe_payload))
            chain_root = ingest / "examplechain"
            existing = chain_root / "data" / "application.db"
            existing.mkdir(parents=True)
            (existing / "CURRENT").write_text("old")
            (chain_root / ".transfer-in-progress").write_text("")
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE),
                    "--config",
                    str(remote_config),
                    "bootstrap",
                    "--chain",
                    "examplechain",
                    "--client-id",
                    "example-source",
                ],
                input=archive_buffer.getvalue(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("escapes data root", result.stderr.decode())
            self.assertEqual((existing / "CURRENT").read_text(), "old")
            self.assertFalse((chain_root / ".bootstrap-data.tmp").exists())
            self.assertFalse((ingest / "escape.txt").exists())

    def test_remote_finalize_removes_marker_and_invokes_finalizer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            finalizer_log = tmp_path / "finalizer.json"
            fake_finalizer = tmp_path / "fake-finalizer"
            fake_finalizer.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"open({str(finalizer_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            )
            fake_finalizer.chmod(fake_finalizer.stat().st_mode | stat.S_IXUSR)
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    f"FINALIZER_COMMAND=\"{fake_finalizer} --config /tmp/finalizer.env\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )
            marker = ingest / "examplechain" / ".transfer-in-progress"
            marker.parent.mkdir(parents=True)
            marker.write_text("")

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "finalize",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
            ])

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads(finalizer_log.read_text()), ["--config", "/tmp/finalizer.env", "--chain", "examplechain"])

    def test_remote_finalize_restores_marker_when_finalizer_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            failing_finalizer = tmp_path / "failing-finalizer"
            failing_finalizer.write_text("#!/bin/sh\nexit 42\n")
            failing_finalizer.chmod(failing_finalizer.stat().st_mode | stat.S_IXUSR)
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    f"FINALIZER_COMMAND=\"{failing_finalizer}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )
            marker = ingest / "examplechain" / ".transfer-in-progress"
            marker.parent.mkdir(parents=True)
            marker.write_text("")

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "finalize",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
            ])

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(marker.exists())

    def test_remote_finalize_ignores_ambient_finalizer_command_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            access = tmp_path / "access.json"
            remote_config = tmp_path / "remote.env"
            ingest = tmp_path / "ingest"
            trusted_log = tmp_path / "trusted-finalizer.json"
            attacker_log = tmp_path / "attacker-finalizer.json"
            trusted_finalizer = tmp_path / "trusted-finalizer"
            attacker_finalizer = tmp_path / "attacker-finalizer"
            trusted_finalizer.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"open({str(trusted_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            )
            attacker_finalizer.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"open({str(attacker_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                "raise SystemExit(99)\n"
            )
            trusted_finalizer.chmod(trusted_finalizer.stat().st_mode | stat.S_IXUSR)
            attacker_finalizer.chmod(attacker_finalizer.stat().st_mode | stat.S_IXUSR)
            access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            remote_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{ingest}\"",
                    f"ACCESS_CONFIG=\"{access}\"",
                    f"FINALIZER_COMMAND=\"{trusted_finalizer}\"",
                    "ALLOW_CLIENT_ID_ARG=1",
                ])
            )
            marker = ingest / "examplechain" / ".transfer-in-progress"
            marker.parent.mkdir(parents=True)
            marker.write_text("")

            result = self.run_cmd([
                REMOTE,
                "--config",
                remote_config,
                "finalize",
                "--chain",
                "examplechain",
                "--client-id",
                "example-source",
            ], env={
                **os.environ,
                "FINALIZER_COMMAND": str(attacker_finalizer),
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(trusted_log.read_text()), ["--chain", "examplechain"])
            self.assertFalse(attacker_log.exists())

    def test_remote_original_command_cannot_override_trusted_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            trusted_access = tmp_path / "trusted-access.json"
            attacker_access = tmp_path / "attacker-access.json"
            trusted_config = tmp_path / "trusted.env"
            attacker_config = tmp_path / "attacker.env"
            trusted_ingest = tmp_path / "trusted-ingest"
            attacker_ingest = tmp_path / "attacker-ingest"
            trusted_access.write_text(json.dumps({"clients": {"example-source": {"chains": ["examplechain"]}}}))
            attacker_access.write_text(json.dumps({"clients": {"example-source": {"chains": ["otherchain"]}}}))
            trusted_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{trusted_ingest}\"",
                    f"ACCESS_CONFIG=\"{trusted_access}\"",
                ])
            )
            attacker_config.write_text(
                "\n".join([
                    f"INGEST_ROOT=\"{attacker_ingest}\"",
                    f"ACCESS_CONFIG=\"{attacker_access}\"",
                ])
            )

            for injected_config_arg in [
                f"--config {attacker_config}",
                f"--config={attacker_config}",
            ]:
                with self.subTest(injected_config_arg=injected_config_arg):
                    result = self.run_cmd([
                        REMOTE,
                        "--config",
                        trusted_config,
                    ], env={
                        **os.environ,
                        "COSMOS_SNAPSHOT_CLIENT_ID": "example-source",
                        "SSH_ORIGINAL_COMMAND": f"cosmos-snapshot-remote {injected_config_arg} prepare --chain otherchain",
                    })

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("may not set wrapper option: --config", result.stderr)
                    self.assertFalse((attacker_ingest / "otherchain" / ".transfer-in-progress").exists())


if __name__ == "__main__":
    unittest.main()

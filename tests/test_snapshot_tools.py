import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / "scripts" / "cosmos-snapshot-finalize"
UPLOADER = ROOT / "scripts" / "cosmos-snapshot-upload"


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
        (data / "state.db").mkdir()
        (data / "application.db" / "CURRENT").write_text("db-current")
        (data / "state.db" / "CURRENT").write_text("state-current")
        (data / "priv_validator_state.json").write_text('{"height":"1"}')
        (chain_root / "config").mkdir()
        (chain_root / "config" / "priv_validator_key.json").write_text('{"secret":"validator"}')
        (chain_root / "config" / "node_key.json").write_text('{"secret":"node"}')
        (chain_root / "keyring-test").mkdir()
        (chain_root / "keyring-test" / "key").write_text("secret")
        return chain_root

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

    def test_finalizer_retains_newest_archives_after_successful_publish(self):
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
                "KEEP_ARCHIVES": "2",
            })

            self.assertEqual(result.returncode, 0, result.stderr)
            archives = sorted(
                path.name
                for path in public_chain.glob("examplechain-*.tar.gz")
                if re.match(r"examplechain-\d{8}T\d{6}Z\.tar\.gz$", path.name)
            )
            self.assertEqual(len(archives), 2)
            self.assertIn("examplechain-20210101T000000Z.tar.gz", archives)
            self.assertNotIn("examplechain-20200101T000000Z.tar.gz", archives)
            self.assertTrue(manual_archive.exists())

    def test_finalizer_honors_nested_source_subdir_with_zstd(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chain_root = tmp_path / "ingest" / "examplechain"
            nested = chain_root / "snapshot" / "data"
            (nested / "application.db").mkdir(parents=True)
            (nested / "application.db" / "CURRENT").write_text("db-current")
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
            source = Path(tmp) / "source-data"
            source.mkdir()
            config = Path(tmp) / "upload.env"
            config.write_text(
                "\n".join([
                    "CHAIN=\"examplechain\"",
                    f"SOURCE_PATH=\"{source}\"",
                    "BUILDER_SSH=\"snapupload@snapshot-builder.example\"",
                    "BUILDER_SSH_PORT=2222",
                    "SSH_OPTIONS=\"-o BatchMode=yes\"",
                    "REMOTE_INGEST_ROOT=\"/ingest\"",
                    "FINALIZER_COMMAND=\"sudo /usr/local/sbin/cosmos-snapshot-finalize --config /etc/cosmos-snapshot-finalizer.env\"",
                    "RSYNC_DELETE=1",
                    "RSYNC_EXTRA_ARGS=\"--numeric-ids\"",
                    "RUN_FINALIZER=1",
                ])
            )

            result = self.run_cmd([UPLOADER, "--config", config, "--dry-run"])

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertIn("touch /ingest/examplechain/.transfer-in-progress", lines[0])
            self.assertIn("rsync -a --partial --info=progress2 --delete --numeric-ids", lines[1])
            self.assertIn("ssh -p 2222 -o BatchMode=yes", lines[1])
            self.assertIn("snapupload@snapshot-builder.example:/ingest/examplechain/data/", lines[1])
            self.assertIn("rm -f /ingest/examplechain/.transfer-in-progress", lines[2])
            self.assertIn("cosmos-snapshot-finalize --config /etc/cosmos-snapshot-finalizer.env --chain examplechain", lines[3])


if __name__ == "__main__":
    unittest.main()

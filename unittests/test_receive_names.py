#!/usr/bin/env python3
"""Unit tests for received-subvolume name parsing (no stem guessing)."""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()

STREAM = Path("corinne.202610150423.btrfs")
REAL_NAME = "corinne.202610150423"
CFG = {"verbose": 0, "dry_run": False}


def _make_stream(cmd: int, name: str) -> bytes:
    """Minimal CRC-valid btrfs send stream header (magic + version + first command).

    Mirrors what `btrfs send` emits: the first command is SUBVOL for a full send
    and SNAPSHOT for an incremental (parent-bearing) send, with the received path
    as a PATH TLV.
    """
    body = struct.pack("<HH", bs.BTRFS_SEND_A_PATH, len(name)) + name.encode()
    region = struct.pack("<IHI", len(body), cmd, 0) + body
    crc = bs.crc32c(region, 0)
    return (bs.BTRFS_STREAM_MAGIC + struct.pack("<I", 1)
            + struct.pack("<IHI", len(body), cmd, crc) + body)


def _stream_file(embedded_name: str, filename: str, cmd: int = bs.BTRFS_SEND_C_SNAPSHOT) -> Path:
    """Write a valid stream whose embedded snapshot name may differ from the filename."""
    tmp = tempfile.mkdtemp(prefix="bubtrsnap-stream-")
    p = Path(tmp) / filename
    p.write_bytes(_make_stream(cmd, f"./{embedded_name}"))
    return p


class TestParseAtSubvol(unittest.TestCase):
    def test_plain_marker(self):
        self.assertEqual(
            bs._parse_at_subvol("At subvol root.202610151200"),
            "root.202610151200",
        )

    def test_quoted_marker(self):
        self.assertEqual(
            bs._parse_at_subvol("At subvol 'snap.202601010000'"),
            "snap.202601010000",
        )

    def test_receiving_variant(self):
        self.assertEqual(
            bs._parse_at_subvol("Receiving subvol foo.202601010000"),
            "foo.202601010000",
        )

    def test_snapshot_marker(self):
        # Incremental (parent-bearing) sends print 'At snapshot', not 'At subvol'
        self.assertEqual(
            bs._parse_at_subvol("At snapshot lama7Maildir.202610210336"),
            "lama7Maildir.202610210336",
        )

    def test_snapshot_quoted_marker(self):
        self.assertEqual(
            bs._parse_at_subvol("At snapshot 'snap.202601010000'"),
            "snap.202601010000",
        )

    def test_no_marker(self):
        self.assertIsNone(bs._parse_at_subvol("some unrelated output"))
        self.assertIsNone(bs._parse_at_subvol(""))
        self.assertIsNone(bs._parse_at_subvol(None))


class TestArchiveNameFromReceived(unittest.TestCase):
    def test_timestamp_stripped(self):
        self.assertEqual(bs.archive_name_from_received("corinne.202610150423"), "corinne")

    def test_bare_name_kept(self):
        self.assertEqual(bs.archive_name_from_received("corinne"), "corinne")


class TestReceiveLocal(unittest.TestCase):
    def setUp(self):
        self._orig = bs._timed_run

    def tearDown(self):
        bs._timed_run = self._orig

    def _fake(self, rc=0, stdout="", stderr=""):
        bs._timed_run = lambda *a, **k: subprocess.CompletedProcess([], rc, stdout, stderr)

    def _run(self, cfg=CFG):
        err = io.StringIO()
        with redirect_stderr(err):
            name = bs._receive_local(STREAM, Path("/backup"), cfg)
        return name, err.getvalue()

    def test_name_parsed_from_stdout(self):
        self._fake(stdout=f"At subvol {REAL_NAME}\n")
        name, err = self._run()
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_name_parsed_from_stderr(self):
        self._fake(stderr=f"At subvol {REAL_NAME}")
        name, err = self._run()
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_fallback_warns_and_uses_stem(self):
        self._fake(stdout="receive finished with no marker")
        name, err = self._run()
        self.assertEqual(name, REAL_NAME)  # stem == real name here by convention
        self.assertIn("Warning", err)
        self.assertIn("could not determine", err.lower())

    def test_dry_run_preview_no_warning(self):
        bs._timed_run = lambda *a, **k: None
        name, err = self._run({"verbose": 0, "dry_run": True})
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_already_exists_returns_none(self):
        self._fake(rc=1, stderr="ERROR: subvolume already exists")
        name, _ = self._run()
        self.assertIsNone(name)

    def test_btrfs_file_exists_returns_none(self):
        # What btrfs-progs actually prints on a duplicate receive (never
        # "already exists"), so the skip branch must match this too.
        self._fake(rc=1, stderr="ERROR: creating snapshot "
                               "_scratch_recv/_scratch_snap -> _snap1 failed: File exists")
        name, err = self._run()
        self.assertIsNone(name)
        self.assertIn("already exists", err.lower())

    def test_incremental_snapshot_marker_is_understood(self):
        self._fake(stdout=f"At snapshot {REAL_NAME}\n")
        name, err = self._run()
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_real_error_exits(self):
        self._fake(rc=1, stderr="ERROR: short read")
        with self.assertRaises(SystemExit):
            bs._receive_local(STREAM, Path("/backup"), CFG)


class TestStreamHeaderIsSourceOfTruth(unittest.TestCase):
    """The stream header holds the exact name receive creates; the filename stem
    only mirrors it by convention (renamed export_file, --stage-file)."""

    def _fake_run(self, rc=0, stdout="", stderr=""):
        self._orig = bs._timed_run
        bs._timed_run = lambda *a, **k: subprocess.CompletedProcess([], rc, stdout, stderr)
        self.addCleanup(setattr, bs, "_timed_run", self._orig)

    def _run(self, stream, cfg=CFG):
        err = io.StringIO()
        with redirect_stderr(err):
            name = bs._receive_local(stream, Path("/backup"), cfg)
        return name, err.getvalue()

    def test_header_wins_over_renamed_stream(self):
        stream = _stream_file("lama7Maildir.202610210336", "lama7Maildir.btrfs")
        self.addCleanup(shutil.rmtree, stream.parent)
        self._fake_run(stdout="At snapshot lama7Maildir.202610210336")
        name, err = self._run(stream)
        self.assertEqual(name, "lama7Maildir.202610210336")
        self.assertEqual(err, "")

    def test_header_used_when_receive_prints_nothing(self):
        stream = _stream_file("corinne.202610150423", "corinne.202610150423.btrfs")
        self.addCleanup(shutil.rmtree, stream.parent)
        self._fake_run(stdout="")
        name, err = self._run(stream)
        self.assertEqual(name, "corinne.202610150423")
        self.assertEqual(err, "")

    def test_header_and_output_mismatch_warns(self):
        stream = _stream_file("corinne.202610150423", "corinne.202610150423.btrfs")
        self.addCleanup(shutil.rmtree, stream.parent)
        self._fake_run(stdout="At subvol somethingelse.202610150423")
        name, err = self._run(stream)
        self.assertEqual(name, "corinne.202610150423")
        self.assertIn("mismatch", err.lower())

    def test_unreadable_header_falls_back_to_output(self):
        self._fake_run(stdout=f"At snapshot {REAL_NAME}")
        name, err = self._run(STREAM)
        self.assertEqual(name, REAL_NAME)
        self.assertEqual(err, "")

    def test_full_send_stream_header(self):
        stream = _stream_file("root.202610150423", "root.202610150423.btrfs",
                              cmd=bs.BTRFS_SEND_C_SUBVOL)
        self.addCleanup(shutil.rmtree, stream.parent)
        self._fake_run(stderr=f"At subvol root.202610150423")
        name, err = self._run(stream)
        self.assertEqual(name, "root.202610150423")
        self.assertEqual(err, "")


class TestReceiveOnRemote(unittest.TestCase):
    """_receive_on_remote is the shared SCP/rsync tail — same name logic."""

    def _fake_run(self, rc=0, stdout="", stderr=""):
        self._orig = bs.run
        bs.run = lambda *a, **k: subprocess.CompletedProcess([], rc, stdout, stderr)
        self.addCleanup(setattr, bs, "run", self._orig)

    def _run(self, stream, cfg=CFG):
        err = io.StringIO()
        with redirect_stderr(err):
            name = bs._receive_on_remote(
                ["ssh", "gerry@thorin", "sudo", "-n", "btrfs", "receive",
                 "/run/media/gerry/backup", "-f", "/tmp/x.btrfs"],
                "gerry@thorin", "/run/media/gerry/backup", "/tmp/x.btrfs",
                stream, cfg, True, datetime.now(), "SCP")
        return name, err.getvalue()

    def test_incremental_snapshot_no_warning(self):
        stream = _stream_file("lama7Maildir.202610210336",
                              "lama7Maildir.202610210336.btrfs")
        self.addCleanup(shutil.rmtree, stream.parent)
        self._fake_run(stdout="At snapshot lama7Maildir.202610210336")
        name, err = self._run(stream)
        self.assertEqual(name, "lama7Maildir.202610210336")
        self.assertEqual(err, "")

    def test_file_exists_returns_none(self):
        self._fake_run(rc=1, stdout="At snapshot lama7Maildir.202610210336\n",
                       stderr="ERROR: creating snapshot lama7Maildir.202610210336 "
                              "failed: File exists")
        name, err = self._run(STREAM)
        self.assertIsNone(name)
        self.assertIn("already exists", err.lower())


class TestReceiveStreamMismatch(unittest.TestCase):
    """A received name from a different archive must warn, not silently pass."""

    def setUp(self):
        self._orig_local = bs._receive_local

    def tearDown(self):
        bs._receive_local = self._orig_local

    def test_mismatch_warns(self):
        bs._receive_local = lambda *a, **k: "otherarchive.202610150423"
        err = io.StringIO()
        with redirect_stderr(err):
            received = bs.receive_stream(STREAM, Path("/backup"), CFG)
        self.assertEqual(received, "otherarchive.202610150423")
        self.assertIn("does not match archive", err.getvalue())

    def test_match_no_warning(self):
        bs._receive_local = lambda *a, **k: REAL_NAME
        err = io.StringIO()
        with redirect_stderr(err):
            received = bs.receive_stream(STREAM, Path("/backup"), CFG)
        self.assertEqual(received, REAL_NAME)
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

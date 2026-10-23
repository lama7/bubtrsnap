#!/usr/bin/env python3
"""Tests for classifying a disk-full receive and renaming its partial subvolume.

TODO 35, commit 2: when `btrfs receive` dies because the destination pool is
full it leaves the destination subvolume behind, half-written, with
`Received UUID: -`.  Verified on a loopback btrfs with btrfs-progs 7.1: the
subvolume is created even with ~128 KiB free, then the file write fails with
`ERROR: writing to huge.bin failed: No space left on device`.

Renaming to `.bubtrsnap-diskfull-{archive}-{ts}`
  * frees the `{archive}.{ts}` name so the next attempt is not blocked by
    "File exists",
  * hides the orphan from every name-based scan (`_ARCHIVE_TS_RE` requires
    `{archive}.{12 digits}`, so a dot-prefixed, hyphenated name cannot match),
  * gives a later run something to detect and refuse to send into.

Rename, not delete: `mv` succeeded on a 100 % full pool in 3-7 ms while
`btrfs subvolume delete` took 38-41 ms and left ~23 s of background extent
cleaning; btrfs-progs 7.1 has no `subvolume rename`, so a plain `mv` inside the
same parent directory is the operation.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
if not _path.is_file():
    raise FileNotFoundError(f"Cannot find bubtrsnap.py at {_path}")
bs = SourceFileLoader("bubtrsnap", str(_path)).load_module()

# The exact line btrfs-progs 7.1 prints when the pool fills up mid-receive.
ENOSPC = ("At subvol big.202602010000\n"
          "ERROR: writing to huge.bin failed: No space left on device")
TRUNCATED = "receive failed: ERROR: short read from stream"
EXISTS = "ERROR: creating subvolume backup/big.202602010000 failed: File exists"
PERM = "ERROR: open failed: Permission denied"
SENTINEL = ".bubtrsnap-diskfull-big-202602010000"
SENTINEL_PATH = f"/backup/{SENTINEL}"


def _result(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class TestIsEnospc(unittest.TestCase):
    """The classification must be narrow: a false positive would block every
    future run for a transient failure."""

    def test_matches_the_real_btrfs_wording(self):
        self.assertTrue(bs._is_enospc(ENOSPC))

    def test_matches_other_space_wordings(self):
        self.assertTrue(bs._is_enospc("ERROR: No space left on device"))
        self.assertTrue(bs._is_enospc("receive failed: ENOSPC"))
        self.assertTrue(bs._is_enospc("NO SPACE LEFT ON DEVICE"))

    def test_rejects_stream_truncation(self):
        self.assertFalse(bs._is_enospc(TRUNCATED))

    def test_rejects_duplicate_and_permission_errors(self):
        self.assertFalse(bs._is_enospc(EXISTS))
        self.assertFalse(bs._is_enospc(PERM))

    def test_rejects_empty_and_none(self):
        self.assertFalse(bs._is_enospc(""))
        self.assertFalse(bs._is_enospc(None))
        self.assertFalse(bs._is_enospc(0))


class TestDiskfullName(unittest.TestCase):
    def test_name_shape(self):
        self.assertEqual(bs._diskfull_name("big.202602010000"), SENTINEL)

    def test_target_without_timestamp(self):
        self.assertEqual(bs._diskfull_name("big"), ".bubtrsnap-diskfull-big")

    def test_sentinel_does_not_match_the_archive_pattern(self):
        self.assertIsNone(bs._ARCHIVE_TS_RE.match(SENTINEL))
        self.assertIsNone(bs._ARCHIVE_TS_RE.match(".bubtrsnap-diskfull-big"))

    def test_orphan_is_invisible_to_name_based_scans(self):
        """A real directory walk must not see the sentinel as an archive."""
        with __import__("tempfile").TemporaryDirectory() as td:
            d = Path(td)
            (d / "big.202602010000").mkdir()
            (d / SENTINEL).mkdir()
            found = [ts for ts, _ in bs.iter_archive_items(d, "big")]
            self.assertEqual(found, ["202602010000"])
            found = [ts for ts, _ in bs.iter_archive_items(d, "big-diskfull")]
            self.assertEqual(found, [])


def _cfg(**over) -> dict:
    cfg = {"verbose": 1, "dry_run": False, "local_sudo": False, "remote_sudo": False}
    cfg.update(over)
    return cfg


class Recorder:
    """Patches bs.run so every external command is recorded; the receive call
    fails with the given stderr, everything else succeeds."""

    def __init__(self, fail_on="receive", stderr=ENOSPC):
        self.calls = []
        self.fail_on = fail_on
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append((cmd, kwargs))
        if kwargs.get("dry_run") and not kwargs.get("read_only"):
            return None  # mirrors run(): a dry-run mutates nothing
        if any(tok in self.fail_on for tok in cmd):
            return _result(1, "", self.stderr)
        return _result(0)

    def renames(self):
        return [c for c, _ in self.calls if "mv" in c]


class TestReceiveLocalRename(unittest.TestCase):
    """`btrfs receive -f <stream> <backup_dir>` failing on a full pool."""

    def _receive(self, stderr, rec, cfg=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(bs, "run", side_effect=rec), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                got = bs._receive_local(Path("big.202602010000.btrfs"),
                                       Path("/backup"), cfg or _cfg())
                return got, err.getvalue()
            except SystemExit:
                return None, err.getvalue()

    def test_disk_full_receive_is_renamed_then_aborts(self):
        rec = Recorder()
        got, err = self._receive(ENOSPC, rec)
        self.assertIsNone(got, "a failed receive must not report a subvolume")
        self.assertEqual(rec.renames(),
                         [["mv", "/backup/big.202602010000", SENTINEL_PATH]])
        self.assertIn("btrfs subvolume delete", err)
        self.assertIn(SENTINEL_PATH, err)

    def test_local_sudo_is_used_for_the_rename(self):
        rec = Recorder()
        self._receive(ENOSPC, rec, _cfg(local_sudo=True))
        self.assertEqual(rec.renames()[0][:2], ["sudo", "-n"])

    def test_other_receive_failure_is_not_renamed(self):
        for stderr in (TRUNCATED, EXISTS, PERM):
            rec = Recorder(stderr=stderr)
            got, err = self._receive(stderr, rec)
            if stderr == EXISTS:
                self.assertIsNone(got)  # duplicate: skip, no abort, no rename
            else:
                self.assertIsNone(got)
            self.assertEqual(rec.renames(), [], f"{stderr!r} is not disk-full")

    def test_dry_run_executes_no_rename(self):
        rec = Recorder()
        got, _ = self._receive(ENOSPC, rec, _cfg(dry_run=True))
        self.assertEqual(got, "big.202602010000")
        self.assertEqual(rec.renames(), [])


class TestReceiveOnRemoteRename(unittest.TestCase):
    """The SCP/rsync route: the stream is copied to the remote, then received
    there; a full remote pool leaves the partial on the remote."""

    def _receive(self, stderr, rec, remote_sudo=False):
        cfg = _cfg(remote_sudo=remote_sudo)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(bs, "run", side_effect=rec), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                got = bs._receive_on_remote(
                    ["ssh", "user@host", "btrfs", "receive", "/backup"],
                    "user@host", "/backup", "/tmp/stream.bin",
                    Path("big.202602010000.btrfs"), cfg, remote_sudo,
                    bs.datetime.now(), "SCP")
                return got, err.getvalue()
            except SystemExit:
                return None, err.getvalue()

    def test_disk_full_remote_receive_is_renamed_on_the_remote(self):
        rec = Recorder()
        got, err = self._receive(ENOSPC, rec)
        self.assertIsNone(got)
        self.assertEqual(rec.renames(),
                         [["ssh", "user@host", "mv",
                           "/backup/big.202602010000", SENTINEL_PATH]])
        self.assertIn("btrfs subvolume delete", err)

    def test_remote_sudo_is_used_for_the_rename(self):
        rec = Recorder()
        self._receive(ENOSPC, rec, remote_sudo=True)
        self.assertEqual(rec.renames()[0][:4],
                         ["ssh", "user@host", "sudo", "-n"])

    def test_a_full_remote_does_not_block_the_local_leg(self):
        """The item-21 rule: a failed remote aborts the run, but the rename is
        scoped to the remote directory only."""
        rec = Recorder()
        self._receive(ENOSPC, rec)
        for cmd in rec.renames():
            self.assertIn("/backup/", cmd[-1])


class TestSendBackupRename(unittest.TestCase):
    """The main piped path: `btrfs send | btrfs receive` (local) and
    `btrfs send | ssh ... receive` (remote)."""

    def _send(self, rec, backup_dir=None, remote=None, remote_dir=None,
              remote_sudo=False):
        cfg = _cfg(remote_sudo=remote_sudo)
        archive_cfg = {"remote_host": remote, "remote_path": remote_dir,
                       "remote_sudo": remote_sudo}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(bs, "run", side_effect=rec), \
             mock.patch.object(bs, "piped_run", return_value=(1, "receive failed: " + ENOSPC)), \
             mock.patch.object(bs, "find_parents", return_value=[]), \
             mock.patch.object(bs, "find_parents_ssh", return_value=[]), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                bs.send_backup(Path("/pool/snapshots/big.202602010000"),
                               Path("/pool/snapshots"), backup_dir, cfg,
                               "big", archive_cfg)
                return None
            except RuntimeError as e:
                return str(e)

    def test_local_piped_enospc_renames_the_partial(self):
        rec = Recorder()
        msg = self._send(rec, backup_dir=Path("/backup"))
        self.assertIn("No space left on device", msg)
        self.assertEqual(rec.renames(),
                         [["mv", "/backup/big.202602010000", SENTINEL_PATH]])

    def test_remote_piped_enospc_renames_on_the_remote(self):
        rec = Recorder()
        msg = self._send(rec, remote="user@host", remote_dir="/backup")
        self.assertIn("No space left on device", msg)
        self.assertEqual(rec.renames(),
                         [["ssh", "user@host", "mv",
                           "/backup/big.202602010000", SENTINEL_PATH]])

    def test_a_local_failure_renames_before_the_remote_leg_runs(self):
        """`send_backup` raises on the local leg, so the remote leg is never
        attempted and has nothing to rename -- the rename is scoped to the
        destination that actually failed."""
        rec = Recorder()
        self._send(rec, backup_dir=Path("/backup"), remote="user@host",
                   remote_dir="/remotebackup")
        renames = rec.renames()
        self.assertEqual(renames, [["mv", "/backup/big.202602010000", SENTINEL_PATH]])


class TestRebuildRename(unittest.TestCase):
    """Rebuild legs: a re-send to a full remote, or a remote->local restore into
    a full snapshot_dir, must rename the partial they left behind."""

    def _run(self, rec, patches, cfg, archive):
        stack = [mock.patch.object(bs, name, **kw) for name, kw in patches]
        stack.append(mock.patch.object(bs, "run", side_effect=rec))
        for p in stack:
            p.__enter__()
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = bs._run_rebuild([archive], cfg, cfg["verbose"])
        finally:
            for p in reversed(stack):
                p.__exit__(None, None, None)
        return rc, out.getvalue(), err.getvalue()

    def test_remote_resend_partial_is_renamed_on_the_remote(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202602010000"
            (snap_dir / f"big.{ts}").mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=None,
                       remote_host="user@host", remote_path="/backup")
            archive = {"name": "big", "subvolume": str(snap_dir / "big"),
                       "remote_host": "user@host", "remote_path": "/backup",
                       "remote_sudo": False}
            rec = Recorder()
            rc, out, err = self._run(
                rec,
                [("iter_archive_items_ssh", {"return_value": []}),
                 ("find_parents_ssh", {"return_value": []}),
                 ("_remote_disk_usage", {"return_value": (10**12, 10**13)}),
                 ("piped_run", {"return_value": (1, "receive failed: " + ENOSPC)})],
                cfg, archive)
            self.assertEqual(rc, 1)
            self.assertEqual(rec.renames(),
                             [["ssh", "user@host", "mv",
                               f"/backup/big.{ts}", f"/backup/{SENTINEL}"]])
            self.assertIn("btrfs subvolume delete", err)

    def test_from_remote_partial_is_renamed_locally(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202602010000"
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=None,
                       remote_host="user@host", remote_path="/backup")
            archive = {"name": "big", "subvolume": str(snap_dir / "big"),
                       "remote_host": "user@host", "remote_path": "/backup",
                       "remote_sudo": False}
            rec = Recorder()
            rc, out, err = self._run(
                rec,
                [("iter_archive_items", {"return_value": iter([])}),
                 ("iter_archive_items_ssh",
                  {"return_value": iter([(ts, f"/backup/big.{ts}")])}),
                 ("_remote_size_of", {"return_value": 1024}),
                 ("_space_check", {"return_value": True}),
                 ("piped_run", {"return_value": (1, "receive failed: " + ENOSPC)})],
                cfg, archive)
            self.assertEqual(rc, 1)
            self.assertEqual(rec.renames(),
                             [["mv", str(snap_dir / f"big.{ts}"),
                               str(snap_dir / SENTINEL)]])

    def test_a_non_enospc_rebuild_failure_leaves_the_partial_alone(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td)
            ts = "202602010000"
            (snap_dir / f"big.{ts}").mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=None,
                       remote_host="user@host", remote_path="/backup")
            archive = {"name": "big", "subvolume": str(snap_dir / "big"),
                       "remote_host": "user@host", "remote_path": "/backup",
                       "remote_sudo": False}
            rec = Recorder(stderr=TRUNCATED)
            rc, out, err = self._run(
                rec,
                [("iter_archive_items_ssh", {"return_value": []}),
                 ("find_parents_ssh", {"return_value": []}),
                 ("_remote_disk_usage", {"return_value": (10**12, 10**13)}),
                 ("piped_run", {"return_value": (1, TRUNCATED)})],
                cfg, archive)
            self.assertEqual(rc, 1)
            self.assertEqual(rec.renames(), [])
            self.assertNotIn("Disk full", err)

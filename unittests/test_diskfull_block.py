#!/usr/bin/env python3
"""Tests for detecting a disk-full sentinel and refusing to send into that pool.

TODO 35, commit 3: the `.bubtrsnap-diskfull-{archive}-{ts}` name written by
`_mark_diskfull` (see unittests/test_diskfull_rename.py) is not just a note for
the admin -- every later run has to see it and refuse to send into the pool that
holds it.  An incremental send's size is unknowable in advance (item 21), so
"the destination already carries a partial" is the only reliable signal that the
pool is broken.

Rules pinned here:
  * detection is per destination and cheap: a local `iterdir`, or the SSH
    `btrfs subvolume list` a run already performs;
  * a blocked leg is skipped, not fatal: the other destination is still written
    and keep-policy pruning still runs;
  * the run still exits non-zero, so cron sees it at -v 0;
  * dry-run cannot create a sentinel but detects a pre-existing one and reports
    the block exactly as a live run would.
"""

from __future__ import annotations

import argparse
import io
import os
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

SENTINEL = ".bubtrsnap-diskfull-big-202602010000"
ENOSPC = ("At subvol big.202602010000\n"
          "ERROR: writing to huge.bin failed: No space left on device")


def _cfg(**over) -> dict:
    cfg = {"verbose": 1, "dry_run": False, "local_sudo": False, "remote_sudo": False}
    cfg.update(over)
    return cfg


def _listing(dest: str, names: list[str]) -> str:
    """A realistic `btrfs subvolume list <dest>` stdout for the given names."""
    return "\n".join(f"ID {i} gen {i} top level 5 path {dest}/{name}"
                     for i, name in enumerate(names, start=256))


class Runner:
    """Records every command; `btrfs subvolume list` returns the given listing,
    receive/send succeed unless told otherwise."""

    def __init__(self, listing=None, fail_on="receive", stderr=ENOSPC):
        self.calls = []
        self.listing = listing or ""
        self.fail_on = fail_on
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if kwargs.get("dry_run") and not kwargs.get("read_only"):
            return None
        if "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self.listing, "")
        if any(tok in self.fail_on for tok in cmd):
            return subprocess.CompletedProcess(cmd, 1, "", self.stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def sends(self):
        return [c for c in self.calls if "send" in c]

    def receives(self):
        return [c for c in self.calls if "receive" in c]


class Base(unittest.TestCase):
    def setUp(self):
        # The sentinel cache lives on cfg, so every fresh _cfg() starts clean.
        pass

    def capture(self, fn):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                result = fn()
            except SystemExit:
                result = "SystemExit"
            except Exception as exc:
                result = exc
        return result, err.getvalue()


class TestSentinelDetection(Base):
    def test_local_scan_finds_only_sentinels(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "big.202602010000").mkdir()
            (d / SENTINEL).mkdir()
            (d / ".bubtrsnap-partial-big").mkdir()
            (d / "notasubvol.btrfs").write_text("x")
            self.assertEqual(bs._diskfull_sentinels(d, _cfg()), [SENTINEL])

    def test_local_scan_of_empty_and_missing_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(bs._diskfull_sentinels(Path(td), _cfg()), [])
            self.assertEqual(bs._diskfull_sentinels(Path(td) / "nope", _cfg()), [])

    def test_local_scan_survives_an_unreadable_directory(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "rootonly"
            d.mkdir()
            os.chmod(d, 0)
            try:
                self.assertEqual(bs._diskfull_sentinels(d, _cfg()), [])
            finally:
                os.chmod(d, 0o755)

    def test_remote_scan_parses_the_path_column(self):
        rec = Runner(listing=_listing("/backup", ["big.202601010000", SENTINEL]))
        with mock.patch.object(bs, "run", side_effect=rec):
            self.assertEqual(bs._diskfull_sentinels("/backup", _cfg(),
                                                   remote="user@host"), [SENTINEL])

    def test_scan_is_cached_per_destination(self):
        cfg = _cfg()
        rec = Runner(listing=_listing("/backup", [SENTINEL]))
        with mock.patch.object(bs, "run", side_effect=rec):
            first = bs._diskfull_blocked("/backup", cfg, remote="user@host")
            again = bs._diskfull_blocked("/backup", cfg, remote="user@host")
        self.assertEqual(first, [SENTINEL])
        self.assertEqual(again, [SENTINEL])
        self.assertEqual(len([c for c in rec.calls if "list" in c]), 1)

    def test_existing_listings_populate_the_cache_for_free(self):
        """The scans a run already does must feed the cache, so detecting the
        sentinel costs no extra command."""
        cfg = _cfg()
        rec = Runner(listing=_listing("/backup", ["big.202601010000", SENTINEL]))
        with mock.patch.object(bs, "run", side_effect=rec):
            list(bs.iter_archive_items_ssh("user@host", "/backup", "big", cfg))
            found = bs._diskfull_sentinels("/backup", cfg, remote="user@host")
        self.assertEqual(found, [SENTINEL])
        self.assertEqual(len([c for c in rec.calls if "list" in c]), 1)

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "big.202602010000").mkdir()
            (d / SENTINEL).mkdir()
            list(bs.iter_archive_items(d, "big"))
            self.assertEqual(bs._diskfull_sentinels(d, cfg), [SENTINEL])

    def test_a_marked_partial_is_visible_to_the_next_leg_in_the_same_run(self):
        cfg = _cfg()
        with mock.patch.object(bs, "run", side_effect=Runner()):
            bs._mark_diskfull("/backup", "big.202602010000", cfg)
            self.assertTrue(bs._diskfull_blocked("/backup", cfg))
        self.assertEqual(cfg["_diskfull_seen"][("/backup", None)], [SENTINEL])

    def test_a_blocked_destination_is_not_rescanned_by_the_next_leg(self):
        """Two legs into the same pool in one run: the second uses the cache,
        so the block is reported once and costs one listing."""
        cfg = _cfg()
        rec = Runner(listing=_listing("/backup", [SENTINEL]))
        with mock.patch.object(bs, "run", side_effect=rec):
            bs._diskfull_blocked("/backup", cfg, remote="user@host")
            bs._diskfull_blocked("/backup", cfg, remote="user@host")
        self.assertEqual(len([c for c in rec.calls if "list" in c]), 1)

    def test_the_cache_does_not_leak_between_runs(self):
        """cfg carries the cache, so a run started after the pool is cleaned is
        not blocked by what an earlier run saw."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / SENTINEL).mkdir()
            first = _cfg()
            self.assertTrue(bs._diskfull_blocked(d, first))
            (d / SENTINEL).rename(d / "big.202602010000")
            second = _cfg()
            self.assertFalse(bs._diskfull_blocked(d, second))
            self.assertTrue(bs._diskfull_blocked(d, first))

    def test_blocked_reports_the_remedy_on_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / SENTINEL).mkdir()
            _, err = self.capture(lambda: bs._diskfull_blocked(d, _cfg()))
        self.assertIn("Disk full", err)
        self.assertIn(SENTINEL, err)
        self.assertIn("btrfs subvolume delete", err)

    def test_dry_run_detects_and_reports_without_mutating(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / SENTINEL).mkdir()
            rec = Runner()
            with mock.patch.object(bs, "run", side_effect=rec):
                blocked = bs._diskfull_blocked(d, _cfg(dry_run=True))
            self.assertTrue(blocked)
            self.assertEqual([c for c in rec.calls if "mv" in c], [])

    def test_a_clean_destination_reports_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            _, err = self.capture(lambda: bs._diskfull_blocked(Path(td), _cfg()))
        self.assertEqual(err, "")

    def test_the_sentinel_is_not_an_archive_item(self):
        """The name must stay invisible to every archive scan, or keep policy
        would try to prune it as a backup."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / SENTINEL).mkdir()
            self.assertEqual(list(bs.iter_archive_items(d, "big")), [])
            self.assertEqual(bs.parse_ts(SENTINEL), None)


class TestLegsBlocked(Base):
    """A destination carrying a sentinel must not be sent into, but the rest of
    the archive still runs."""

    def _send(self, rec, piped, backup_dir=None, remote=None, remote_dir=None,
              cfg=None):
        cfg = cfg or _cfg()
        archive_cfg = {"remote_host": remote, "remote_path": remote_dir,
                       "remote_sudo": cfg.get("remote_sudo", False)}
        with mock.patch.object(bs, "run", side_effect=rec), \
             mock.patch.object(bs, "piped_run", side_effect=piped), \
             mock.patch.object(bs, "find_parents", return_value=[]), \
             mock.patch.object(bs, "find_parents_ssh", return_value=[]):
            return self.capture(
                lambda: bs.send_backup(Path("/pool/snapshots/big.202602010000"),
                                       Path("/pool/snapshots"), backup_dir, cfg,
                                       "big", archive_cfg))

    def test_local_block_skips_the_local_send(self):
        with tempfile.TemporaryDirectory() as td:
            bak = Path(td)
            (bak / SENTINEL).mkdir()
            piped = []
            _, err = self._send(Runner(), lambda *a, **k: piped.append(a) or (0, ""),
                                backup_dir=bak)
            self.assertEqual(piped, [])
            self.assertIn("Disk full", err)

    def test_remote_block_skips_only_the_remote_leg(self):
        rec = Runner(listing=_listing("/remotebackup", [SENTINEL]))
        legs = []
        _, err = self._send(rec, lambda s, r, **k: legs.append(list(r)) or (0, ""),
                            remote="user@host", remote_dir="/remotebackup")
        self.assertEqual(legs, [])
        self.assertIn("Disk full", err)

    def test_a_full_remote_does_not_stop_a_healthy_local_backup(self):
        rec = Runner(listing=_listing("/remotebackup", [SENTINEL]))
        legs = []
        with tempfile.TemporaryDirectory() as td:
            bak = Path(td)
            _, err = self._send(rec, lambda s, r, **k: legs.append(list(r)) or (0, ""),
                                backup_dir=bak, remote="user@host",
                                remote_dir="/remotebackup")
        self.assertEqual(len(legs), 1)
        self.assertIn(str(bak), legs[0])
        self.assertIn("Disk full", err)

    def test_both_legs_run_when_clean(self):
        rec = Runner()
        legs = []
        with tempfile.TemporaryDirectory() as td:
            bak = Path(td)
            _, err = self._send(rec, lambda s, r, **k: legs.append(list(r)) or (0, ""),
                                backup_dir=bak, remote="user@host",
                                remote_dir="/remotebackup")
        self.assertEqual(len(legs), 2)
        self.assertEqual(err, "")

    def test_receive_local_is_skipped_when_blocked(self):
        rec = Runner()
        with tempfile.TemporaryDirectory() as td:
            bak = Path(td)
            (bak / SENTINEL).mkdir()
            got, err = self.capture(lambda: bs._receive_local(
                Path("big.202602010000.btrfs"), bak, _cfg()))
        self.assertIsNone(got)
        self.assertEqual(rec.receives(), [])
        self.assertIn("Disk full", err)

    def test_receive_on_remote_is_skipped_when_blocked(self):
        rec = Runner(listing=_listing("/backup", [SENTINEL]))
        with mock.patch.object(bs, "run", side_effect=rec):
            got, err = self.capture(
                lambda: bs._receive_on_remote(
                    ["ssh", "user@host", "btrfs", "receive", "/backup"],
                    "user@host", "/backup", "/tmp/stream.bin",
                    Path("big.202602010000.btrfs"), _cfg(), False,
                    bs.datetime.now(), "SCP"))
        self.assertIsNone(got)
        self.assertEqual(rec.receives(), [])
        self.assertIn("Disk full", err)

    def test_receive_stream_still_uses_the_unblocked_destination(self):
        rec = Runner()
        legs = []
        with tempfile.TemporaryDirectory() as td:
            bak = Path(td)
            (bak / SENTINEL).mkdir()
            stream = bak / "big.202602010000.btrfs"
            stream.write_text("")
            with mock.patch.object(bs, "run", side_effect=rec), \
                 mock.patch.object(bs, "_scp_and_receive",
                                   side_effect=lambda *a, **k: legs.append(a)
                                   or "big.202602010000"):
                got, err = self.capture(
                    lambda: bs.receive_stream(stream, bak, _cfg(), "user@host",
                                            "/remotebackup"))
        self.assertEqual(got, "big.202602010000")
        self.assertEqual(len(legs), 1)
        self.assertFalse([c for c in rec.receives() if str(bak) in " ".join(c)])
        self.assertIn("Disk full", err)

    def test_piped_send_to_local_is_skipped_when_blocked(self):
        rec = Runner()
        legs = []
        with tempfile.TemporaryDirectory() as td:
            bak = Path(td)
            (bak / SENTINEL).mkdir()
            with mock.patch.object(bs, "run", side_effect=rec), \
                 mock.patch.object(bs, "piped_run",
                                   side_effect=lambda *a, **k: legs.append(a) or (0, "")):
                _, err = self.capture(lambda: bs._piped_send_to_local(
                    Path("/pool/snapshots/big.202602010000"),
                    Path("/pool/snapshots"), bak, _cfg(), "big"))
        self.assertEqual(legs, [])
        self.assertIn("Disk full", err)

    def test_pruning_still_runs_for_a_blocked_archive(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snaps"
            bak_dir = Path(td) / "backup"
            snap_dir.mkdir()
            bak_dir.mkdir()
            (bak_dir / SENTINEL).mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=str(bak_dir))
            archive = {"name": "big", "subvolume": str(snap_dir / "big"),
                       "backup_dir": str(bak_dir), "keep": {}}
            pruned = []
            with mock.patch.object(bs, "run", side_effect=Runner()), \
                 mock.patch.object(bs, "create_snapshot",
                                   return_value=snap_dir / "big.202602010000"), \
                 mock.patch.object(bs, "find_parents", return_value=[]), \
                 mock.patch.object(bs, "piped_run", return_value=(0, "")), \
                 mock.patch.object(bs, "_apply_all_keep_policies",
                                   side_effect=lambda *a, **k: pruned.append(a)):
                _, err = self.capture(lambda: bs.process_archive(archive, cfg))
            self.assertTrue(pruned, "keep policy must still run when the send is blocked")
            self.assertIn("Disk full", err)
            self.assertTrue(cfg.get("_diskfull_blocks"))


class TestRunBackupExitCode(Base):
    def test_a_blocked_destination_makes_the_run_fail(self):
        import argparse

        def args():
            return argparse.Namespace(snaps_only=False, export_file=None,
                                      import_file=None, stage_file=None,
                                      export_dir=None, import_dir=None,
                                      stage_dir=None, remote_host=None,
                                      remote_path=None, verbose=0, debug=False,
                                      dry_run=False, config=None)

        with tempfile.TemporaryDirectory() as td:
            bak = Path(td)
            (bak / SENTINEL).mkdir()
            cfg = {"verbose": 0, "dry_run": False, "snapshot_dir": str(bak)}
            with mock.patch.object(bs, "load_and_resolve_archives",
                                   lambda a, c: (cfg, [{"name": "big",
                                                      "subvolume": str(bak / "big"),
                                                      "backup_dir": str(bak)}])), \
                 mock.patch.object(bs, "process_archive",
                                   side_effect=lambda a, c: bs._diskfull_blocked(
                                       a["backup_dir"], c)):
                rc, err = self.capture(lambda: bs._run_backup(args()))
        self.assertEqual(rc, 1)
        self.assertIn("Disk full", err)
        self.assertIn("disk-full", err)


class TestRebuildBlocked(Base):
    """Rebuild legs into a pool that holds a sentinel are refused, and the
    rebuild exits non-zero so cron sees it."""

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

    def test_backup_leg_into_a_full_pool_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snaps"
            bak_dir = Path(td) / "backup"
            snap_dir.mkdir()
            bak_dir.mkdir()
            (snap_dir / "big.202602010000").mkdir()
            (bak_dir / SENTINEL).mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=str(bak_dir))
            archive = {"name": "big", "subvolume": str(snap_dir / "big")}
            rec = Runner()
            rc, out, err = self._run(rec, [("piped_run", {"return_value": (0, "")})],
                                     cfg, archive)
            self.assertEqual(rc, 1)
            self.assertIn("Disk full", err)
            self.assertFalse(rec.sends())
            self.assertIn("failed", err)

    def test_remote_leg_is_refused_without_touching_local_legs(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snaps"
            bak_dir = Path(td) / "backup"
            snap_dir.mkdir()
            bak_dir.mkdir()
            ts = "202602010000"
            (snap_dir / f"big.{ts}").mkdir()
            (bak_dir / f"big.{ts}").mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=str(bak_dir),
                       remote_host="user@host", remote_path="/remotebackup")
            archive = {"name": "big", "subvolume": str(snap_dir / "big"),
                       "remote_host": "user@host", "remote_path": "/remotebackup",
                       "remote_sudo": False}
            rec = Runner(listing=_listing("/remotebackup", [SENTINEL]))
            rc, out, err = self._run(
                rec,
                [("iter_archive_items_ssh", {"return_value": []}),
                 ("find_parents_ssh", {"return_value": []}),
                 ("_remote_disk_usage", {"return_value": (10**12, 10**13)}),
                 ("piped_run", {"return_value": (0, "")})],
                cfg, archive)
            self.assertEqual(rc, 1)
            self.assertIn("Disk full", err)

    def test_from_remote_leg_into_a_full_snapshot_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snaps"
            snap_dir.mkdir()
            ts = "202602010000"
            (snap_dir / SENTINEL).mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=None,
                       remote_host="user@host", remote_path="/backup")
            archive = {"name": "big", "subvolume": str(snap_dir / "big"),
                       "remote_host": "user@host", "remote_path": "/backup",
                       "remote_sudo": False}
            rec = Runner()
            rc, out, err = self._run(
                rec,
                [("iter_archive_items_ssh",
                  {"return_value": iter([(ts, f"/backup/big.{ts}")])}),
                 ("_remote_size_of", {"return_value": 1024}),
                 ("_space_check", {"return_value": True}),
                 ("piped_run", {"return_value": (0, "")})],
                cfg, archive)
            self.assertEqual(rc, 1)
            self.assertIn("Disk full", err)
            self.assertFalse(rec.sends())

    def test_metadata_only_snapshot_restore_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snaps"
            bak_dir = Path(td) / "backup"
            snap_dir.mkdir()
            bak_dir.mkdir()
            ts = "202602010000"
            (bak_dir / f"big.{ts}").mkdir()
            (snap_dir / SENTINEL).mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=str(bak_dir))
            archive = {"name": "big", "subvolume": str(snap_dir / "big")}
            rec = Runner()
            rc, out, err = self._run(rec, [], cfg, archive)
            self.assertEqual(rc, 0)
            self.assertTrue(any("snapshot" in c for c in rec.calls))
            self.assertNotIn("Disk full", err)

    def test_dry_run_reports_a_preexisting_block(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snaps"
            bak_dir = Path(td) / "backup"
            snap_dir.mkdir()
            bak_dir.mkdir()
            (snap_dir / "big.202602010000").mkdir()
            (bak_dir / SENTINEL).mkdir()
            cfg = _cfg(snapshot_dir=str(snap_dir), backup_dir=str(bak_dir),
                       dry_run=True)
            archive = {"name": "big", "subvolume": str(snap_dir / "big")}
            rec = Runner()
            rc, out, err = self._run(
                rec, [("piped_run", {"return_value": (0, "")})], cfg, archive)
            self.assertEqual(rc, 1)
            self.assertIn("Disk full", err)
            self.assertEqual([c for c in rec.calls if "mv" in c], [])

#!/usr/bin/env python3
"""Regression guard for TODO item 21 and the abort-vs-warn policy around it.

Policy: a send is hard-stopped only when we *know* it cannot fit.  That is only
true for a full send (no usable parents), whose stream really is the whole
subvolume.  Everything else is a warning and an admin decision.

`_size_of()` sums the logical size of the *whole* snapshot, but an incremental
send only writes the delta, which btrfs cannot report in advance (there is no
--dry-run, no size report in btrfs-progs 6.14 or 7.1, and `btrfs qgroup` does
not give the delta: 'excl' is essentially zero for snapshots because the new
extents are also referenced by the live subvolume).  So a fit check based on
the whole snapshot aborts on space that will never be used -- measured on real
archives it over-estimated by 251x (lama7Maildir: 4.46 GiB snapshot vs 18.1
MiB stream) and 1035x (lama7: 650.7 GiB vs 644 MiB).

The guard now splits:
  * full send (no usable parents) -> hard fit check, abort on won't-fit
  * incremental send (parents found) -> no fit check, only a warning when the
    destination has less than LOW_SPACE_FRACTION of its capacity free

The parents discovered for the check are handed to send_backup so the listing
is not repeated.  The same rule is now applied to the rebuild's re-send to the
SSH remote: when the remote has received parents that re-send is incremental,
so its size is only an upper bound and it can never be hard-skipped -- it gets
the remote floor warning instead.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

GB = 1024 ** 3
MB = 1024 ** 2


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()


class _Usage:
    """Shaped like the shutil.disk_usage namedtuple."""

    def __init__(self, total: int, free: int):
        self.total = total
        self.used = total - free
        self.free = free


class TestLowSpaceWarning(unittest.TestCase):
    """The floor warning that replaces the fit check for incremental sends."""

    def _run(self, total: int, free: int) -> tuple[bool, str]:
        out = io.StringIO()
        cfg = {"verbose": 1}
        with patch.object(bs.shutil, "disk_usage", lambda p: _Usage(total, free)), \
                redirect_stdout(out):
            ok = bs._low_space_warning(Path("/backup"), "backup", cfg)
        return ok, out.getvalue()

    def test_warns_below_the_floor(self):
        ok, out = self._run(100 * GB, 20 * GB)
        self.assertFalse(ok, "20% free is below the 25% floor")
        self.assertIn("[space]", out)
        self.assertIn("low space", out)
        self.assertIn("20%", out)

    def test_silent_above_the_floor(self):
        ok, out = self._run(100 * GB, 30 * GB)
        self.assertTrue(ok, "30% free has headroom")
        self.assertEqual(out.strip(), "", f"no warning expected, got: {out}")

    def test_exactly_at_the_floor_is_not_low(self):
        ok, out = self._run(100 * GB, 25 * GB)
        self.assertTrue(ok, "exactly 25% free is not 'below' the floor")
        self.assertEqual(out.strip(), "", f"no warning expected, got: {out}")

    def test_never_aborts(self):
        """The warning must not raise: an incremental send still runs."""
        ok, out = self._run(100 * GB, 1 * GB)
        self.assertFalse(ok)
        self.assertIn("1.0 GiB", out)

    def test_unmeasurable_space_is_not_a_warning(self):
        out = io.StringIO()
        with patch.object(bs.shutil, "disk_usage", side_effect=OSError), redirect_stdout(out):
            ok = bs._low_space_warning(Path("/backup"), "backup", {"verbose": 1})
        self.assertTrue(ok, "unknown free space must not warn or block")

    def test_zero_free_warns(self):
        ok, out = self._run(100 * GB, 0)
        self.assertFalse(ok)
        self.assertIn("no free space", out)


class TestProcessArchiveSpaceCheck(unittest.TestCase):
    """process_archive must fit-check only full sends."""

    def _run(self, parents, space_ok=True, low_ok=True, size=200 * GB):
        archive = {"name": "testarchive", "subvolume": "/fake/subvol",
                   "backup_dir": "/fake/backup", "keep": {}}
        cfg = {"verbose": 0, "dry_run": False, "snapshot_dir": "/fake/snapshots",
               "backup_dir": "/fake/backup", "remote_sudo": False}
        snap = Path("/fake/snapshots/testarchive.202601010000")
        with patch.object(bs, "chk_btrfs_subvolume"), \
                patch.object(bs, "_validate_destinations"), \
                patch.object(bs, "create_snapshot", return_value=snap), \
                patch.object(bs, "run_hook"), \
                patch.object(bs, "find_parents", return_value=parents) as m_find, \
                patch.object(bs, "_size_of", return_value=size) as m_size, \
                patch.object(bs, "_space_check", return_value=space_ok) as m_space, \
                patch.object(bs, "_low_space_warning", return_value=low_ok) as m_low, \
                patch.object(bs, "send_backup", return_value=snap.name) as m_sb, \
                patch.object(bs, "send_backup_tofile"), \
                patch.object(bs, "_apply_all_keep_policies"):
            exit_code = None
            try:
                bs.process_archive(archive, cfg)
            except SystemExit as e:
                exit_code = e.code
        return exit_code, m_find, m_size, m_space, m_low, m_sb

    def test_incremental_send_is_not_fit_checked(self):
        """Parents exist -> the delta is unknown, so a 'won't fit' verdict must
        not abort the archive."""
        parents = [Path("/fake/snapshots/testarchive.202512310000")]
        exit_code, m_find, m_size, m_space, m_low, m_sb = self._run(parents, space_ok=False)
        self.assertIsNone(exit_code, f"incremental send must not abort (exit {exit_code})")
        self.assertEqual(m_space.call_count, 0,
                         "_space_check is a full-send guard; it must not run "
                         "for an incremental send")
        self.assertEqual(m_low.call_count, 1, "the low-space floor warning must run")
        self.assertEqual(m_sb.call_count, 1, "the backup must still be attempted")

    def test_incremental_send_warns_instead_of_aborting(self):
        parents = [Path("/fake/snapshots/testarchive.202512310000")]
        exit_code, _f, _s, _sc, _lw, m_sb = self._run(parents, space_ok=False, low_ok=False)
        self.assertIsNone(exit_code, "a low-space warning never aborts an archive")
        self.assertEqual(m_sb.call_count, 1)

    def test_full_send_is_fit_checked(self):
        """No parents -> a full send -> the whole-snapshot estimate is correct
        and a 'won't fit' verdict still aborts."""
        exit_code, m_find, m_size, m_space, m_low, m_sb = self._run([], space_ok=False)
        self.assertEqual(exit_code, 1, "a full send that won't fit must abort")
        self.assertEqual(m_space.call_count, 1)
        self.assertEqual(m_size.call_count, 1, "the estimate is only computed for full sends")
        self.assertEqual(m_low.call_count, 0, "the floor warning is for incremental sends")
        self.assertEqual(m_sb.call_count, 0, "the send must not run after aborting")

    def test_full_send_that_fits_proceeds(self):
        exit_code, _f, _s, m_space, _lw, m_sb = self._run([], space_ok=True)
        self.assertIsNone(exit_code)
        self.assertEqual(m_space.call_count, 1)
        self.assertEqual(m_sb.call_count, 1)

    def test_parents_are_discovered_once(self):
        parents = [Path("/fake/snapshots/testarchive.202512310000")]
        _code, m_find, _s, _sc, _lw, m_sb = self._run(parents)
        self.assertEqual(m_find.call_count, 1, "find_parents must not run twice")
        self.assertEqual(m_sb.call_args.kwargs.get("local_parents"), parents,
                         "the parents found for the check must be reused by send_backup")


class TestSendBackupUsesPassedParents(unittest.TestCase):
    """send_backup accepts the parents already discovered by the guard."""

    def _cfg(self):
        return {"verbose": 0, "dry_run": True, "local_sudo": True, "remote_sudo": False}

    def test_passed_parents_are_used_and_not_recomputed(self):
        parent = Path("/fake/snapshots/testarchive.202512310000")
        snap = Path("/fake/snapshots/testarchive.202601010000")
        with patch.object(bs, "find_parents") as m_find, \
                patch.object(bs, "piped_run", return_value=(0, "")) as m_pipe, \
                patch.object(bs, "log"):
            bs.send_backup(snap, Path("/fake/snapshots"), Path("/fake/backup"),
                           self._cfg(), "testarchive", {},
                           local_parents=[parent])
        self.assertEqual(m_find.call_count, 0, "send_backup must not re-list parents")
        send_cmd = m_pipe.call_args[0][0]
        self.assertEqual(send_cmd[send_cmd.index("-p") + 1], str(parent),
                         "the passed parent must be used as the incremental parent")

    def test_missing_parents_argument_still_computes_them(self):
        parent = Path("/fake/snapshots/testarchive.202512310000")
        snap = Path("/fake/snapshots/testarchive.202601010000")
        with patch.object(bs, "find_parents", return_value=[parent]) as m_find, \
                patch.object(bs, "piped_run", return_value=(0, "")) as m_pipe, \
                patch.object(bs, "log"):
            bs.send_backup(snap, Path("/fake/snapshots"), Path("/fake/backup"),
                           self._cfg(), "testarchive", {})
        self.assertEqual(m_find.call_count, 1, "the old call form must still work")
        send_cmd = m_pipe.call_args[0][0]
        self.assertEqual(send_cmd[send_cmd.index("-p") + 1], str(parent))


class TestRebuildSpaceCheck(unittest.TestCase):
    """_run_rebuild restores backups with parents too, so its estimate is an
    upper bound: hard-stop only when the sends would be full sends."""

    def _run(self, parents, space_ok):
        with tempfile.TemporaryDirectory() as td:
            snap_d = Path(td) / "snapshots"
            bak_d = Path(td) / "backup"
            ts = "202601010000"
            snap_d.mkdir()
            bak_d.mkdir()
            # The snapshot whose backup is missing: it must exist on disk, or
            # phase 3 never sees a nonzero `needed`.
            (snap_d / f"root.{ts}").mkdir()
            cfg = {"verbose": 0, "dry_run": True, "local_sudo": True,
                   "snapshot_dir": str(snap_d), "backup_dir": str(bak_d),
                   "remote_host": None, "remote_path": None, "remote_sudo": False}
            archive = {"name": "root", "subvolume": str(snap_d / "root")}
            with patch.object(bs, "iter_archive_items",
                              side_effect=[[(ts, snap_d / f"root.{ts}")], []]), \
                    patch.object(bs, "_size_of", return_value=200 * GB) as m_size, \
                    patch.object(bs, "find_parents", return_value=parents) as m_find, \
                    patch.object(bs, "_disk_free", return_value=10 * GB), \
                    patch.object(bs, "_space_check", return_value=space_ok) as m_space, \
                    patch.object(bs, "_low_space_warning", return_value=False) as m_low, \
                    patch.object(bs, "_piped_send_to_local") as m_send:
                rc = bs._run_rebuild([archive], cfg, verbosity=0)
        return rc, m_find, m_size, m_space, m_low, m_send

    def test_incremental_restore_is_not_hard_stopped(self):
        parents = [Path("/fake/backup/root.202512310000")]
        rc, _f, _s, m_space, m_low, m_send = self._run(parents, space_ok=False)
        self.assertEqual(rc, 0, "an incremental restore must not be skipped on an "
                                "over-estimated size")
        self.assertEqual(m_space.call_count, 0,
                         "_space_check is a full-send guard")
        self.assertEqual(m_low.call_count, 1, "the low-space floor warning must run")
        self.assertEqual(m_send.call_count, 1, "the restore must still be attempted")

    def test_full_restore_still_hard_stops(self):
        rc, _f, _s, m_space, m_low, m_send = self._run([], space_ok=False)
        self.assertEqual(rc, 0, "the archive is skipped, the run still succeeds")
        self.assertEqual(m_space.call_count, 1, "a full restore must be fit-checked")
        self.assertEqual(m_send.call_count, 0, "the restore must be skipped")
        self.assertEqual(m_low.call_count, 0)


class TestRemoteDiskUsage(unittest.TestCase):
    """`_remote_disk_usage` is the probe; `_remote_disk_free` stays a thin
    wrapper so the free-only callers (and their tests) keep working."""

    DF = ("Filesystem      1-blocks  Used   Available  Use% Mounted on\n"
          "/dev/sda1   34359738368 31600000000 2759738368 92% /backup\n")

    def test_returns_free_and_total(self):
        with patch("bubtrsnap.run",
                   return_value=MagicMock(returncode=0, stdout=self.DF, stderr="")) as m_run:
            usage = bs._remote_disk_usage("user@host", "/backup", {"verbose": 0, "dry_run": False})
        self.assertEqual(usage, (2759738368, 34359738368))
        self.assertEqual(m_run.call_count, 1, "one df probe per call")

    def test_free_wrapper_still_returns_free(self):
        with patch("bubtrsnap.run",
                   return_value=MagicMock(returncode=0, stdout=self.DF, stderr="")):
            self.assertEqual(bs._remote_disk_free("user@host", "/backup",
                                                 {"verbose": 0, "dry_run": False}), 2759738368)

    def test_probe_failure_returns_none(self):
        with patch("bubtrsnap.run", return_value=MagicMock(returncode=1, stdout="", stderr="")):
            self.assertIsNone(bs._remote_disk_usage("user@host", "/backup",
                                                   {"verbose": 0, "dry_run": False}))

    def test_unparseable_output_returns_none(self):
        with patch("bubtrsnap.run",
                   return_value=MagicMock(returncode=0,
                                         stdout="Filesystem 1-blocks Used Available Use%\n",
                                         stderr="")):
            self.assertIsNone(bs._remote_disk_usage("user@host", "/backup",
                                                   {"verbose": 0, "dry_run": False}))


class TestLowSpaceWarningRemote(unittest.TestCase):
    """The remote counterpart of _low_space_warning(): warn, never abort."""

    def _run(self, usage) -> tuple[bool, str]:
        out = io.StringIO()
        cfg = {"verbose": 1, "dry_run": False}
        with patch.object(bs, "_remote_disk_usage", return_value=usage), redirect_stdout(out):
            ok = bs._low_space_warning_remote("user@host", "/backup", "remote backup", cfg)
        return ok, out.getvalue()

    def test_warns_below_the_floor(self):
        ok, out = self._run((20 * GB, 100 * GB))
        self.assertFalse(ok, "20% free is below the 25% floor")
        self.assertIn("[space]", out)
        self.assertIn("low space", out)
        self.assertIn("20%", out)
        self.assertIn("user@host", out)

    def test_silent_above_the_floor(self):
        ok, out = self._run((30 * GB, 100 * GB))
        self.assertTrue(ok)
        self.assertEqual(out.strip(), "", f"no warning expected, got: {out}")

    def test_exactly_at_the_floor_is_not_low(self):
        ok, out = self._run((25 * GB, 100 * GB))
        self.assertTrue(ok)
        self.assertEqual(out.strip(), "", f"no warning expected, got: {out}")

    def test_zero_free_warns_but_does_not_abort(self):
        ok, out = self._run((0, 100 * GB))
        self.assertFalse(ok)
        self.assertIn("no free space", out)

    def test_unmeasurable_space_is_not_a_warning(self):
        ok, _out = self._run(None)
        self.assertTrue(ok, "unknown remote free space must not warn or block")


class TestRebuildRemoteGuard(unittest.TestCase):
    """The rebuild re-send to the remote is hard-skipped only when it would be a
    full send.  With received parents on the remote it is incremental, so the
    whole-snapshot estimate is an upper bound and the guard must not skip."""

    def _run(self, ssh_parents, remote_check_ok, ts_list=("202601010000",)):
        with tempfile.TemporaryDirectory() as td:
            snap_d = Path(td) / "snapshots"
            bak_d = Path(td) / "backup"
            snap_d.mkdir()
            bak_d.mkdir()
            snap_items, bak_items = [], []
            for ts in ts_list:
                (snap_d / f"root.{ts}").mkdir()
                (bak_d / f"root.{ts}").mkdir()
                snap_items.append((ts, snap_d / f"root.{ts}"))
                bak_items.append((ts, bak_d / f"root.{ts}"))
            cfg = {"verbose": 0, "dry_run": True, "local_sudo": True,
                   "snapshot_dir": str(snap_d), "backup_dir": str(bak_d),
                   "remote_host": None, "remote_path": None, "remote_sudo": False}
            archive = {"name": "root", "subvolume": str(snap_d / "root"),
                       "remote_host": "user@host", "remote_path": "/remote/backup"}
            with patch.object(bs, "iter_archive_items", side_effect=[snap_items, bak_items]), \
                    patch.object(bs, "iter_archive_items_ssh", return_value=[]), \
                    patch.object(bs, "_size_of", return_value=200 * GB), \
                    patch.object(bs, "find_parents_ssh",
                                 return_value=ssh_parents) as m_ssh, \
                    patch.object(bs, "_remote_disk_usage", return_value=(10 * GB, 100 * GB)), \
                    patch.object(bs, "_space_check_remote",
                                 return_value=remote_check_ok) as m_check, \
                    patch.object(bs, "_low_space_warning_remote",
                                 return_value=False) as m_low, \
                    patch.object(bs, "piped_run", return_value=(0, "")) as m_pipe:
                rc = bs._run_rebuild([archive], cfg, verbosity=0)
        return rc, m_ssh, m_check, m_low, m_pipe

    def test_incremental_re_send_is_never_skipped(self):
        parents = [Path("/remote/backup/root.202512310000")]
        rc, m_ssh, m_check, m_low, m_pipe = self._run(parents, remote_check_ok=False)
        self.assertEqual(rc, 0)
        self.assertEqual(m_check.call_count, 0,
                         "_space_check_remote is a full-send guard; it must not run "
                         "when the remote has received parents")
        self.assertEqual(m_low.call_count, 1, "the remote low-space floor warning must run")
        self.assertEqual(m_pipe.call_count, 1, "the re-send must still be attempted")

    def test_full_re_send_is_still_skipped(self):
        rc, _ssh, m_check, m_low, m_pipe = self._run([], remote_check_ok=False)
        self.assertEqual(rc, 0, "only the remote part is skipped, the run succeeds")
        self.assertEqual(m_check.call_count, 1, "a full re-send must be fit-checked")
        self.assertEqual(m_low.call_count, 0, "the floor warning is for incremental sends")
        self.assertEqual(m_pipe.call_count, 0, "a re-send that won't fit must be skipped")

    def test_ssh_parents_are_listed_once_per_archive(self):
        parents = [Path("/remote/backup/root.202512310000")]
        rc, m_ssh, _check, _low, m_pipe = self._run(
            parents, True, ts_list=("202601010000", "202601020000", "202601030000"))
        self.assertEqual(rc, 0)
        self.assertEqual(m_pipe.call_count, 3, "every timestamp must still be re-sent")
        self.assertEqual(m_ssh.call_count, 1,
                         "find_parents_ssh must run once per archive, not once per item")

    def test_re_send_uses_the_cached_parents(self):
        parents = [Path("/remote/backup/root.202512310000")]
        _rc, _ssh, _check, _low, m_pipe = self._run(parents, True,
                                                   ts_list=("202601010000", "202601020000"))
        send_cmd = m_pipe.call_args_list[0][0][0]
        self.assertEqual(send_cmd[send_cmd.index("-p") + 1], str(parents[0]),
                         "the cached remote parents must be used for the re-send")


class TestProcessArchiveRemoteWarning(unittest.TestCase):
    """A remote destination gets the floor warning too: nothing about the remote
    is ever hard-stopped, so silence is the only feedback an admin would get."""

    def _run(self, remote: bool, low_ok: bool = True):
        archive = {"name": "testarchive", "subvolume": "/fake/subvol",
                   "backup_dir": "/fake/backup", "keep": {}}
        if remote:
            archive["remote_host"] = "user@host"
            archive["remote_path"] = "/remote/backup"
        cfg = {"verbose": 0, "dry_run": False, "snapshot_dir": "/fake/snapshots",
               "backup_dir": "/fake/backup", "remote_sudo": False}
        snap = Path("/fake/snapshots/testarchive.202601010000")
        with patch.object(bs, "chk_btrfs_subvolume"), \
                patch.object(bs, "_validate_destinations"), \
                patch.object(bs, "create_snapshot", return_value=snap), \
                patch.object(bs, "run_hook"), \
                patch.object(bs, "find_parents", return_value=[Path("/fake/snapshots/x")]), \
                patch.object(bs, "_low_space_warning", return_value=True), \
                patch.object(bs, "_low_space_warning_remote",
                             return_value=low_ok) as m_rem, \
                patch.object(bs, "send_backup", return_value=snap.name), \
                patch.object(bs, "send_backup_tofile"), \
                patch.object(bs, "_apply_all_keep_policies"):
            exit_code = None
            try:
                bs.process_archive(archive, cfg)
            except SystemExit as e:
                exit_code = e.code
        return exit_code, m_rem

    def test_remote_destination_is_warned_about(self):
        exit_code, m_rem = self._run(remote=True)
        self.assertIsNone(exit_code)
        self.assertEqual(m_rem.call_count, 1, "the remote destination must get a floor check")

    def test_a_low_remote_does_not_abort_the_archive(self):
        exit_code, _m = self._run(remote=True, low_ok=False)
        self.assertIsNone(exit_code, "a low-space warning never aborts an archive")

    def test_no_remote_no_probe(self):
        exit_code, m_rem = self._run(remote=False)
        self.assertIsNone(exit_code)
        self.assertEqual(m_rem.call_count, 0, "no remote destination, no SSH probe")

    def test_file_transfer_to_the_remote_is_warned_about_too(self):
        """export_file + remote goes through rsync/scp, not the piped branch, so
        the floor warning must be emitted before the send, not inside it."""
        archive = {"name": "testarchive", "subvolume": "/fake/subvol",
                   "export_file": "/fake/stage.btrfs", "keep": {},
                   "remote_host": "user@host", "remote_path": "/remote/backup"}
        cfg = {"verbose": 0, "dry_run": False, "snapshot_dir": "/fake/snapshots",
               "backup_dir": None, "remote_sudo": False}
        snap = Path("/fake/snapshots/testarchive.202601010000")
        with patch.object(bs, "chk_btrfs_subvolume"), \
                patch.object(bs, "_validate_destinations"), \
                patch.object(bs, "create_snapshot", return_value=snap), \
                patch.object(bs, "run_hook"), \
                patch.object(bs, "_check_interrupted_rsync", return_value=False), \
                patch.object(bs, "receive_stream", return_value=None), \
                patch.object(bs, "_low_space_warning_remote") as m_rem, \
                patch.object(bs, "send_backup_tofile"), \
                patch.object(bs, "_apply_all_keep_policies"):
            bs.process_archive(archive, cfg)
        self.assertEqual(m_rem.call_count, 1,
                         "a remote destination is checked however it is written to")


if __name__ == "__main__":
    unittest.main()

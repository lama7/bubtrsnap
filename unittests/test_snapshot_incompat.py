#!/usr/bin/env python3
"""Tests for TODO 33b: an entry in snapshot_dir that bubtrsnap cannot use.

`snapshot_dir` cannot be judged by the Received UUID -- a locally made
`btrfs subvolume snapshot -r` has none either -- so TODO 33's partial-receive
rule deliberately never runs there.  The signal that *does* work is btrfs's
read-only flag, and `btrfs subvolume list -r` ("list readonly subvolumes
(including snapshots)") gives it for a whole directory in one read-only command
(verified on btrfs-progs 6.14 and 7.1).  Everything an archive-named direct
child of snapshot_dir that is not read-only has in common is that btrfs refuses
to use it: `btrfs send` rejects it ("subvolume ... is not read-only"), and so
does `btrfs send -p` when it is the parent.  That covers a receive that died
mid-stream (rebuild's remote -> snapshot_dir leg), a snapshot made without -r,
and a snapshot someone flipped with `btrfs property set ... ro false`.

Rules pinned here:
  * a direct child of snapshot_dir whose name matches archive.YYYYMMDDhhmm and
    which btrfs does not mark read-only is "incompat": unusable, not a backup;
  * a read-only snapshot is never incompat -- the scan must not flag the pool it
    is meant to protect;
  * the scan is one extra `subvolume list -r` per snapshot_dir per run, cached
    like the other destination scans, and never runs for a backup/remote pool
    that is not also snapshot_dir;
  * it is fail-closed: if `list -r` is rejected the fallback is one
    `subvolume show` per candidate, and if that fails too nothing is reported
    and nothing is ever deleted;
  * the report goes to stderr at -v 0, once per destination per run, with a
    copy-pasteable delete hint; it does not change the exit code;
  * keep policy excludes it from slot counting; --prune-partial-subvolumes
    (config prune_partial_subvolumes) additionally deletes it;
  * it is never chosen as an incremental-send parent;
  * `list` labels the snapshot column "incompat", and `rebuild` counts it as
    absent: a leg that would land on it is refused and counted as failed.
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

NEW = "202601020000"      # the incompat entry: newest timestamp in the pool
MID = "202601010000"      # a usable snapshot
OLD = "202512310000"      # an older usable snapshot


def _cfg(**over) -> dict:
    cfg = {
        "verbose": 1,
        "dry_run": False,
        "local_sudo": False,
        "remote_sudo": False,
        "snapshot_dir": "/pool/snapshots",
        "week_startday": "sunday",
    }
    cfg.update(over)
    return cfg


def _ro_listing(names: list[str]) -> str:
    """A realistic `btrfs subvolume list -r <dir>` stdout.

    Direct children print as a bare name (verified on 6.14 and 7.1); a nested
    subvolume carries a slash.  The listing is a filter, not a column: there is
    no flags field anywhere in `subvolume list` output.
    """
    return "\n".join(
        f"ID {i} gen 10 parent {256 if '/' in name else 255} top level 255 "
        f"path {name}"
        for i, name in enumerate(names, start=256)
    )


def _uuid_listing(children: list[tuple[str, str | None, bool]]) -> str:
    """A realistic `btrfs subvolume list -u -R -p <dir>` stdout.

    The read-only flag is not in this output at all (verified on 6.14 and 7.1),
    so the third element of each child is only used by the `list -r` fake.
    """
    return "\n".join(
        f"ID {i} gen 10 parent 255 top level 255 "
        f"received_uuid {received or '-'} uuid uuid-{i} path {name}"
        for i, (name, received, _ro) in enumerate(children, start=256)
    )


class Pool:
    """Fake btrfs pool.

    * children: [(name, received_uuid_or_None, is_readonly), ...]
    * ro_rejected: this btrfs-progs build has no `list -r`
    * flags_rejected: the build rejects -u/-R/-p (TODO 33's fallback path)
    * list_fails: the pool cannot be listed at all (unmounted, permissions)
    * show_fails: `subvolume show` always fails
    """

    def __init__(self, children, ro_rejected=False, flags_rejected=False,
                 list_fails=False, show_fails=False):
        self.children = children
        self.ro_rejected = ro_rejected
        self.flags_rejected = flags_rejected
        self.list_fails = list_fails
        self.show_fails = show_fails
        self.calls: list[list[str]] = []
        self.deletes: list[list[str]] = []
        self.snapshots: list[list[str]] = []

    def _result(self, cmd, rc, out, err, kwargs):
        if rc and kwargs.get("check"):
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def __call__(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if kwargs.get("dry_run") and not kwargs.get("read_only"):
            return None
        if "subvolume list" in joined:
            if self.list_fails:
                return self._result(cmd, 1, "", "cannot open pool", kwargs)
            if "-r" in cmd:
                if self.ro_rejected:
                    return self._result(cmd, 1, "", "invalid option -r", kwargs)
                return self._result(
                    cmd, 0,
                    _ro_listing([n for n, _, ro in self.children if ro]),
                    "", kwargs)
            if self.flags_rejected and "-R" in cmd:
                return self._result(cmd, 1, "", "invalid option -R", kwargs)
            return self._result(cmd, 0, _uuid_listing(self.children), "", kwargs)
        if "subvolume show" in joined:
            if self.show_fails:
                return self._result(cmd, 1, "", "not a subvolume", kwargs)
            name = Path(cmd[-1]).name
            for child, received, ro in self.children:
                if child == name:
                    return self._result(
                        cmd, 0,
                        f"UUID: uuid-{name}\n"
                        f"Received UUID: {received or '-'}\n"
                        f"Flags: {'readonly' if ro else '-'}\n", "", kwargs)
            return self._result(cmd, 0, "Subvolume ID: 255\n", "", kwargs)
        if "subvolume snapshot" in joined:
            self.snapshots.append(cmd)
            return self._result(cmd, 0, "", "", kwargs)
        if "subvolume delete" in joined:
            self.deletes.append(cmd)
            return self._result(cmd, 0, "", "", kwargs)
        return self._result(cmd, 0, "", "", kwargs)

    def listings(self) -> list[list[str]]:
        return [c for c in self.calls if "subvolume list" in " ".join(c)]

    def ro_listings(self) -> list[list[str]]:
        return [c for c in self.listings() if "-r" in c]

    def deleted_names(self) -> list[str]:
        return [Path(c[-1]).name for c in self.deletes]


def _pool_dir(names: list[str]) -> Path:
    """A real directory whose children are the given subvolume names."""
    td = Path(tempfile.mkdtemp())
    for name in names:
        (td / name).mkdir()
    return td


def _capture(fn):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = fn()
    return out.getvalue(), err.getvalue(), result


class TestReadonlyScan(unittest.TestCase):
    """Which snapshot_dir entries btrfs considers usable."""

    def test_readonly_listing_is_the_signal(self):
        pool = Pool([(f"lama7.{MID}", None, True), (f"lama7.{NEW}", None, False)])
        d = _pool_dir([f"lama7.{MID}", f"lama7.{NEW}"])
        cfg = _cfg(snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            ro = bs._readonly_children(d, cfg)
        self.assertEqual(ro, {f"lama7.{MID}"})
        self.assertTrue(any("-r" in c for c in pool.ro_listings()),
                        "the scan must use `subvolume list -r`")

    def test_writable_child_in_snapshot_dir_is_incompat(self):
        pool = Pool([(f"lama7.{MID}", None, True), (f"lama7.{NEW}", None, False)])
        d = _pool_dir([f"lama7.{MID}", f"lama7.{NEW}"])
        cfg = _cfg(snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            inv = bs._destination_inventory(d, cfg)
        self.assertEqual(inv["incompat"], [f"lama7.{NEW}"])
        self.assertEqual(inv["entries"] and len(inv["entries"]), 2,
                         "the orphan is still enumerable so prune --ts can reach it")

    def test_readonly_snapshots_are_not_incompat(self):
        """The whole point: `snapshot -r` has no Received UUID but IS read-only."""
        pool = Pool([(f"lama7.{MID}", None, True), (f"lama7.{OLD}", None, True)])
        d = _pool_dir([f"lama7.{MID}", f"lama7.{OLD}"])
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._incompat_snapshots(d, _cfg(snapshot_dir=str(d))), [])

    def test_nested_children_are_not_judged(self):
        pool = Pool([(f"lama7.{NEW}/inner", None, False), (f"lama7.{MID}", None, True)])
        d = _pool_dir([f"lama7.{MID}"])
        cfg = _cfg(snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            ro = bs._readonly_children(d, cfg)
        self.assertEqual(ro, {f"lama7.{MID}"})

    def test_a_backup_pool_needs_no_readonly_scan(self):
        """snapshot_dir is the only pool the readonly question applies to."""
        pool = Pool([(f"lama7.{NEW}", None, False)])
        bak = _pool_dir([f"lama7.{NEW}"])
        cfg = _cfg(snapshot_dir="/pool/snapshots", backup_dir=str(bak))
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._partial_receives(bak, cfg), [f"lama7.{NEW}"])
            self.assertEqual(bs._incompat_snapshots(bak, cfg), [])
        self.assertEqual(pool.ro_listings(), [])

    def test_scan_runs_once_per_snapshot_dir_per_run(self):
        pool = Pool([(f"lama7.{NEW}", None, False), (f"lama7.{MID}", None, True)])
        d = _pool_dir([f"lama7.{NEW}", f"lama7.{MID}"])
        cfg = _cfg(snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            bs._incompat_snapshots(d, cfg)
            bs._incompat_ts_set(d, "lama7", cfg)
            list(bs.iter_archive_items(d, "lama7", cfg))
            bs._incompat_snapshots(d, cfg)
        self.assertEqual(len(pool.ro_listings()), 1)
        self.assertEqual(len(pool.listings()), 1)

    def test_old_btrfs_progs_falls_back_to_subvolume_show(self):
        """No `list -r` in the build: one `show` per candidate, Flags column."""
        pool = Pool([(f"lama7.{NEW}", None, False), (f"lama7.{MID}", None, True)],
                    ro_rejected=True)
        d = _pool_dir([f"lama7.{NEW}", f"lama7.{MID}"])
        cfg = _cfg(snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._incompat_snapshots(d, cfg), [f"lama7.{NEW}"])
        shows = [c for c in pool.calls if "subvolume show" in " ".join(c)]
        self.assertTrue(shows, "the fallback must inspect each candidate")

    def test_unreadable_pool_reports_nothing(self):
        """Fail closed: no listing, nothing labelled, nothing deleted."""
        pool = Pool([(f"lama7.{NEW}", None, False)], list_fails=True, show_fails=True)
        d = _pool_dir([f"lama7.{NEW}"])
        cfg = _cfg(snapshot_dir=str(d), prune_partial_subvolumes=True)
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._incompat_snapshots(d, cfg), [])
            bs.apply_keep_policy(d, "lama7", {"keep_daily": 1}, cfg)
        self.assertEqual(pool.deletes, [])

    def test_dry_run_still_sees_the_orphan(self):
        """The scan is read-only, so a dry run must report what a live run left."""
        pool = Pool([(f"lama7.{NEW}", None, False)])
        d = _pool_dir([f"lama7.{NEW}"])
        cfg = _cfg(snapshot_dir=str(d), dry_run=True, verbose=0)
        with mock.patch.object(bs, "run", pool):
            _out, err, _ = _capture(lambda: bs._incompat_snapshots(d, cfg))
        self.assertIn(f"lama7.{NEW}", err)
        self.assertEqual(pool.deletes, [])

    def test_a_snapshot_created_this_run_is_not_flagged(self):
        """A snapshot made after the scan is not in the readonly set either way."""
        pool = Pool([(f"lama7.{MID}", None, True)])
        d = _pool_dir([f"lama7.{MID}"])
        cfg = _cfg(snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            bs._destination_inventory(d, cfg)
            bs._note_inventory(cfg, d, entries=[(f"lama7.{NEW}", NEW)])
            self.assertEqual(bs._incompat_snapshots(d, cfg), [])
        self.assertIn(f"lama7.{NEW}", [n for n, _ in cfg["_dest_seen"][(str(d), None)]["entries"]])


class TestReporting(unittest.TestCase):
    """The report an unusable snapshot produces: visible at -v 0, once per pool."""

    def _report(self, pool, d, cfg=None):
        cfg = cfg or _cfg(verbose=0, snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            _out, err, _ = _capture(lambda: bs._incompat_snapshots(d, cfg))
        return err

    def test_report_names_the_entry_and_how_to_delete_it(self):
        pool = Pool([(f"lama7.{NEW}", None, False)])
        d = _pool_dir([f"lama7.{NEW}"])
        err = self._report(pool, d)
        self.assertIn("incompat", err.lower())
        self.assertIn(f"lama7.{NEW}", err)
        self.assertIn("read-only", err)
        self.assertIn("btrfs subvolume delete", err)
        self.assertIn(f"{d}/lama7.{NEW}", err)
        self.assertIn("--prune-partial-subvolumes", err)

    def test_report_mentions_the_prune_alternative(self):
        pool = Pool([(f"lama7.{NEW}", None, False)])
        err = self._report(pool, _pool_dir([f"lama7.{NEW}"]))
        self.assertIn("prune", err)
        self.assertIn(f"--ts {NEW}", err)

    def test_report_once_per_destination_per_run(self):
        pool = Pool([(f"lama7.{NEW}", None, False)])
        d = _pool_dir([f"lama7.{NEW}"])
        cfg = _cfg(verbose=0, snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            _o, err1, _ = _capture(lambda: bs._incompat_snapshots(d, cfg))
            _o, err2, _ = _capture(lambda: bs._incompat_snapshots(d, cfg))
            _o, err3, _ = _capture(lambda: list(bs.iter_archive_items(d, "lama7", cfg)))
        self.assertEqual(err1.count("Unusable snapshot"), 1)
        self.assertEqual(err2, "")
        self.assertEqual(err3, "")

    def test_report_uses_sudo_when_the_pool_needs_it(self):
        pool = Pool([(f"lama7.{NEW}", None, False)])
        d = _pool_dir([f"lama7.{NEW}"])
        err = self._report(pool, d, _cfg(verbose=0, snapshot_dir=str(d), local_sudo=True))
        self.assertIn("sudo -n btrfs subvolume delete", err)

    def test_no_report_when_nothing_is_unusable(self):
        pool = Pool([(f"lama7.{MID}", None, True)])
        err = self._report(pool, _pool_dir([f"lama7.{MID}"]))
        self.assertEqual(err, "")

    def test_a_failed_receive_into_snapshot_dir_is_marked_at_once(self):
        """The run that broke the stream knows it: it reports the orphan without
        waiting for a listing made before the orphan existed."""
        pool = Pool([(f"lama7.{MID}", None, True)])
        d = _pool_dir([f"lama7.{MID}"])
        cfg = _cfg(verbose=0, snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            _o, err, _ = _capture(lambda: bs._mark_incompat(cfg, d, f"lama7.{NEW}"))
        self.assertIn(f"lama7.{NEW}", err)
        self.assertIn(f"lama7.{NEW}", cfg["_dest_seen"][(str(d), None)]["incompat"])

        cfg2 = _cfg(verbose=0, snapshot_dir=str(d), dry_run=True)
        _o, err2, _ = _capture(lambda: bs._mark_incompat(cfg2, d, f"lama7.{NEW}"))
        self.assertEqual(err2, "")
        inv2 = cfg2.get("_dest_seen", {}).get((str(d), None), {})
        self.assertNotIn(f"lama7.{NEW}", inv2.get("incompat", []))


class TestKeepPolicy(unittest.TestCase):
    """An unusable snapshot must not hold a keep-policy slot, and is deleted only
    when the user opted in."""

    KEEP = {"keep_hourly": 0, "keep_daily": 1, "keep_weekly": 0,
            "keep_monthly": 0, "keep_yearly": 0}

    def _children(self):
        # the newest entry is the orphan: counted, it takes the daily slot and
        # the last real snapshot is pruned.
        return [(f"lama7.{OLD}", None, True),
                (f"lama7.{MID}", None, True),
                (f"lama7.{NEW}", None, False)]

    def test_orphan_is_not_counted_in_a_slot(self):
        pool = Pool(self._children())
        d = _pool_dir([f"lama7.{OLD}", f"lama7.{MID}", f"lama7.{NEW}"])
        with mock.patch.object(bs, "run", pool):
            bs.apply_keep_policy(d, "lama7", self.KEEP, _cfg(snapshot_dir=str(d)))
        self.assertEqual(pool.deleted_names(), [f"lama7.{OLD}"])


class TestSendParents(unittest.TestCase):
    """A writable entry cannot be an incremental-send parent: btrfs rejects it."""

    def test_writable_snapshot_is_not_a_parent(self):
        snap_pool = Pool([(f"lama7.{MID}", None, True), (f"lama7.{NEW}", None, False)])
        snap = _pool_dir([f"lama7.{MID}", f"lama7.{NEW}"])
        # the backup side received the MID snapshot, whose uuid is uuid-256
        bak_pool = Pool([(f"lama7.{MID}", "uuid-256", True)])
        bak = _pool_dir([f"lama7.{MID}"])
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=str(bak))
        with mock.patch.object(bs, "run", snap_pool):
            snaps = bs._archive_snapshots("lama7", snap, cfg)
        self.assertEqual([Path(p).name for _, p, _ in snaps], [f"lama7.{MID}"])
        with mock.patch.object(bs, "run", bak_pool):
            parents = bs.find_parents("lama7", snap, bak, cfg)
        self.assertEqual([Path(p).name for p in parents], [f"lama7.{MID}"])

    def test_readonly_snapshots_are_still_parents(self):
        snap_pool = Pool([(f"lama7.{MID}", None, True), (f"lama7.{OLD}", None, True)])
        snap = _pool_dir([f"lama7.{MID}", f"lama7.{OLD}"])
        bak_pool = Pool([(f"lama7.{MID}", "uuid-256", True),
                         (f"lama7.{OLD}", "uuid-257", True)])
        bak = _pool_dir([f"lama7.{MID}", f"lama7.{OLD}"])
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=str(bak))
        with mock.patch.object(bs, "run", snap_pool):
            snaps = bs._archive_snapshots("lama7", snap, cfg)
        self.assertEqual(len(snaps), 2)
        with mock.patch.object(bs, "run", bak_pool):
            parents = bs.find_parents("lama7", snap, bak, cfg)
        self.assertEqual(sorted(Path(p).name for p in parents),
                         sorted([f"lama7.{MID}", f"lama7.{OLD}"]))


class TestListLabels(unittest.TestCase):
    """`list` must say "incompat" rather than claiming the snapshot is usable."""

    def _archives(self, snap: Path) -> list[dict]:
        return [{
            "name": "lama7",
            "backup_dir": None,
            "remote_host": None,
            "remote_path": None,
            "remote_sudo": False,
            "forced_keep": [],
            "week_startday": "sunday",
        }]

    def _table(self, pool, snap, cfg):
        with mock.patch.object(bs, "run", pool):
            out, err, rc = _capture(
                lambda: bs._run_list(self._archives(snap), cfg, 0)
            )
        return out, err, rc

    def test_snapshot_column_labels_an_unusable_entry(self):
        pool = Pool([(f"lama7.{NEW}", None, False), (f"lama7.{MID}", None, True)])
        snap = _pool_dir([f"lama7.{NEW}", f"lama7.{MID}"])
        out, err, rc = self._table(pool, snap, _cfg(snapshot_dir=str(snap), verbose=0))
        self.assertEqual(rc, 0)
        rows = {line.split()[0]: line for line in out.splitlines() if line.strip().startswith(NEW) or line.strip().startswith(MID)}
        self.assertIn("incompat", rows[NEW])
        self.assertNotIn("yes", rows[NEW])
        self.assertIn("yes", rows[MID])

    def test_backup_column_stays_partial_for_a_receive_dest(self):
        """TODO 33's wording is still right where a receive actually happened."""
        pool = Pool([(f"lama7.{NEW}", None, False)])
        bak = _pool_dir([f"lama7.{NEW}"])
        cfg = _cfg(snapshot_dir=None, backup_dir=str(bak), verbose=0)
        out, _err, rc = self._table(pool, None, cfg)
        self.assertEqual(rc, 0)
        self.assertIn("partial", out)
        self.assertNotIn("incompat", out)

    def test_shared_pool_reports_the_orphan_in_both_columns(self):
        """snapshot_dir == backup_dir is a supported configuration: a dead receive
        there is not a snapshot either, and the readonly scan is what catches it."""
        pool = Pool([(f"lama7.{NEW}", None, False), (f"lama7.{MID}", "r1", True)])
        shared = _pool_dir([f"lama7.{NEW}", f"lama7.{MID}"])
        cfg = _cfg(snapshot_dir=str(shared), backup_dir=str(shared), verbose=0)
        out, err, rc = self._table(pool, shared, cfg)
        self.assertEqual(rc, 0)
        rows = [line for line in out.splitlines() if NEW in line]
        self.assertTrue(rows)
        self.assertIn("incompat", rows[0])
        self.assertIn(f"lama7.{NEW}", err)


class MultiPool:
    """One fake run over several pools, keyed by destination path."""

    def __init__(self, pools: dict[str, list[tuple[str, str | None, bool]]],
                 ro_rejected=False, list_fails=False, show_fails=False):
        self.pools = {str(d): Pool(children, ro_rejected=ro_rejected,
                                    list_fails=list_fails, show_fails=show_fails)
                      for d, children in pools.items()}
        self.calls: list[list[str]] = []
        self.deletes: list[list[str]] = []
        self.snapshots: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)
        # the destination is the last argument that names a pool this run knows
        # about; `subvolume show` on a subvolume inside a pool is answered by the
        # pool that contains it.
        dest = next((a for a in reversed(cmd) if a in self.pools), None)
        if dest is None:
            for arg in reversed(cmd):
                parent = str(Path(arg).parent)
                if parent in self.pools:
                    dest = parent
                    break
        pool = self.pools.get(dest) or Pool([])
        result = pool(cmd, **kwargs)
        if "subvolume delete" in joined:
            self.deletes.append(cmd)
        if "subvolume snapshot" in joined:
            self.snapshots.append(cmd)
        return result

    def ro_listings(self) -> list[list[str]]:
        return [c for c in self.calls if "subvolume list" in " ".join(c) and "-r" in c]

    def deleted_names(self) -> list[str]:
        return [Path(c[-1]).name for c in self.deletes]


class TestRebuild(unittest.TestCase):
    """An unusable snapshot is not a snapshot: rebuild must not believe it is one,
    and must refuse to write over it."""

    ARCHIVE = [{"name": "lama7", "backup_dir": None, "remote_host": None,
                "remote_path": None, "remote_sudo": False, "forced_keep": [],
                "week_startday": "sunday"}]

    def _rebuild(self, pool, cfg, remote=None, remote_dir=None):
        archives = [dict(a) for a in self.ARCHIVE]
        archives[0]["backup_dir"] = cfg.get("backup_dir")
        archives[0]["remote_host"] = remote
        archives[0]["remote_path"] = remote_dir
        with mock.patch.object(bs, "run", pool), \
             mock.patch.object(bs, "piped_run") as m_piped:
            out, err, rc = _capture(lambda: bs._run_rebuild(archives, cfg, 1))
        return out, err, rc, m_piped

    def test_restore_into_a_writable_orphan_is_a_failed_leg(self):
        """The backup is there, the snapshot is a half-written receive: rebuild
        plans to restore it, refuses to land on the orphan, and exits 1."""
        snap = _pool_dir([f"lama7.{NEW}"])
        bak = _pool_dir([f"lama7.{NEW}"])
        pool = MultiPool({
            str(snap): [(f"lama7.{NEW}", None, False)],
            str(bak): [(f"lama7.{NEW}", "r1", True)],
        })
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=str(bak), verbose=1)
        out, err, rc, _m = self._rebuild(pool, cfg)
        self.assertEqual(rc, 1, f"rebuild should fail when a leg is blocked by an "
                               f"unusable snapshot (got {rc})")
        self.assertIn("1 rebuild action(s) failed", err)
        self.assertIn("read-only", out.lower())
        self.assertFalse(pool.snapshots,
                        "a leg blocked by an orphan must never reach btrfs snapshot")

    def test_lone_orphan_is_reported_but_not_a_failure(self):
        """Nothing to restore, nothing to delete: the report is the only output."""
        snap = _pool_dir([f"lama7.{NEW}"])
        pool = MultiPool({str(snap): [(f"lama7.{NEW}", None, False)]})
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=None, verbose=0)
        out, err, rc, _m = self._rebuild(pool, cfg)
        self.assertEqual(rc, 0)
        self.assertIn("Unusable snapshot", err)
        self.assertEqual(out, "")

    def test_a_remote_restore_into_an_orphan_is_refused(self):
        """The only copy is on the remote and snapshot_dir holds a writable
        orphan of the same name: the receive cannot land and the leg fails."""
        snap = _pool_dir([f"lama7.{NEW}"])
        remote_dir = "/backup"
        pool = MultiPool({
            str(snap): [(f"lama7.{NEW}", None, False)],
            remote_dir: [(f"lama7.{NEW}", "r1", True)],
        })
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=None, verbose=1)
        out, err, rc, m_piped = self._rebuild(pool, cfg, remote="user@host",
                                             remote_dir=remote_dir)
        self.assertEqual(rc, 1)
        self.assertIn("1 rebuild action(s) failed", err)
        m_piped.assert_not_called()
        self.assertFalse(any("receive" in c for c in pool.calls),
                        "a leg refused because of an orphan must not receive")

    def test_a_died_receive_into_snapshot_dir_is_marked_in_the_same_run(self):
        """The leg that broke the stream records the orphan it left, so the run
        that made it reports it instead of calling it a restored snapshot."""
        snap = _pool_dir([])
        remote_dir = "/backup"
        pool = MultiPool({
            str(snap): [],
            remote_dir: [(f"lama7.{NEW}", "r1", True)],
        })
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=None, verbose=0)
        archives = [dict(a) for a in self.ARCHIVE]
        archives[0]["remote_host"] = "user@host"
        archives[0]["remote_path"] = remote_dir
        with mock.patch.object(bs, "run", pool), \
             mock.patch.object(bs, "piped_run",
                               return_value=(1, "short read from stream")):
            out, err, rc = _capture(lambda: bs._run_rebuild(archives, cfg, 0))
        self.assertEqual(rc, 1)
        self.assertIn("Unusable snapshot", err)
        self.assertIn(f"lama7.{NEW}", err)


class TestNoFalsePositives(unittest.TestCase):
    """The scan must not disturb a healthy pool."""

    def test_healthy_snapshot_pool_is_untouched(self):
        pool = Pool([(f"lama7.{MID}", None, True), (f"lama7.{OLD}", None, True)])
        d = _pool_dir([f"lama7.{MID}", f"lama7.{OLD}"])
        cfg = _cfg(snapshot_dir=str(d), verbose=1, prune_partial_subvolumes=True)
        with mock.patch.object(bs, "run", pool):
            _o, err, _ = _capture(lambda: bs.apply_keep_policy(d, "lama7", {"keep_daily": 1}, cfg))
        self.assertEqual(err, "")
        self.assertEqual(pool.deleted_names(), [f"lama7.{OLD}"])

#!/usr/bin/env python3
"""Tests for detecting subvolumes left by a half-finished `btrfs receive` (TODO 33).

A receive that dies mid-stream -- truncated stream, dropped SSH pipe, a pool that
filled up -- still creates the destination subvolume.  It keeps the normal
`archive.YYYYMMDDhhmm` name, so everything that walks a destination treats it as
a real backup: the keep policy counts it in a slot, `list` says "yes", and
`rebuild` thinks the backup is there and does not restore it.  The only reliable
marker is the missing Received UUID: btrfs writes it only when a stream
completes, and the listing prints `received_uuid -`.

Rules pinned here:
  * a direct child of a receive destination whose name matches
    `archive.YYYYMMDDhhmm` and whose Received UUID is absent is a partial;
  * detection never runs in snapshot_dir: a locally created `snapshot -r` also
    has no Received UUID, so the rule would flag every snapshot;
  * detection is fail-closed: if the listing or the UUID lookup fails, nothing is
    reported and nothing is ever deleted;
  * the partial is reported once per destination per run on stderr (visible at
    -v 0) with a delete hint, but it does not change the exit code;
  * keep policy excludes it from slot counting; --prune-partial-subvolumes
    (config prune_partial_subvolumes) additionally deletes it;
  * `list` labels it "partial", and `rebuild` treats it as absent: a restore leg
    that would land on it is refused and counted as failed.
"""

from __future__ import annotations

import io
import argparse
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

NEW = "202601020000"      # the partial: newest timestamp in the pool
MID = "202601010000"      # a complete backup
OLD = "202512310000"      # an older complete backup
SENTINEL = ".bubtrsnap-diskfull-lama7-202601030000"


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


def _listing(children: list[tuple[str, str | None]]) -> str:
    """A realistic `btrfs subvolume list -u -R -p <dir>` stdout.

    Direct children of the searched directory are printed as a bare name, as
    btrfs-progs 6.14/7.1 do; a nested subvolume carries a slash.
    """
    lines = []
    for i, (name, received) in enumerate(children, start=256):
        lines.append(f"ID {i} gen 10 parent 255 top level 255 "
                     f"received_uuid {received or '-'} uuid uuid-{i} path {name}")
    return "\n".join(lines)


class Pool:
    """Fake btrfs pool for one destination.

    * children: [(name, received_uuid_or_None), ...] as the listing prints them
    * flags_rejected: the build rejects -u/-R/-p, so only the plain listing works
    * show_fails: `subvolume show` always fails (unreadable pool)
    """

    def __init__(self, children, dest="/backup", remote=None, flags_rejected=False,
                 show_fails=False, list_fails=False):
        self.children = children
        self.dest = dest
        self.remote = remote
        self.flags_rejected = flags_rejected
        self.show_fails = show_fails
        self.list_fails = list_fails
        self.calls: list[list[str]] = []
        self.deletes: list[list[str]] = []

    def _result(self, cmd, rc, out, err, kwargs):
        """Behave like subprocess.run: check=True turns a bad rc into an error."""
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
            if self.flags_rejected and "-R" in cmd:
                return self._result(cmd, 1, "", "invalid option -R", kwargs)
            return self._result(cmd, 0, _listing(self.children), "", kwargs)
        if "subvolume show" in joined:
            if self.show_fails:
                return self._result(cmd, 1, "", "not a subvolume", kwargs)
            name = Path(cmd[-1]).name
            for child, received in self.children:
                if child == name:
                    return self._result(
                        cmd, 0,
                        f"UUID: uuid-{name}\nReceived UUID: {received or '-'}\n", "", kwargs)
            # any other path is the destination itself: btrfs reports its ID,
            # which is what the listing's parent column says
            return self._result(cmd, 0, "Subvolume ID: 255\n", "", kwargs)
        if "subvolume delete" in joined:
            self.deletes.append(cmd)
            return self._result(cmd, 0, "", "", kwargs)
        return self._result(cmd, 0, "", "", kwargs)

    def listings(self) -> list[list[str]]:
        return [c for c in self.calls if "subvolume list" in " ".join(c)]


def _pool_dir(names: list[str]) -> Path:
    """A real directory whose children are the given subvolume names."""
    td = Path(tempfile.mkdtemp())
    for name in names:
        (td / name).mkdir()
    return td


class TestClassification(unittest.TestCase):
    """What counts as a partial receive."""

    def test_missing_received_uuid_is_a_partial(self):
        pool = Pool([(f"lama7.{NEW}", None), (f"lama7.{MID}", "r1")])
        d = _pool_dir([f"lama7.{NEW}", f"lama7.{MID}", SENTINEL])
        cfg = _cfg()
        with mock.patch.object(bs, "run", pool):
            inv = bs._destination_inventory(d, cfg)
        self.assertEqual(inv["partials"], [f"lama7.{NEW}"])
        self.assertEqual(inv["sentinels"], [SENTINEL])
        # the detection listing is the only btrfs command the run needs
        self.assertEqual(len(pool.listings()), 1)

    def test_complete_receive_is_not_a_partial(self):
        pool = Pool([(f"lama7.{MID}", "r1"), (f"lama7.{OLD}", "r2")])
        d = _pool_dir([f"lama7.{MID}", f"lama7.{OLD}"])
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._partial_receives(d, _cfg()), [])

    def test_snapshot_dir_is_never_a_partial_destination(self):
        """A local `snapshot -r` has no Received UUID either: flagging snapshots
        would report every snapshot in snapshot_dir as corrupt."""
        pool = Pool([(f"lama7.{MID}", None), (f"lama7.{OLD}", None)])
        d = _pool_dir([f"lama7.{MID}", f"lama7.{OLD}"])
        cfg = _cfg(snapshot_dir=str(d))
        with mock.patch.object(bs, "run", pool):
            inv = bs._destination_inventory(d, cfg)
        self.assertEqual(inv["partials"], [])

    def test_old_btrfs_progs_falls_back_to_subvolume_show(self):
        """Builds that reject -u/-R/-p: classify via per-subvolume `show`."""
        pool = Pool([(f"lama7.{NEW}", None)], flags_rejected=True)
        d = _pool_dir([f"lama7.{NEW}"])
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._partial_receives(d, _cfg()), [f"lama7.{NEW}"])

    def test_unreadable_pool_reports_nothing(self):
        """Fail closed: no listing, no partials, no crash, no prune."""
        pool = Pool([(f"lama7.{NEW}", None)], list_fails=True, show_fails=True)
        d = _pool_dir([f"lama7.{NEW}"])
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._partial_receives(d, _cfg()), [])
            self.assertEqual(pool.deletes, [])

    def test_missing_destination_reports_nothing(self):
        pool = Pool([])
        with mock.patch.object(bs, "run", pool):
            self.assertEqual(bs._partial_receives(Path("/no/such/pool"), _cfg()), [])

    def test_inventory_is_cached_per_run(self):
        pool = Pool([(f"lama7.{NEW}", None)])
        d = _pool_dir([f"lama7.{NEW}"])
        cfg = _cfg()
        with mock.patch.object(bs, "run", pool):
            bs._partial_receives(d, cfg)
            bs._diskfull_sentinels(d, cfg)
            bs._partial_receives(d, cfg)
        self.assertEqual(len(pool.listings()), 1)
        self.assertEqual(cfg["_dest_seen"][(str(d), None)]["partials"], [f"lama7.{NEW}"])

    def test_remote_detection_reuses_the_listing_it_already_runs(self):
        """iter_archive_items_ssh already lists the destination; the partial scan
        must not add a second SSH round trip."""
        pool = Pool([(f"lama7.{NEW}", None), (f"lama7.{MID}", "r1"), (SENTINEL, None)])
        cfg = _cfg()
        with mock.patch.object(bs, "run", pool):
            items = list(bs.iter_archive_items_ssh("user@host", "/backup", "lama7", cfg))
            partials = bs._partial_receives("/backup", cfg, remote="user@host")
            sentinels = bs._diskfull_sentinels("/backup", cfg, remote="user@host")
        self.assertEqual(partials, [f"lama7.{NEW}"])
        self.assertEqual(sentinels, [SENTINEL])
        # the enumeration listing is the detection listing: exactly one SSH call
        self.assertEqual(len(pool.listings()), 1)
        self.assertTrue(any("-R" in c for c in pool.listings()),
                        "the shared listing must carry the received-uuid column")
        # a partial is still enumerable so `prune --ts` can reach it
        self.assertEqual(sorted(t for t, _ in items), sorted([NEW, MID]))

    def test_local_walk_records_partials_for_the_block_check(self):
        pool = Pool([(f"lama7.{NEW}", None), (SENTINEL, None)])
        d = _pool_dir([f"lama7.{NEW}", SENTINEL])
        cfg = _cfg()
        with mock.patch.object(bs, "run", pool):
            list(bs.iter_archive_items(d, "lama7", cfg))
            self.assertTrue(bs._diskfull_blocked(d, cfg))
            self.assertEqual(bs._partial_receives(d, cfg), [f"lama7.{NEW}"])
        self.assertEqual(len(pool.listings()), 1)


def _capture(fn):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = fn()
    return out.getvalue(), err.getvalue(), result


class TestReporting(unittest.TestCase):
    """The report a partial produces: visible at -v 0, once per destination."""

    def _report(self, pool, d, cfg=None, remote=None):
        cfg = cfg or _cfg(verbose=0)
        with mock.patch.object(bs, "run", pool):
            out, err, _ = _capture(
                lambda: bs._partial_receives(d, cfg, remote=remote)
            )
        return err

    def test_report_names_the_partial_and_how_to_delete_it(self):
        pool = Pool([(f"lama7.{NEW}", None)])
        d = _pool_dir([f"lama7.{NEW}"])
        err = self._report(pool, d)
        self.assertIn("partial", err.lower())
        self.assertIn(f"lama7.{NEW}", err)
        self.assertIn("Received UUID", err)
        self.assertIn("btrfs subvolume delete", err)
        self.assertIn("--prune-partial-subvolumes", err)
        # the hint must be copy-pasteable, not generic
        self.assertIn(f"{d}/lama7.{NEW}", err)

    def test_report_mentions_prune_alternative(self):
        pool = Pool([(f"lama7.{NEW}", None)])
        err = self._report(pool, _pool_dir([f"lama7.{NEW}"]))
        self.assertIn("prune", err)
        self.assertIn(f"--ts {NEW}", err)

    def test_report_once_per_destination_per_run(self):
        pool = Pool([(f"lama7.{NEW}", None)])
        d = _pool_dir([f"lama7.{NEW}"])
        cfg = _cfg(verbose=0)
        with mock.patch.object(bs, "run", pool):
            _, err1, _ = _capture(lambda: bs._partial_receives(d, cfg))
            _, err2, _ = _capture(lambda: bs._partial_receives(d, cfg))
            _, err3, _ = _capture(lambda: bs.iter_archive_items(d, "lama7", cfg) and None)
        self.assertEqual(err1.count("Partial receive"), 1)
        self.assertEqual(err2, "")
        self.assertEqual(err3, "")

    def test_remote_report_uses_ssh_delete(self):
        pool = Pool([(f"lama7.{NEW}", None)], remote="user@host")
        cfg = _cfg(verbose=0, remote_sudo=True)
        with mock.patch.object(bs, "run", pool):
            _, err, _ = _capture(
                lambda: bs._partial_receives("/backup", cfg, remote="user@host")
            )
        self.assertIn(f"lama7.{NEW}", err)
        self.assertIn("ssh user@host sudo -n btrfs subvolume delete", err)

    def test_no_report_when_there_is_nothing_to_report(self):
        pool = Pool([(f"lama7.{MID}", "r1")])
        err = self._report(pool, _pool_dir([f"lama7.{MID}"]))
        self.assertEqual(err, "")


class TestKeepPolicy(unittest.TestCase):
    """A partial must not occupy a keep-policy slot, and is only ever deleted
    when the user opts in."""

    KEEP = {"keep_hourly": 0, "keep_daily": 1, "keep_weekly": 0,
            "keep_monthly": 0, "keep_yearly": 0}

    def _children(self):
        # oldest, a complete backup in the middle, and the newest entry is a
        # partial: if the partial were counted, the complete backup would be
        # pruned and the corrupt one kept.
        return [(f"lama7.{OLD}", "r3"), (f"lama7.{MID}", "r1"), (f"lama7.{NEW}", None)]

    def test_partial_is_not_counted_in_a_slot(self):
        pool = Pool(self._children())
        d = _pool_dir([f"lama7.{OLD}", f"lama7.{MID}", f"lama7.{NEW}"])
        cfg = _cfg()
        with mock.patch.object(bs, "run", pool):
            bs.apply_keep_policy(d, "lama7", self.KEEP, cfg)
        deleted = [Path(c[-1]).name for c in pool.deletes]
        self.assertEqual(deleted, [f"lama7.{OLD}"])

    def test_partial_survives_by_default(self):
        pool = Pool(self._children())
        d = _pool_dir([f"lama7.{OLD}", f"lama7.{MID}", f"lama7.{NEW}"])
        with mock.patch.object(bs, "run", pool):
            bs.apply_keep_policy(d, "lama7", self.KEEP, _cfg())
        self.assertNotIn(f"lama7.{NEW}", [Path(c[-1]).name for c in pool.deletes])

    def test_opt_in_prune_deletes_partials(self):
        pool = Pool(self._children())
        d = _pool_dir([f"lama7.{OLD}", f"lama7.{MID}", f"lama7.{NEW}"])
        cfg = _cfg(prune_partial_subvolumes=True)
        with mock.patch.object(bs, "run", pool):
            bs.apply_keep_policy(d, "lama7", self.KEEP, cfg)
        deleted = [Path(c[-1]).name for c in pool.deletes]
        self.assertIn(f"lama7.{NEW}", deleted)
        self.assertEqual(sorted(deleted), [f"lama7.{OLD}", f"lama7.{NEW}"])

    def test_opt_in_prune_does_nothing_in_a_dry_run(self):
        pool = Pool(self._children())
        d = _pool_dir([f"lama7.{OLD}", f"lama7.{MID}", f"lama7.{NEW}"])
        cfg = _cfg(prune_partial_subvolumes=True, dry_run=True)
        with mock.patch.object(bs, "run", pool):
            bs.apply_keep_policy(d, "lama7", self.KEEP, cfg)
        self.assertEqual(pool.deletes, [])

    def test_remote_keep_policy_excludes_partials(self):
        pool = Pool(self._children())
        cfg = _cfg()
        with mock.patch.object(bs, "run", pool):
            bs.apply_keep_policy_ssh("user@host", "/backup", "lama7", self.KEEP, cfg)
        deleted = [c[-1].split("/")[-1] for c in pool.deletes]
        self.assertEqual(deleted, [f"lama7.{OLD}"])
        self.assertEqual(len(pool.listings()), 1)

    def test_remote_opt_in_prune_deletes_partials(self):
        pool = Pool(self._children())
        cfg = _cfg(prune_partial_subvolumes=True)
        with mock.patch.object(bs, "run", pool):
            bs.apply_keep_policy_ssh("user@host", "/backup", "lama7", self.KEEP, cfg)
        deleted = [c[-1].split("/")[-1] for c in pool.deletes]
        self.assertIn(f"lama7.{NEW}", deleted)


class TestListLabels(unittest.TestCase):
    """`list` must say what a partial is instead of claiming the backup is there."""

    def _archives(self, bak: Path) -> list[dict]:
        return [{
            "name": "lama7",
            "backup_dir": str(bak),
            "remote_host": None,
            "remote_path": None,
            "remote_sudo": False,
            "forced_keep": [],
            "week_startday": "sunday",
        }]

    def test_backup_column_labels_a_partial(self):
        pool = Pool([(f"lama7.{NEW}", None), (f"lama7.{MID}", "r1")])
        bak = _pool_dir([f"lama7.{NEW}", f"lama7.{MID}"])
        cfg = _cfg(snapshot_dir=None, verbose=1)
        with mock.patch.object(bs, "run", pool):
            out, _err, rc = _capture(
                lambda: bs._run_list(self._archives(bak), cfg, 0)
            )
        self.assertEqual(rc, 0)
        rows = [line for line in out.splitlines() if NEW in line]
        self.assertTrue(rows, "the partial's timestamp is missing from the table")
        self.assertIn("partial", rows[0])
        self.assertNotIn("yes", rows[0])
        # the complete backup is still a plain "yes"
        complete = [line for line in out.splitlines() if MID in line]
        self.assertIn("yes", complete[0])

    def test_snapshot_column_is_never_labelled_partial(self):
        snap = _pool_dir([f"lama7.{MID}"])
        pool = Pool([(f"lama7.{MID}", None)])
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=None, verbose=0)
        archives = self._archives(Path("/nope"))
        archives[0]["backup_dir"] = None
        with mock.patch.object(bs, "run", pool):
            out, err, rc = _capture(lambda: bs._run_list(archives, cfg, 0))
        self.assertEqual(rc, 0)
        self.assertNotIn("partial", out)
        self.assertEqual(err, "")


class TestRebuild(unittest.TestCase):
    """A partial is not a backup: rebuild must not believe it is one, and must
    refuse to receive over it."""

    def _rebuild(self, pool, snap: Path, bak: Path, dry: bool = False):
        cfg = _cfg(snapshot_dir=str(snap), backup_dir=str(bak),
                   verbose=1, dry_run=dry)
        archives = [{"name": "lama7", "backup_dir": str(bak), "remote_host": None,
                     "remote_path": None, "remote_sudo": False, "forced_keep": [],
                     "week_startday": "sunday"}]
        with mock.patch.object(bs, "run", pool), \
             mock.patch.object(bs, "piped_run") as m_piped:
            out, err, rc = _capture(lambda: bs._run_rebuild(archives, cfg, 1))
        return out, err, rc, m_piped

    def test_restore_into_a_partial_is_a_failed_leg(self):
        """The snapshot is there, the backup is a half-written receive: rebuild
        must try to restore it, refuse to land on the partial, and exit 1."""
        pool = Pool([(f"lama7.{NEW}", None)])
        snap = _pool_dir([f"lama7.{NEW}"])
        bak = _pool_dir([f"lama7.{NEW}"])
        out, err, rc, m_piped = self._rebuild(pool, snap, bak)
        self.assertEqual(rc, 1, f"rebuild should fail when a leg is blocked "
                               f"by a partial (got {rc})")
        self.assertIn("1 rebuild action(s) failed", err)
        self.assertIn("partial", out.lower())
        m_piped.assert_not_called()
        self.assertFalse([c for c in pool.calls if "receive" in c],
                        "a leg blocked by a partial must never reach btrfs receive")

    def test_lone_partial_is_reported_but_not_a_failure(self):
        """A partial nowhere else is reported at -v 0 but does not change the
        exit code: there is nothing to restore and nothing to delete."""
        pool = Pool([(f"lama7.{NEW}", None)])
        snap = _pool_dir([])
        bak = _pool_dir([f"lama7.{NEW}"])
        out, err, rc, _ = self._rebuild(pool, snap, bak)
        self.assertEqual(rc, 0)
        self.assertIn(f"lama7.{NEW}", err)

    def test_dry_run_rebuild_mutates_nothing(self):
        pool = Pool([(f"lama7.{NEW}", None)])
        snap = _pool_dir([f"lama7.{NEW}"])
        bak = _pool_dir([f"lama7.{NEW}"])
        out, err, rc, m_piped = self._rebuild(pool, snap, bak, dry=True)
        self.assertEqual(rc, 1)
        self.assertIn("partial", err.lower())
        m_piped.assert_not_called()
        self.assertEqual(pool.deletes, [])


class TestFlagPlumbing(unittest.TestCase):
    """--prune-partial-subvolumes / prune_partial_subvolumes."""

    CLI_DEFAULTS = dict(
        archives=[],
        config=None, snapshot_dir=None, backup_dir=None, snaps_only=False,
        export_file=None, import_file=None, stage_file=None, export_dir=None,
        import_dir=None, stage_dir=None, local_sudo=False, remote_host=None,
        remote_path=None, remote_sudo=False, rsync=False, rsync_opts=None,
        keep_hourly=None, keep_daily=None, keep_weekly=None, keep_monthly=None,
        keep_yearly=None, week_startday=None, forced_keep=None, verbose=0,
        debug=False, dry_run=False, ts=None, yes=False,
        prune_partial_subvolumes=False,
    )

    def test_flag_is_not_a_value_taker(self):
        """A store_true flag must not swallow the subcommand token (item 18)."""
        self.assertNotIn("--prune-partial-subvolumes", bs._MAINT_VALUE_TAKERS)
        self.assertEqual(
            bs._maint_first_positional(["--prune-partial-subvolumes", "list", "lama7"]),
            1,
        )

    def test_config_option_is_allowed(self):
        self.assertIn("prune_partial_subvolumes", bs.ALLOWED_GLOBAL_OPTIONS)
        self.assertIn("prune_partial_subvolumes", bs.ALLOWED_ARCHIVE_OPTIONS)
        self.assertIn("prune_partial_subvolumes", bs.DEFAULT_CONFIG)

    def test_config_rejects_a_non_bool(self):
        with self.assertRaises(SystemExit):
            bs.validate_config({"prune_partial_subvolumes": "yes"})

    def _resolve(self, cli: bool, archive: bool | None, global_: bool | None):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('snapshot_dir = "/pool/snap"\n')
            if global_ is not None:
                f.write(f"prune_partial_subvolumes = {str(global_).lower()}\n")
            f.write('[lama7]\nsubvolume = "/home"\n')
            if archive is not None:
                f.write(f"prune_partial_subvolumes = {str(archive).lower()}\n")
            path = Path(f.name)
        cli_ns = argparse.Namespace(**self.CLI_DEFAULTS)
        cli_ns.prune_partial_subvolumes = cli
        try:
            # /home is not a btrfs subvolume on every machine; the subvolume
            # check is irrelevant to how the flag resolves.
            with mock.patch.object(bs, "chk_btrfs_subvolume", lambda *a, **k: None):
                _, archives = bs.load_and_resolve_archives(cli_ns, path)
        finally:
            path.unlink()
        return archives[0]["prune_partial_subvolumes"]

    def test_precedence_cli_over_archive_over_global(self):
        self.assertTrue(self._resolve(cli=True, archive=False, global_=False))
        self.assertTrue(self._resolve(cli=False, archive=True, global_=False))
        self.assertFalse(self._resolve(cli=False, archive=False, global_=True))
        self.assertFalse(self._resolve(cli=False, archive=None, global_=None))

    def test_parser_accepts_the_flag(self):
        for build in (bs._build_backup_parser, bs._build_list_parser,
                      bs._build_prune_parser, bs._build_rebuild_parser):
            ns = build().parse_args(["--prune-partial-subvolumes", "lama7"])
            self.assertTrue(getattr(ns, "prune_partial_subvolumes", False),
                            f"{build.__name__} must accept --prune-partial-subvolumes")

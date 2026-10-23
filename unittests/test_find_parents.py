#!/usr/bin/env python3
"""Regression guards for parent discovery (TODO item 22).

`find_parents()` and `find_parents_ssh()` used to spawn one `btrfs subvolume
show` per candidate subvolume to read its UUID / Received UUID. A single
`btrfs subvolume list -u -R -p -o <dir>` prints uuid, received_uuid and parent
ID for every subvolume below the directory in one call, so that loop should
collapse to one listing per directory (plus one `show` on the directory itself
to learn its Subvolume ID, which is what identifies *direct* children).

These tests count subprocess invocations, so a change that quietly reverts to
the per-subvolume loop fails even when the returned parent list is correct.
The fake runner honours `check=` like the real subprocess.run does, which is
what makes the "options not supported" fallback test meaningful.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()

T1, T2, T3, T4 = "202601010000", "202601020000", "202601030000", "202601030100"

# Direct children of the snapshot dir (parent 100) plus a nested entry whose
# basename also matches the archive pattern (parent 202) - the nested one must
# not be treated as a snapshot of the archive.
SNAP_LIST = "\n".join([
    f"ID 200 gen 5 parent 100 top level 100 received_uuid - uuid 1111 path snapshots/lama7.{T1}",
    f"ID 201 gen 6 parent 100 top level 100 received_uuid - uuid 2222 path snapshots/lama7.{T2}",
    f"ID 202 gen 7 parent 100 top level 100 received_uuid - uuid 3333 path snapshots/lama7.{T3}",
    f"ID 203 gen 8 parent 100 top level 100 received_uuid - uuid 4444 path snapshots/other.{T3}",
    f"ID 204 gen 9 parent 202 top level 100 received_uuid - uuid 5555 path snapshots/lama7.{T3}/lama7.{T4}",
])

# Direct children of the backup dir (parent 5): two of them were received.
BACKUP_LIST = "\n".join([
    f"ID 300 gen 10 parent 5 top level 5 received_uuid 1111 uuid a000 path lama7.{T1}",
    f"ID 301 gen 11 parent 5 top level 5 received_uuid 2222 uuid b000 path lama7.{T2}",
    f"ID 302 gen 12 parent 5 top level 5 received_uuid - uuid c000 path lama7.{T3}",
    f"ID 303 gen 13 parent 5 top level 5 received_uuid 3333 uuid d000 path root.{T1}",
])


def _result(rc: int, stdout: str = "", stderr: str = ""):
    if rc != 0:
        raise subprocess.CalledProcessError(rc, "cmd", stdout, stderr)
    return subprocess.CompletedProcess("cmd", rc, stdout, stderr)


class FakeBtrfs:
    """Records every command and answers list/show calls like btrfs-progs would.

    list_rc > 0 simulates a btrfs-progs build that does not understand -u/-R/-p/-o.
    show_id is the "Subvolume ID:" line returned for the directory itself.
    """

    def __init__(self, snap_list=SNAP_LIST, backup_list=BACKUP_LIST,
                 list_rc=0, show_ids=None, uuids=None):
        self.snap_list = snap_list
        self.backup_list = backup_list
        self.list_rc = list_rc
        # Subvolume ID reported by `subvolume show` for each directory itself.
        self.show_ids = show_ids or {"snapshots": 100, "backup": 5}
        self.uuids = uuids or {
            f"lama7.{T1}": "1111",
            f"lama7.{T2}": "2222",
            f"lama7.{T3}": "3333",
        }
        self.received = {
            f"lama7.{T1}": "1111",
            f"lama7.{T2}": "2222",
            f"lama7.{T3}": "-",
        }
        self.calls: list[str] = []

    def run(self, cmd, **kwargs):
        cmd_str = " ".join(cmd)
        self.calls.append(cmd_str)
        if "subvolume list" in cmd_str:
            # Simulate an older btrfs-progs that rejects the new flags but still
            # answers a plain listing.
            if self.list_rc and "-R" in cmd_str:
                return _result(self.list_rc, "", "unknown option -R")
            out = self.snap_list if "snapshots" in cmd_str else self.backup_list
            return _result(0, out)
        if "subvolume show" in cmd_str:
            name = cmd_str.rstrip("/").split("/")[-1]
            if name in self.show_ids:
                return _result(0, f"Name: {name}\nSubvolume ID: {self.show_ids[name]}\n")
            if "snapshots" in cmd_str:
                return _result(0, f"UUID: {self.uuids.get(name, '-')}\nReceived UUID: -\n")
            return _result(0, f"UUID: 9999\nReceived UUID: {self.received.get(name, '-')}\n")
        return _result(0, "")

    def lists(self) -> list[str]:
        return [c for c in self.calls if "subvolume list" in c]

    def shows(self) -> list[str]:
        return [c for c in self.calls if "subvolume show" in c]


class TestParseSubvolumeList(unittest.TestCase):
    def test_parses_uuid_and_received_uuid(self):
        entries = bs._parse_subvolume_list(SNAP_LIST)
        self.assertEqual(entries[f"lama7.{T1}"], ("1111", None))

    def test_received_dash_is_none(self):
        entries = bs._parse_subvolume_list(BACKUP_LIST)
        self.assertEqual(entries[f"lama7.{T3}"][1], None)
        self.assertEqual(entries[f"lama7.{T1}"][1], "1111")

    def test_parent_filter_keeps_only_direct_children(self):
        entries = bs._parse_subvolume_list(SNAP_LIST, parent_id="100")
        self.assertIn(f"lama7.{T3}", entries)
        nested = f"lama7.{T4}"
        self.assertNotIn(nested, entries)

    def test_empty_and_bad_output(self):
        self.assertEqual(bs._parse_subvolume_list(""), {})
        self.assertEqual(bs._parse_subvolume_list("ID 1 gen 2 path"), {})

    def test_names_with_spaces(self):
        line = ("ID 400 gen 1 parent 100 top level 100 received_uuid - "
                "uuid 6666 path snapshots/My Docs.202601010000")
        self.assertEqual(bs._parse_subvolume_list(line),
                         {"My Docs.202601010000": ("6666", None)})

    def test_nested_path_with_spaces_is_filtered_by_parent(self):
        line = ("ID 401 gen 1 parent 200 top level 100 received_uuid - "
                "uuid 7777 path snapshots/My Docs.202601010000/inner sub")
        self.assertEqual(bs._parse_subvolume_list(line, parent_id="100"), {})
        self.assertEqual(bs._parse_subvolume_list(line, parent_id="200"),
                         {"inner sub": ("7777", None)})


class TestFindParentsLocal(unittest.TestCase):
    """find_parents() against a fake btrfs: two listings, no per-subvolume show."""

    def _setup(self, fake):
        root = Path(tempfile.mkdtemp())
        self.root = root
        snap = root / "snapshots"
        backup = root / "backup"
        snap.mkdir()
        backup.mkdir()
        for name in (f"lama7.{T1}", f"lama7.{T2}", f"lama7.{T3}", f"other.{T3}"):
            (snap / name).mkdir()
        for name in (f"lama7.{T1}", f"lama7.{T2}", f"lama7.{T3}"):
            (backup / name).mkdir()
        (snap / f"lama7.{T3}" / f"lama7.{T4}").mkdir()
        cfg = {"local_sudo": False, "verbose": 0, "dry_run": False}
        return snap, backup, cfg, fake

    def test_one_listing_per_directory(self):
        fake = FakeBtrfs()
        snap, backup, cfg, _ = self._setup(fake)
        with patch.object(bs, "run", fake.run):
            bs.find_parents("lama7", snap, backup, cfg)

        # TODO 33b: the read-only flag is only visible through `subvolume list -r`,
        # so the snapshot directory gets one extra read-only listing.  The UUID
        # walk itself is still one call per directory, and the directory's ID is
        # looked up once and reused by both scans.
        uuid_lists = [c for c in fake.lists() if "-r" not in c]
        ro_lists = [c for c in fake.lists() if "-r" in c]
        self.assertEqual(len(uuid_lists), 2, f"expected 2 listings, got {fake.calls}")
        self.assertEqual(len(ro_lists), 1, f"read-only scan ran more than once: {fake.calls}")
        for cmd in uuid_lists:
            for flag in ("-u", "-R", "-p", "-o"):
                self.assertIn(flag, cmd, f"listing missing {flag}: {cmd}")
        # Only the two directory ID lookups, never one show per candidate.
        self.assertEqual(len(fake.shows()), 2, f"per-subvolume shows still happening: {fake.calls}")

    def test_parents_matched_and_sorted_newest_first(self):
        fake = FakeBtrfs()
        snap, backup, cfg, _ = self._setup(fake)
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents("lama7", snap, backup, cfg)
        names = [p.name for p in parents]
        self.assertEqual(names, [f"lama7.{T2}", f"lama7.{T1}"])

    def test_nested_same_name_is_not_a_parent(self):
        fake = FakeBtrfs()
        snap, backup, cfg, _ = self._setup(fake)
        # The nested entry (parent 202) has uuid 5555 which is also 'received'
        # in the backup listing, so an unfiltered map would invent a parent.
        fake.backup_list += f"\nID 304 gen 14 parent 5 top level 5 received_uuid 5555 uuid e000 path lama7.{T3}"
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents("lama7", snap, backup, cfg)
        self.assertEqual([p.name for p in parents], [f"lama7.{T2}", f"lama7.{T1}"])
        self.assertNotIn(f"lama7.{T4}", [p.name for p in parents])

    def test_falls_back_when_listing_options_unsupported(self):
        fake = FakeBtrfs(list_rc=1)
        snap, backup, cfg, _ = self._setup(fake)
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents("lama7", snap, backup, cfg)
        self.assertEqual([p.name for p in parents], [f"lama7.{T2}", f"lama7.{T1}"])
        # Fallback path really does use the per-subvolume show loop.
        uuid_lists = [c for c in fake.lists() if "-r" not in c]
        self.assertEqual(len(uuid_lists), 2)
        self.assertTrue(len(fake.shows()) >= 6, fake.calls)

    def test_unreceived_snapshot_is_not_a_parent(self):
        fake = FakeBtrfs()
        snap, backup, cfg, _ = self._setup(fake)
        fake.backup_list = "\n".join([
            f"ID 300 gen 10 parent 5 top level 5 received_uuid 1111 uuid a000 path lama7.{T1}",
        ])
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents("lama7", snap, backup, cfg)
        self.assertEqual([p.name for p in parents], [f"lama7.{T1}"])

    def test_archive_name_containing_a_space(self):
        """A subvolume basename with a space must still be matched end to end."""
        fake = FakeBtrfs(
            snap_list="\n".join([
                f"ID 200 gen 5 parent 100 top level 100 received_uuid - uuid 1111 path snapshots/My Docs.{T1}",
                f"ID 201 gen 6 parent 100 top level 100 received_uuid - uuid 2222 path snapshots/My Docs.{T2}",
            ]),
            backup_list="\n".join([
                f"ID 300 gen 10 parent 5 top level 5 received_uuid 1111 uuid a000 path My Docs.{T1}",
                f"ID 301 gen 11 parent 5 top level 5 received_uuid 2222 uuid b000 path My Docs.{T2}",
            ]),
        )
        fake.uuids = {f"My Docs.{T1}": "1111", f"My Docs.{T2}": "2222"}
        fake.received = {f"My Docs.{T1}": "1111", f"My Docs.{T2}": "2222"}

        root = Path(tempfile.mkdtemp())
        snap = root / "snapshots"
        backup = root / "backup"
        snap.mkdir()
        backup.mkdir()
        for name in (f"My Docs.{T1}", f"My Docs.{T2}"):
            (snap / name).mkdir()
            (backup / name).mkdir()

        cfg = {"local_sudo": False, "verbose": 0, "dry_run": False}
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents("My Docs", snap, backup, cfg)
        self.assertEqual([p.name for p in parents], [f"My Docs.{T2}", f"My Docs.{T1}"])


class TestFindParentsSSH(unittest.TestCase):
    """find_parents_ssh() resolves the remote received UUIDs with one SSH call."""

    def _setup(self, fake):
        root = Path(tempfile.mkdtemp())
        snap = root / "snapshots"
        snap.mkdir()
        for name in (f"lama7.{T1}", f"lama7.{T2}", f"lama7.{T3}"):
            (snap / name).mkdir()
        cfg = {"local_sudo": False, "verbose": 0, "dry_run": False}
        return snap, cfg, fake

    def test_single_ssh_listing_no_per_subvolume_ssh_shows(self):
        fake = FakeBtrfs()
        snap, cfg, _ = self._setup(fake)
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents_ssh("lama7", snap, "gerry@thorin", "/run/media/gerry/backup", cfg)

        remote_cmds = [c for c in fake.calls if c.startswith("ssh ")]
        remote_lists = [c for c in remote_cmds if "subvolume list" in c]
        remote_shows = [c for c in remote_cmds if "subvolume show" in c]
        self.assertEqual(len(remote_lists), 1, fake.calls)
        self.assertEqual(len(remote_shows), 1, fake.calls)
        self.assertEqual([p.name for p in parents], [f"lama7.{T2}", f"lama7.{T1}"])

    def test_ssh_fallback_uses_per_subvolume_shows(self):
        fake = FakeBtrfs(list_rc=1)
        snap, cfg, _ = self._setup(fake)
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents_ssh("lama7", snap, "gerry@thorin", "/run/media/gerry/backup", cfg)
        self.assertEqual([p.name for p in parents], [f"lama7.{T2}", f"lama7.{T1}"])
        remote_shows = [c for c in fake.calls if c.startswith("ssh ") and "subvolume show" in c]
        self.assertTrue(len(remote_shows) >= 3, fake.calls)

    def test_ssh_ignores_other_archives_received_uuids(self):
        fake = FakeBtrfs()
        snap, cfg, _ = self._setup(fake)
        with patch.object(bs, "run", fake.run):
            parents = bs.find_parents_ssh("lama7", snap, "gerry@thorin", "/run/media/gerry/backup", cfg)
        # root.<T1> is received with uuid 3333 which is lama7.<T3>'s uuid; it
        # must not make lama7.<T3> look like a usable parent.
        self.assertNotIn(f"lama7.{T3}", [p.name for p in parents])

#!/usr/bin/env python3
"""Tests: one failing archive must not abort the whole run."""

from __future__ import annotations

import argparse
import sys
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


def _args():
    return argparse.Namespace(
        snaps_only=False,
        export_file=None, import_file=None, stage_file=None,
        export_dir=None, import_dir=None, stage_dir=None,
        remote_host=None, remote_path=None,
        verbose=0, debug=False, dry_run=False, config=None,
    )


def _run_with(archives, process_fn):
    global_cfg = {
        "verbose": 0,
        "dry_run": False,
        "snapshot_dir": "/tmp/fake-snaps",
    }
    with patch.object(bs, "load_and_resolve_archives",
                      lambda a, c: (global_cfg, archives)), \
         patch.object(bs, "process_archive", process_fn):
        return bs._run_backup(_args())


class TestRunBackupErrorIsolation(unittest.TestCase):
    def test_systemexit_in_one_archive_continues_run(self):
        archives = [
            {"name": "bad", "subvolume": "/a"},
            {"name": "good", "subvolume": "/b"},
        ]
        calls = []

        def fake_process(archive, cfg):
            calls.append(archive["name"])
            if archive["name"] == "bad":
                sys.exit(1)
            return None

        rc = _run_with(archives, fake_process)
        self.assertEqual(rc, 1)
        # The run must continue past the failing archive, not abort.
        self.assertEqual(calls, ["bad", "good"])

    def test_exception_in_one_archive_continues_run(self):
        archives = [
            {"name": "bad", "subvolume": "/a"},
            {"name": "good", "subvolume": "/b"},
        ]
        calls = []

        def fake_process(archive, cfg):
            calls.append(archive["name"])
            if archive["name"] == "bad":
                raise RuntimeError("boom")
            return None

        rc = _run_with(archives, fake_process)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["bad", "good"])

    def test_all_success_returns_zero(self):
        archives = [{"name": "a", "subvolume": "/a"}, {"name": "b", "subvolume": "/b"}]
        rc = _run_with(archives, lambda archive, cfg: None)
        self.assertEqual(rc, 0)

    def test_failure_in_reprocess_returns_nonzero(self):
        archives = [{"name": "a", "subvolume": "/a"}]

        def fake_process(archive, cfg):
            return "RECOVERED" if len(calls) == 1 else sys.exit(1)

        calls = []
        def counting(archive, cfg):
            calls.append(archive["name"])
            return fake_process(archive, cfg)

        rc = _run_with(archives, counting)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["a", "a"])  # processed, then re-processed


if __name__ == "__main__":
    unittest.main()

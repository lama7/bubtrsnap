#!/usr/bin/env python3
"""Regression guards for _validate_subvol_path() (TODO item 30).

A failing `btrfs subvolume show` must be reported as an error and abort, not be
logged as a success and cached as validated. The old code called run() without
check=True, so subprocess never raised and the except branch was unreachable.

The fake subprocess runner below honours the `check` keyword the way the real
subprocess.run() does, which is what makes these tests meaningful: if the caller
does not ask for check=True (or inspect returncode), a non-zero exit is silently
accepted and the test fails.
"""

from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


def _load():
    path = Path(__file__).resolve().parent.parent / "bubtrsnap.py"
    if not path.is_file():
        raise FileNotFoundError(f"Cannot find bubtrsnap.py at {path}")
    return SourceFileLoader("bubtrsnap", str(path)).load_module()


bs = _load()

CFG = {"verbose": 0, "local_sudo": False, "dry_run": False}


def _fake_run(rc: int, stdout: str | None = None, stderr: str | None = None):
    """subprocess.run look-alike that respects check= like the real one."""

    def fake(cmd, **kwargs):
        result = subprocess.CompletedProcess(cmd, rc, stdout, stderr)
        if kwargs.get("check") and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, stdout, stderr)
        return result

    return fake


class TestSubvolumeValidation(unittest.TestCase):
    def _run_local(self, path, cfg=CFG):
        """Return (stderr_text, exit_code) — exit_code is None when validation passed."""
        err = io.StringIO()
        exit_code = None
        with redirect_stderr(err):
            try:
                bs.chk_btrfs_subvolume(Path(path), cfg)
            except SystemExit as exc:
                exit_code = exc.code
        return err.getvalue(), exit_code

    def _run_ssh(self, remote, remote_dir, cfg=CFG, remote_sudo=False):
        err = io.StringIO()
        exit_code = None
        with redirect_stderr(err):
            try:
                bs.chk_btrfs_subvolume_ssh(remote, remote_dir, cfg, remote_sudo)
            except SystemExit as exc:
                exit_code = exc.code
        return err.getvalue(), exit_code

    def test_show_command_is_run_with_check(self):
        """The validation must actually ask subprocess to raise on failure."""
        calls = []

        def recorder(cmd, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(bs, "run", recorder):
            bs.chk_btrfs_subvolume(Path("/pool/snapshots"), CFG)

        self.assertTrue(calls, "validation did not call run()")
        self.assertTrue(
            calls[0].get("check"),
            "run() called without check=True -> CalledProcessError can never fire",
        )

    def test_btrfs_show_failure_aborts_local(self):
        with patch.object(bs.subprocess, "run",
                          _fake_run(1, stderr="ERROR: Not a Btrfs subvolume: Invalid argument")):
            _, exit_code = self._run_local("/nonexistent-path-xyz")
            self.assertEqual(exit_code, 1)

    def test_btrfs_show_failure_message_is_written(self):
        with patch.object(bs.subprocess, "run",
                          _fake_run(1, stderr="ERROR: Not a Btrfs subvolume: Invalid argument")):
            err, exit_code = self._run_local("/nonexistent-path-xyz")
            self.assertEqual(exit_code, 1)
            self.assertIn("not a btrfs subvolume", err)
            self.assertIn("/nonexistent-path-xyz", err)

    def test_ssh_show_failure_aborts(self):
        with patch.object(bs.subprocess, "run",
                          _fake_run(1, stdout="", stderr="ERROR: Not a Btrfs subvolume")):
            _, exit_code = self._run_ssh("gerry@thorin", "/run/media/gerry/backup")
            self.assertEqual(exit_code, 1)

    def test_ssh_failure_reports_the_remote_error(self):
        with patch.object(bs.subprocess, "run",
                          _fake_run(1, stdout="", stderr="ssh: connect to host thorin port 22: Connection refused")):
            err, exit_code = self._run_ssh("gerry@thorin", "/run/media/gerry/backup")
            self.assertEqual(exit_code, 1)
            self.assertIn("SSH error:", err)
            self.assertIn("Connection refused", err)

    def test_failing_path_is_not_cached_as_validated(self):
        cfg = dict(CFG)
        cfg["_validated_paths"] = set()
        with patch.object(bs.subprocess, "run", _fake_run(1, stderr="Not a subvolume")):
            try:
                bs.chk_btrfs_subvolume(Path("/bad/dir"), cfg)
            except SystemExit:
                pass
        self.assertNotIn("local:/bad/dir", cfg["_validated_paths"])

    def test_valid_subvolume_is_cached_and_passes(self):
        cfg = dict(CFG)
        with patch.object(bs.subprocess, "run", _fake_run(0, stdout="snapshots")):
            bs.chk_btrfs_subvolume(Path("/pool/snapshots"), cfg)
        self.assertIn("local:/pool/snapshots", cfg["_validated_paths"])

    def test_timeout_still_aborts(self):
        def fake(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

        with patch.object(bs.subprocess, "run", fake):
            err, exit_code = self._run_ssh("gerry@thorin", "/backup", CFG)
            self.assertEqual(exit_code, 1)
            self.assertIn("timed out", err)


if __name__ == "__main__":
    unittest.main()

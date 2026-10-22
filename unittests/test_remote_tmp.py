#!/usr/bin/env python3
"""Unit tests for the remote temp file used by the SCP and rsync transfers.

Regression guard for TODO item 15: the remote temp path was
`/tmp/bubtrsnap-<stream name>`, fully predictable from the config.  A user on
the remote could pre-create that path as a symlink (scp wrote through it, the
`rm -f` cleanup then removed only the link) or as a directory (the transfer
failed and the run exited), the stream sat in /tmp at the remote's default mode
between transfer and receive, and two archives whose streams shared a basename
collided.  The path now carries a random suffix and the file is chmod'd 600 on
the remote before `btrfs receive` runs.
"""

from __future__ import annotations

import io
import re
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

# /tmp/bubtrsnap-<sanitized stream name>-<16 hex chars>
TEMP_RE = re.compile(r"^/tmp/bubtrsnap-[A-Za-z0-9_.-]+-[0-9a-f]{16}$")

STREAM = Path("/pool/snap/lama7Maildir.202601010000.btrfs")
REMOTE = "user@host"
REMOTE_DIR = "/remote/backup"


def _ok(*args):
    return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")


def _fail(label, stderr):
    return subprocess.CompletedProcess(args=[label], returncode=1, stdout="", stderr=stderr)


def _cmds(mock_run):
    """The command list of every run() call, in call order."""
    return [c.args[0] for c in mock_run.call_args_list]


def _dest_of(cmd):
    """host:/path -> /path for the scp/rsync destination argument."""
    return cmd[-1].split(":", 1)[1]


class TestRemoteTempPath(unittest.TestCase):
    """The remote temp path must not be guessable from the config."""

    def test_helper_exists(self):
        self.assertTrue(hasattr(bs, "_remote_tmp_path"),
                        "no helper builds the remote temp path")

    def test_path_is_not_predictable_from_the_stream_name(self):
        got = bs._remote_tmp_path(STREAM)
        self.assertNotEqual(got, f"/tmp/bubtrsnap-{STREAM.name}",
                            "the remote temp path is still derived only from the stream name")

    def test_the_same_stream_name_never_repeats(self):
        paths = {bs._remote_tmp_path(STREAM) for _ in range(50)}
        self.assertEqual(len(paths), 50, "the remote temp name repeats across runs")

    def test_path_is_shell_and_glob_safe(self):
        for name in ("foo[0-9].btrfs", "foo bar.btrfs", "../evil;rm -rf /.btrfs",
                     "a$b`c'.btrfs", "sub/dir/stream.btrfs"):
            got = bs._remote_tmp_path(Path(name))
            self.assertRegex(got, TEMP_RE, f"unsafe remote temp path for {name!r}: {got}")
            self.assertNotIn("/", got[5:], f"path escaped /tmp for {name!r}")
            self.assertNotIn(" ", got)

    def test_stream_name_is_still_visible_for_debugging(self):
        got = bs._remote_tmp_path(STREAM)
        self.assertIn("lama7Maildir", got, "temp path no longer identifies the archive")


class TestSCPRemoteTemp(unittest.TestCase):
    """SCP flow: one unpredictable path used by transfer, chmod, receive, cleanup."""

    @patch("bubtrsnap.run")
    def test_transfer_chmod_receive_and_cleanup_share_one_path(self, mock_run):
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False}
        mock_run.return_value = None

        bs._scp_and_receive(STREAM, REMOTE, REMOTE_DIR, cfg)

        cmds = _cmds(mock_run)
        transfer = cmds[0]
        self.assertEqual(transfer[0], "scp")
        dest = _dest_of(transfer)
        self.assertRegex(dest, TEMP_RE, f"scp destination is not unpredictable: {dest}")

        chmod = next(c for c in cmds if "chmod" in c)
        self.assertEqual(chmod[-2:], ["600", dest], "chmod 600 does not target the temp file")

        receive = next(c for c in cmds if "receive" in c)
        self.assertEqual(receive[receive.index("-f") + 1], dest,
                         "btrfs receive reads a different file than was transferred")

        cleanup = next(c for c in cmds if "rm" in c)
        self.assertEqual(cleanup[-1], dest, "cleanup removes a different file than was written")

        # chmod must happen after the transfer and before the receive
        self.assertGreater(cmds.index(chmod), cmds.index(transfer))
        self.assertLess(cmds.index(chmod), cmds.index(receive))
        # ... and cleanup is last
        self.assertEqual(cmds.index(cleanup), len(cmds) - 1)

    @patch("bubtrsnap.run")
    def test_chmod_honors_remote_sudo(self, mock_run):
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": True}
        mock_run.return_value = None

        bs._scp_and_receive(STREAM, REMOTE, REMOTE_DIR, cfg, remote_sudo=True)

        cmds = _cmds(mock_run)
        chmod = next(c for c in cmds if "chmod" in c)
        self.assertEqual(chmod[:4], ["ssh", REMOTE, "sudo", "-n"],
                         "the remote chmod ignores remote_sudo")
        self.assertEqual(chmod[4:6], ["chmod", "600"])

    @patch("bubtrsnap.run")
    def test_chmod_failure_warns_but_does_not_abort(self, mock_run):
        cfg = {"dry_run": False, "verbose": 1, "remote_sudo": False}
        mock_run.side_effect = [
            _ok("scp"),
            _fail("ssh", "chmod: Operation not permitted"),
            _ok("ssh", "btrfs", "receive"),
            _ok("ssh", "rm"),
        ]

        buf = io.StringIO()
        with redirect_stderr(buf):
            result = bs._scp_and_receive(STREAM, REMOTE, REMOTE_DIR, cfg)

        self.assertEqual(result, STREAM.stem, "the receive should still have run")
        self.assertIn("temp file", buf.getvalue())
        self.assertIn("not permitted", buf.getvalue())

    @patch("bubtrsnap.run")
    def test_cleanup_removes_the_same_random_path_it_wrote(self, mock_run):
        """The rm must use the per-run path, not a stale predictable one."""
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False}
        mock_run.return_value = None

        bs._scp_and_receive(STREAM, REMOTE, REMOTE_DIR, cfg)

        cmds = _cmds(mock_run)
        cleanup = next(c for c in cmds if "rm" in c)
        self.assertNotEqual(cleanup[-1], f"/tmp/bubtrsnap-{STREAM.name}")


class TestRsyncRemoteTemp(unittest.TestCase):
    """rsync flow: same rules, different transfer command."""

    @patch("bubtrsnap.run")
    def test_transfer_chmod_receive_and_cleanup_share_one_path(self, mock_run):
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": None}
        mock_run.return_value = None

        bs._rsync_and_receive(STREAM, REMOTE, REMOTE_DIR, cfg,
                              rsync_opts=None, archive_name="lama7Maildir")

        cmds = _cmds(mock_run)
        transfer = next(c for c in cmds if c[0] == "rsync")
        dest = _dest_of(transfer)
        self.assertRegex(dest, TEMP_RE, f"rsync destination is not unpredictable: {dest}")

        chmod = next(c for c in cmds if "chmod" in c)
        self.assertEqual(chmod[-2:], ["600", dest])

        receive = next(c for c in cmds if "receive" in c)
        self.assertEqual(receive[receive.index("-f") + 1], dest)

        cleanup = next(c for c in cmds if "rm" in c)
        self.assertEqual(cleanup[-1], dest)

        self.assertLess(cmds.index(chmod), cmds.index(receive))

    @patch("bubtrsnap.run")
    def test_rsync_partial_dir_and_temp_file_are_separate_paths(self, mock_run):
        cfg = {"dry_run": True, "verbose": 2, "remote_sudo": False,
               "local_sudo": False, "rsync": True, "rsync_opts": None}
        mock_run.return_value = None

        bs._rsync_and_receive(STREAM, REMOTE, REMOTE_DIR, cfg,
                              rsync_opts=None, archive_name="lama7Maildir")

        cmds = _cmds(mock_run)
        transfer = next(c for c in cmds if c[0] == "rsync")
        partial = transfer[transfer.index("--partial-dir") + 1]
        dest = _dest_of(transfer)
        self.assertTrue(partial.startswith(".bubtrsnap-partial-"))
        self.assertNotEqual(partial, dest)


if __name__ == "__main__":
    unittest.main()

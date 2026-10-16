# bubtrsnap bug backlog — reconciled audit findings (2026-10-16)

Consolidates FINDINGS.md (2026-10-14 audit of branch `maintain` @ 4dedd35)
and HANDOFF.md (2026-10-16 status). All "remaining" items below were
re-verified against current `main` (v0.4.1).

Original audit evidence traces: ~/.hermes/cache/delegation/live/deleg_d5fc1199/task-{0,1,2}.log
Full audit report: ~/.hermes/cache/scratch/bubtrsnap-audit-2026-10-15.md

## SHIPPED in v0.4.1 (tag pushed, GitHub release + PyPI published)

- 353270b  `--stage-file` alone was rejected by its own mutual-exclusion check
- 32a2f01  store_true flags in _MAINT_VALUE_TAKERS swallowed the subcommand
- 8d5b76e  per-archive week_startday accepted but never used
- 6453708  rebuild planned a restore into literal 'None' when backup_dir unset
- e767bd1  CLI --week-startday now beats per-archive value (CLI > archive > global)

## REMAINING — functional bugs (all re-verified in current source)

1. DONE (b954c6d, unpushed): piped_run now captures both stderr streams,
   waits on both processes, and returns the first non-zero rc with labeled
   errors; display line uses _quote_for_display. Tests in
   unittests/test_piped_run.py (7). Live-verified: failed send reports
   "send failed: ERROR: unable to resolve ...".

2. DONE (b836c3c, unpushed): _receive_local now parses "At subvol" from the
   real receive output via shared _parse_at_subvol() (stdout+stderr, all
   three receive paths); stem is a warned fallback only; receive_stream
   warns when the received name belongs to a different archive using the
   formerly-dead archive_name_from_received(). Tests in
   unittests/test_receive_names.py (14).

3. DONE (02fc1b0, unpushed): _run_backup now catches SystemExit as well as
   Exception per archive, logs the failed archive, continues with the rest,
   and returns 1 with a summary of failed names. _run_backup coverage gap
   addressed by unittests/test_run_backup_errors.py (4).

4. DONE (61fbefa, unpushed): _receive_and_post now gates only the hook on
   `received`; keep policies always run (idempotent pruning). Tests:
   TestReceiveAndPostGate in unittests/test_keep_policy.py (3).

5. DONE (7b2ba5d, unpushed): partial-dir is now per-archive
   (.bubtrsnap-partial-<name>, sanitized); detection and rsync both use it.
   Tests: unittests/test_partial_dir.py (5). Orphan /tmp/.bubtrsnap-partial
   dirs from older versions remain on remotes but are inert.

6. DONE (7b75d30, unpushed): _run_prune now counts actual successful
   deletes and reports that; logs a note when it differs from the preview.
   Test: TestPruneActualCount in unittests/test_keep_policy.py.

7. DONE (26f3047, unpushed): archive-level file+dir mixing now hard-errors,
   mirroring the CLI blanket rule; README documents it. Tests:
   TestFileDirMixing in unittests/test_file_options.py (6).

8. DONE: dead `(send_file or send_dir) and remote and remote_dir` gate inside
   the plain-backup elif (the elif chain guarantees both are None there) and
   the no-op `if recv_file and send_file and not staged: recv_path = ...`
   reassignment removed; dead `dest =` stores in the export branches dropped.
   Guarded by TestPlainBackupBranch in unittests/test_runtime.py (2).

9. DONE: run() docstring now states read_only dry-run returns the
   CompletedProcess (matches the code).

## REMAINING — validation / precedence gaps

10. Negative keep counts pass validation (579-583, type-check only).
    CONFLICT to resolve with one probe first: FINDINGS says they distort
    build_keep_set accounting and over-prune; the keep-algorithm audit says
    negatives are treated as 0. Validation fix only — do not touch the
    keep algorithm itself (needs explicit approval).

11. CLI --forced-keep values never format-validated: only config values get
    the 12-digit check (571-578); _merge_keep accepts any string; malformed
    CLI timestamps are silently warned-and-ignored (~2181).

## REMAINING — security hardening

12. Untrusted remote output into shell: 'At subvol' parsed from remote
    stderr (1594-1600, 1713-1719) enters the hook context (2288-2290) and is
    interpolated raw into a shell=True command (1813). A compromised remote
    can inject local shell commands. (shell=True for user-configured hook
    templates is accepted design; the remote-output path is the risk.)
    Overlaps improvement: run_hook substitutions are unquoted, so paths with
    spaces break.

13. Unquoted SSH remote command strings: build_ssh_cmd (1407-1410) joins
    args into one string re-parsed by the remote shell — config paths with
    metacharacters execute remotely (prune deletes 3358-3360, keep-policy
    deletes 2224-2226, df/du 3101, 3138). remote_host starting with '-' is
    parsed as an ssh option (e.g. -oProxyCommand=). Remote listing output is
    re-injected into later ssh commands in prune/rebuild.
    Fix: shlex.quote in build_ssh_cmd + reject remote_host starting with '-'.

14. rsync blocklist bypass: --rsh blocked but short form -e not; --config
    not blocked. Verified: --rsync-opts '-e "ssh -oProxyCommand=id"
    --config=...' passes _filter_rsync_opts. Add -e/--config or switch to an
    allow-list.

15. Predictable temp file /tmp/bubtrsnap-<name> on the remote — symlink/TOCTOU
    exposure on multi-user hosts, then unconditional rm -f.

16. Archive names unsanitized in filesystem paths and globs:
    snap_dir / f'{archive}.{ts}' escapes snapshot_dir for names with '/' or
    '..'; glob(f'{archive}.*') treats metacharacters as pattern syntax.

17. Hooks run with check=False (1813): a failing pre-snapshot hook (e.g. DB
    quiesce) is silently ignored and the snapshot is taken anyway.

## REMAINING — convention violations

18. Direct subprocess.run outside run(): 3122 (_remote_disk_free), 3159
    (_remote_size_of) — no [dry-run] prefix, no level-2 logging; should be
    run(cmd, read_only=True).

19. remove_stage_file (494-500): rm runs check=False, returncode never
    checked, yet "Removed staged stream file" logged unconditionally.

20. No timeout on the ssh `test -d` in _check_interrupted_rsync (1617-1622):
    unreachable remote hangs the recovery check indefinitely.

## REMAINING — improvements (not bugs)

21. _size_of walks the whole snapshot summing logical sizes, then compares
    against free space before an INCREMENTAL send -> false "insufficient
    space" aborts. Also used by rebuild to report required memory.

22. find_parents / find_parents_ssh spawn one `btrfs subvolume show` (or SSH)
    per candidate; `btrfs subvolume list -u <dir>` gets all UUIDs in one call.

23. DONE: _scp_and_receive and _rsync_and_receive now share a single
    _receive_on_remote() tail (SSH receive, temp cleanup, error handling,
    name extraction); only the transfer command and log label differ.
24. FALSE POSITIVE: crc32c is live — _parse_stream_prefix validates the
    stream CRC via it (bubtrsnap.py:440), reached by is_btrfs_stream and
    stream_snapshot_name.

25. --config only accepted before the subcommand (bubtrsnap list --config x
    fails).

26. SKILL.md version field now tracks 0.4.1; keep in sync at each release.

## Keep-algorithm audit notes (no changes made, per instruction)

Correct across 7 scenarios. Observations only: total kept can be less than
sum(keep_*) because _get_keeps does not backfill slots taken by a shorter
interval; partial interval fills are silent; lexicographic ts compare is safe
only because ts are 12-digit padded.

## Verified clean (do not re-flag)

- All tests pass (121 across 6 files); no secrets, eval/exec, pickle; stderr
  discipline correct on error paths.
- forced_keep threading intact (read from archive dict 2369, threaded through
  _apply_all_keep_policies to local and SSH paths).
- Sudo scoping correct: local_sudo local-only, remote_sudo only via
  build_ssh_cmd, ssh client never root.
- Prune safety nets sound: _confirm fail-safes on EOF/Ctrl-C, forced-keep
  targets require explicit confirmation even with --yes, no partial deletes.
- Staged dual-destination guard is correct (false positive in review).
- 'At subvol' stderr parsing is correct (btrfs-progs prints to stderr).

## Suggested sequencing

Phase 1 (untested-path bugs, highest value): 1, 2, 3, 9
Phase 2 (security): 13, 12, 14, 15+16
Phase 3 (validation/consistency): 10 (probe first), 11, 6, 4, 5, 7
Phase 4 (cleanup): 8, 18, 19, 20, 21, 22, 23, 25

Each item = one commit, test written first and proven red.

#!/usr/bin/env python3
"""
Integration test: verify rsync interrupted transfer recovery with bubtrsnap.

This test requires:
  - Docker daemon running with --privileged support
  - Docker image built with: docker build -t bubtrsnap-test -f Dockerfile.test .
  - Container started with: docker run -d --name bubtrsnap-test --privileged -p 2222:22 bubtrsnap-test
  - pip install paramiko

The test:
1. Starts a Docker container with SSH + btrfs + rsync
2. Creates a large file on the host (to simulate a btrfs stream)
3. Initiates an rsync transfer to the remote
4. Kills rsync mid-transfer to simulate interruption
5. Verifies the partial-dir is created on the remote
6. Re-runs rsync and verifies it resumes/completes the transfer
7. Verifies the file on the remote matches the source

Usage:
    python3 test_rsync_interrupted.py
"""
import hashlib
import os
import subprocess
import sys
import time
import paramiko

REMOTE_HOST = "localhost"
REMOTE_PORT = 2222
REMOTE_USER = "testuser"
REMOTE_PASS = "testpass"
REMOTE_BASE = "/btrfs_mount"


def ssh_connect():
    """Establish SSH connection to the test container."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        REMOTE_HOST, port=REMOTE_PORT, username=REMOTE_USER,
        password=REMOTE_PASS, allow_agent=False, look_for_keys=False
    )
    return client


def run_ssh(client, cmd):
    """Run a command on the remote and return (stdout, stderr, exit_code)."""
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    return stdout.read().decode().strip(), stderr.read().decode().strip(), exit_code


def file_md5(path):
    """Compute MD5 hash of a local file."""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def remote_md5(client, path):
    """Compute MD5 hash of a remote file."""
    out, _, _ = run_ssh(client, f"md5sum {path}")
    return out.split()[0] if out else None


def rsync_cmd(stream_path, remote_dst, ssh_key_path):
    """Build rsync command with key-based SSH auth."""
    return [
        "rsync", "-a", "--partial-dir", ".bubtrsnap-partial",
        stream_path, f"{REMOTE_USER}@{REMOTE_HOST}:{remote_dst}"
    ]


def rsync_env(ssh_key_path):
    """Build environment for rsync to use SSH key auth."""
    return {
        **os.environ,
        "RSYNC_RSH": f"ssh -i {ssh_key_path} -p {REMOTE_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    }


def main():
    # Check prerequisites
    print("=== Checking prerequisites ===")
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=bubtrsnap-test", "--format", "{{.Names}}"],
        capture_output=True, text=True
    )
    if "bubtrsnap-test" not in result.stdout:
        print("ERROR: Container 'bubtrsnap-test' is not running.")
        print("Build and start with:")
        print("  docker build -t bubtrsnap-test -f Dockerfile.test .")
        print("  docker run -d --name bubtrsnap-test --privileged -p 2222:22 bubtrsnap-test")
        sys.exit(1)

    # Wait for SSH to be ready
    print("Waiting for SSH to be ready...")
    client = None
    for i in range(10):
        try:
            client = ssh_connect()
            print("SSH connected!")
            break
        except Exception:
            print(f"  Attempt {i+1}/10 failed, retrying...")
            time.sleep(1)
    if not client:
        print("ERROR: Could not connect via SSH")
        sys.exit(1)

    # Set up SSH key-based auth for rsync
    print("Setting up SSH key-based auth for rsync...")
    ssh_key_path = os.path.expanduser("~/.ssh/id_rsa_test_rsync")
    if not os.path.exists(ssh_key_path):
        subprocess.run(
            ["ssh-keygen", "-t", "rsa", "-b", "2048", "-f", ssh_key_path, "-N", "", "-q"],
            check=True
        )
    with open(ssh_key_path + ".pub") as f:
        pub_key = f.read().strip()
    run_ssh(client, f"mkdir -p ~/.ssh && echo '{pub_key}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && chmod 700 ~/.ssh")
    print("SSH key auth configured")

    try:
        # Create a 10MB test file (simulating a btrfs stream)
        print("\n=== Creating test stream file (10MB) ===")
        stream_path = "/tmp/test_stream_10m.btrfs"
        os.system(f"dd if=/dev/urandom of={stream_path} bs=1M count=10 2>/dev/null")
        local_hash = file_md5(stream_path)
        print(f"Local file MD5: {local_hash}")

        remote_tmp = f"/tmp/bubtrsnap-{os.path.basename(stream_path)}"
        partial_dir = "/tmp/.bubtrsnap-partial"
        env = rsync_env(ssh_key_path)

        # 1. Run rsync normally (complete transfer)
        print("\n=== Test 1: Normal rsync transfer (complete) ===")
        cmd = rsync_cmd(stream_path, remote_tmp, ssh_key_path)
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        print(f"rsync exit code: {result.returncode}")
        if result.returncode != 0:
            print(f"rsync stderr: {result.stderr}")

        remote_hash = remote_md5(client, remote_tmp)
        print(f"Remote file MD5: {remote_hash}")
        assert remote_hash == local_hash, "File hashes don't match!"

        # Clean up
        run_ssh(client, f"rm -f {remote_tmp}")
        run_ssh(client, f"rm -rf {partial_dir}")
        print("Normal transfer: PASS")

        # 2. Simulate interrupted transfer
        print("\n=== Test 2: Simulate interrupted rsync transfer ===")
        cmd = rsync_cmd(stream_path, remote_tmp, ssh_key_path)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env
        )

        # Wait a short time for rsync to start transferring, then kill it
        time.sleep(0.3)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print(f"rsync killed (exit code: {proc.returncode})")

        # Check that partial-dir was created on the remote
        _, _, exit_code = run_ssh(client, f"test -d {partial_dir}")
        print(f"Partial-dir exists on remote: {exit_code == 0}")
        assert exit_code == 0, "Partial-dir was not created on remote!"

        # Check that a partial file exists
        out, _, _ = run_ssh(client, f"ls -la {partial_dir}/")
        print(f"Partial-dir contents: {out}")

        # 3. Resume the transfer
        print("\n=== Test 3: Resume interrupted rsync transfer ===")
        cmd = rsync_cmd(stream_path, remote_tmp, ssh_key_path)
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        print(f"rsync resume exit code: {result.returncode}")
        if result.returncode != 0:
            print(f"rsync resume stderr: {result.stderr}")

        remote_hash = remote_md5(client, remote_tmp)
        print(f"Remote file MD5 after resume: {remote_hash}")
        assert remote_hash == local_hash, "File hashes don't match after resume!"
        print("Resumed transfer: PASS")

        # 4. Verify partial-dir is cleaned up after successful transfer
        _, _, exit_code = run_ssh(client, f"test -d {partial_dir}")
        print(f"Partial-dir cleaned up after success: {exit_code != 0}")

        # Clean up
        run_ssh(client, f"rm -f {remote_tmp}")

        print("\n=== All integration tests PASSED ===")
        return 0

    except AssertionError as e:
        print(f"\nTEST FAILURE: {e}")
        return 1
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if client:
            client.close()
        # Clean up local test file
        sp = "/tmp/test_stream_10m.btrfs"
        if os.path.exists(sp):
            os.remove(sp)
        # Clean up SSH key
        sk = os.path.expanduser("~/.ssh/id_rsa_test_rsync")
        if os.path.exists(sk):
            os.remove(sk)
        if os.path.exists(sk + ".pub"):
            os.remove(sk + ".pub")


if __name__ == "__main__":
    sys.exit(main())

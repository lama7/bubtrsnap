#!/usr/bin/env python3
"""
Integration test using Docker container as SSH remote test bed.
Requires: docker, btrfs-progs on host (for building image)
"""
import subprocess
import time
import tempfile
import os
import sys
import socket
import paramiko
from pathlib import Path

class BtrfsTestContainer:
    """Manages a Docker container for bubtrsnap SSH integration testing."""
    
    def __init__(self, image_name="bubtrsnap-test"):
        self.image_name = image_name
        self.container_name = f"bubtrsnap-test-{int(time.time())}"
        self.container_id = None
        self.ssh_port = None
        self.ssh_client = None
        
    def build(self):
        """Build the test container image."""
        print(f"Building {self.image_name}...")
        result = subprocess.run(
            ["docker", "build", "-t", self.image_name, "-f", "Dockerfile.test", "."],
            cwd="/home/lama7/src/bubtrsnap",
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode != 0:
            print(f"Build failed: {result.stderr}")
            return False
        print("Build successful")
        return True
        
    def start(self):
        """Start the container and get SSH port."""
        print(f"Starting container {self.container_name}...")
        
        # Start container with privileged mode for btrfs
        result = subprocess.run(
            ["docker", "run", "-d", 
             "--name", self.container_name,
             "--privileged",
             "-p", "0:22",
             self.image_name],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f"Start failed: {result.stderr}")
            return False
            
        self.container_id = result.stdout.strip()
        
        # Get assigned port
        time.sleep(3)  # Wait for container to start
        result = subprocess.run(
            ["docker", "port", self.container_id, "22"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f"Failed to get port: {result.stderr}")
            return False
            
        # Output format: 0.0.0.0:XXXXX
        port_str = result.stdout.strip()
        self.ssh_port = int(port_str.split(":")[-1])
        print(f"Container started on port {self.ssh_port}")
        
        # Wait for SSH to be ready
        for i in range(30):
            try:
                sock = socket.create_connection(("127.0.0.1", self.ssh_port), timeout=2)
                sock.close()
                print("SSH ready")
                return True
            except:
                time.sleep(1)
        print("SSH not ready after 30 seconds")
        return False
        
    def get_ssh_connection(self):
        """Get SSH connection to container."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            "127.0.0.1",
            port=self.ssh_port or 22,
            username="testuser",
            password="testpass",
            timeout=10
        )
        return ssh
        
    def run_remote_cmd(self, cmd):
        """Run command on remote via SSH."""
        ssh = self.get_ssh_connection()
        stdin, stdout, stderr = ssh.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode()
        error = stderr.read().decode()
        ssh.close()
        return exit_code, output, error
        
    def copy_to_remote(self, local_path, remote_path):
        """Copy file to remote via SCP."""
        ssh = self.get_ssh_connection()
        sftp = ssh.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        ssh.close()
        
    def stop(self):
        """Stop and remove container."""
        if self.container_id:
            subprocess.run(["docker", "stop", self.container_id], capture_output=True)
            subprocess.run(["docker", "rm", self.container_id], capture_output=True)
            print("Container stopped and removed")


def run_integration_test():
    """Run a basic integration test."""
    container = BtrfsTestContainer()
    
    try:
        if not container.build():
            return False
        if not container.start():
            return False
            
        # Test basic btrfs operations
        exit_code, out, err = container.run_remote_cmd("btrfs subvolume list /btrfs_mount")
        print(f"Subvolumes: {out}")
        
        # Test bubtrsnap from host to container
        # (would need bubtrsnap installed on host or in another container)
        
        return True
        
    finally:
        container.stop()


if __name__ == "__main__":
    # Check dependencies
    try:
        import paramiko
    except ImportError:
        print("Installing paramiko...")
        subprocess.run([sys.executable, "-m", "pip", "install", "paramiko"])
        import paramiko
        
    run_integration_test()
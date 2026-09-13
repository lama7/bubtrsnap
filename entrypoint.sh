#!/bin/sh
set -e

# Configure SSH to allow password authentication
echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config

# Start SSH daemon
/usr/sbin/sshd

# Create a 2GB btrfs loopback image and mount it
echo "Creating btrfs loopback image..."
dd if=/dev/zero of=/btrfs_pool/btrfs.img bs=1M count=2048 2>&1
echo "Formatting btrfs..."
mkfs.btrfs /btrfs_pool/btrfs.img 2>&1
echo "Setting up loop device..."
losetup -f /btrfs_pool/btrfs.img
echo "Mounting btrfs..."
mount -o loop /btrfs_pool/btrfs.img /btrfs_mount

# Create subvolumes
mkdir -p /btrfs_mount/snapshots
mkdir -p /btrfs_mount/backups
mkdir -p /btrfs_mount/streams

# Keep container running
echo "SSH daemon started on port 22"
echo "btrfs mounted at /btrfs_mount"

# Wait forever
exec tail -f /dev/null

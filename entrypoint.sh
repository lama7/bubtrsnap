#!/bin/bash
# Entrypoint script for btrfs test container (Alpine)
set -e

# Ensure loop module is loaded
modprobe loop 2>/dev/null || true

# Setup btrfs filesystem on first run
if [ ! -f /btrfs_pool/btrfs.img ]; then
    echo "Creating btrfs filesystem..."
    mkdir -p /btrfs_pool
    truncate -s 2G /btrfs_pool/btrfs.img
    mkfs.btrfs /btrfs_pool/btrfs.img
fi

# Mount btrfs
mkdir -p /btrfs_mount
if ! mountpoint -q /btrfs_mount; then
    mount -o loop /btrfs_pool/btrfs.img /btrfs_mount
fi

# Create subvolumes if they don't exist
if [ ! -d /btrfs_mount/snapshots ]; then
    btrfs subvolume create /btrfs_mount/snapshots
fi
if [ ! -d /btrfs_mount/backups ]; then
    btrfs subvolume create /btrfs_mount/backups
fi
if [ ! -d /btrfs_mount/streams ]; then
    mkdir -p /btrfs_mount/streams
fi

# Fix permissions
chown -R testuser:testuser /btrfs_mount

# Start SSH daemon
exec /usr/sbin/sshd -D
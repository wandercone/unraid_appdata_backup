# Docker Appdata Backup for Unraid

A Python utility to back up and restore Docker container appdata (files and configuration) on Unraid and other Docker hosts, locally or remotely over SSH.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
  - [Options](#options)
- [Configuration](#configuration)
  - [Example config.yaml](#example-configyaml)
- [How it Works](#how-it-works)
- [Notes](#notes)
- [Troubleshooting](#troubleshooting)

---

## Features

- Backup and restore Docker container appdata (files and configuration) on Unraid
- Supports grouping containers for batch operations
- Works with local and remote Docker hosts via SSH
- Flexible storage: grouped or flat backup directory structure
- Optionally stops/restarts containers before/after backup or restore for consistency
- Dry-run and debug logging modes for safe testing
- Colorized, structured logging output

## Requirements

- Python 3.7 or later
- Docker Engine on target hosts
- `rsync` installed on both source and destination systems
- SSH access for remote operations
- Python packages:
  - `docker`
  - `pyyaml`
  - `schema`
  - `colorlog`

## Installation

Install Python dependencies with:

```bash
pip install -r requirements.txt
```

Ensure `rsync` and `ssh` are available on all participating hosts.

## Usage

```bash
python backup_script.py [options]
```

### Options

| Option                      | Description                                                      |
|-----------------------------|------------------------------------------------------------------|
| `--group GROUP`             | Only back up the specified group as defined in `config.yaml`     |
| `--restore`                 | Perform a restore operation (all groups by default)              |
| `--restore-group GROUP`     | Restore a specific group                                         |
| `--restore-container NAME`  | Restore a specific container (requires `--restore-group`)        |
| `--dry-run`                 | Show what would happen without making changes                    |
| `--debug`                   | Enable verbose debug logging                                     |

## Configuration

All settings are defined in a `config.yaml` file located in the same directory as the script.

### Example `config.yaml`

```yaml
# Path where all backups will be stored.
backup_destination: /mnt/user/backup/appdata

# Whether to organize backups into subfolders based on groups (e.g., group-1, group-2).
store_by_group: yes

# Definition of backup groups
groups:
  group-1:
    - name: container-a
      host: local
      appdata_path: /mnt/user/appdata/container-a
      restart: yes

    - name: container-b
      host: local
      appdata_path: /mnt/user/appdata/container-b
      restart: yes

  group-2:
    - name: container-c
      host: 10.253.0.2
      ssh_key: /mnt/user/system/keys/ssh_key.pub
      ssh_port: 2222
      appdata_path: /docker/container-c
      restart: yes

    - name: container-d
      host: 10.253.0.2
      ssh_key: /mnt/user/system/keys/ssh_key.pub
      ssh_port: 22
      appdata_path: /docker/container-d
      restart: yes
```

## How it Works

- For each container, the script may:
  - Optionally stop the container for backup consistency
  - Export the container's configuration to JSON
  - Use `rsync` to copy the appdata directory to the backup destination
  - Optionally restart the container

- Restore operations support group or per-container granularity, syncing data back to the original path and restarting containers if configured.

- If present, notifications are sent using Unraid's `notify` script.

## Notes

- The script assumes `rsync` and `ssh` are available.
- For remote hosts, ensure SSH key-based access is set up.
- This tool does **not** back up Docker images; only configurations and appdata directories are saved.
- Tested with Unraid and standard Docker hosts.

## Troubleshooting

- Ensure all dependencies are installed on both local and remote hosts.
- If SSH connections fail, verify SSH keys and network connectivity.
- Use `--debug` for verbose logging to diagnose issues.
- Check permissions for reading appdata and writing to the backup destination.

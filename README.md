# Unraid Docker Appdata Backup Tool

A Python utility for backing up Docker container appdata and configuration on Unraid systems. This tool supports container stop/start sequencing, selective backup groups, JSON config export, `rsync`-based appdata sync, and Unraid system notifications.

## Features
- Backup appdata directories using `rsync`
- Save Docker container configurations to JSON
- Gracefully stop and restart containers during backup
- Support for multiple groups of containers
- Dry-run mode to simulate operations
- Debug mode for verbose output
- Unraid notification integration

## Usage
Required Python packages:
- `docker`
- `pyyaml`
- `colorlog`

```bash
python backup.py [--group GROUP_NAME] [--dry-run] [--debug]
```

### Arguments:
- `--group`: Specify a group from `config.yaml` to back up (default: all groups)
- `--dry-run`: Simulate actions without making changes
- `--debug`: Enable verbose logging

## Notifications

Unraid notifications are triggered on:
- Container stop/start failures
- rsync errors
- Missing or invalid configuration

Notifications use `/usr/local/emhttp/webGui/scripts/notify`, so this script is best run on Unraid OS.

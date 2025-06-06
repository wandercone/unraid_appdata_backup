## Features

- Backup and restore Docker container appdata (files and configuration) on Unraid
- Supports grouping containers for batch operations
- Operates on local and remote Docker hosts via SSH
- Grouped or flat backup storage
- Optionally stops/restarts containers around backup/restore for consistency
- Dry-run and debug logging modes for safe testing
- Colorized, structured logging output

## Requirements

- Python 3.7+
- Docker Engine on target hosts
- `rsync` installed on source and destination systems
- SSH access for remote operations
- The following Python packages:
  - `docker`
  - `pyyaml`
  - `schema`
  - `colorlog`

Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

```bash
python backup_script.py [options]
```

### Options

- `--group GROUP`  
  Only back up the specified group as defined in `config.yaml`.

- `--restore`  
  Perform a restore operation (all groups by default).

- `--restore-group GROUP`  
  Restore a specific group.

- `--restore-container CONTAINER`  
  Restore a specific container (requires `--restore-group`).

- `--dry-run`  
  Show what would happen without making changes.

- `--debug`  
  Enable verbose debug logging.

## Configuration

All settings are defined in a `config.yaml` file in the same directory as the script.

### Example `config.yaml`

```yaml
# Path where all backups will be stored.
backup_destination: /mnt/user/backup/appdata

# Whether to organize backups into subfolders based on groups (e.g., group-1, group-2).
store_by_group: yes

# Definition of backup groups
groups:
  group-1:
    # First container in group-1
    - name: container-a  # Name of the container
      host: local  # Container is running on the local machine
      appdata_path: /mnt/user/appdata/container-a  # Path to the container's appdata/config directory
      restart: yes  # Restart the container after backup is completed

    # Second container in group-1
    - name: container-b
      host: local
      appdata_path: /mnt/user/appdata/container-b
      restart: yes

  group-2:
    # First container in group-2, hosted on a remote server
    - name: container-c
      host: 10.253.0.2  # IP address of the remote host
      ssh_key: /mnt/user/system/keys/ssh_key.pub  # Public SSH key for accessing the remote host
      ssh_port: 2222  # Custom SSH port used by the remote host
      appdata_path: /docker/container-c  # Appdata path on the remote server
      restart: yes

    # Second container in group-2, also on the same remote host
    - name: container-d
      host: 10.253.0.2
      ssh_key: /mnt/user/system/keys/ssh_key.pub
      ssh_port: 22  # Standard SSH port
      appdata_path: /docker/container-d
      restart: yes
```

## How it Works

- For each container, the script can:
  - Optionally stop the container
  - Dump the container's configuration to JSON
  - Rsync the appdata directory to the backup destination
  - Optionally restart the container

- Restore operations support group or per-container granularity, and will rsync data back to the correct path and restart containers if needed.

- Notifications are sent using Unraid's `notify` script (if present).


## Notes

- The script assumes `rsync` and `ssh` are available.
- For remote hosts, ensure SSH key-based access is set up.
- The script does not manage Docker images; it only backs up configurations and appdata directories.
- Tested with Unraid and standard Docker hosts.

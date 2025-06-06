import json
import os
import subprocess
import argparse
import logging
import docker
import yaml
from pathlib import Path
from colorlog import ColoredFormatter

CONFIG_FILE = 'config.yaml'

# Setting up logging
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter(
    fmt='%(log_color)s[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    log_colors={
        'DEBUG':    'cyan',
        'INFO':     'green',
        'WARNING':  'yellow',
        'ERROR':    'red',
        'CRITICAL': 'bold_red',
    }
))

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.propagate = False

_docker_clients = {}

def get_docker_client(host='local'):
    if host not in _docker_clients:
        _docker_clients[host] = set_docker_client(host)
    return _docker_clients[host]

def set_docker_client(host='local'):
    if host == 'local':
        return docker.from_env()
    else:
        remote_docker_url = f'tcp://{host}:2375'
        return docker.DockerClient(base_url=remote_docker_url)

def remote_path_exists(host, ssh_user, ssh_key, ssh_port, remote_path):
    check_cmd = ["ssh", "-o", "BatchMode=yes", "-p", str(ssh_port)]
    if ssh_key:
        check_cmd.extend(["-i", ssh_key])
    check_cmd.append(f"{ssh_user}@{host}")
    check_cmd.append(f"test -d '{remote_path}'")
    try:
        subprocess.run(check_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False

def is_container_running(container_id, host, docker_client):
    try:
        container = docker_client.containers.get(container_id)
        return container.status == 'running'
    except docker.errors.NotFound:
        logger.warning(f"Container not found: {container_id}")
        return False

def stop_container(container_id, docker_client, host, dry_run=False):
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Stopping container: {container_id} on {host}")
    if dry_run:
        return
    try:
        container = docker_client.containers.get(container_id)
        container.stop()
    except Exception as e:
        sub = f"Error stopping {container_id}"
        msg = f"{e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)

def start_container(container_id, docker_client, host, dry_run=False):
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Starting container: {container_id} on {host}")
    if dry_run:
        return
    try:
        container = docker_client.containers.get(container_id)
        container.start()
    except Exception as e:
        sub = f"Error starting {container_id}"
        msg = f"{e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)

def backup_container_appdata(source_path, dest_root, container_id, host, ssh_user, ssh_key=None, ssh_port=22, dry_run=False, debug=False):
    source = Path(source_path)
    dest_path = Path(dest_root) / container_id
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Backing up data from {host}:{source} to {dest_path}")

    if dry_run:
        logger.info(f"- DRY RUN - Would create directory {dest_path} if it doesn't exist")
        logger.info(f"- DRY RUN - Would rsync from {host}:{source} to {dest_path}")
        return

    if host == "local":
        if not source.exists():
            raise FileNotFoundError(f"Source path does not exist: {source}")
    else:
        if not remote_path_exists(host, ssh_user, ssh_key, ssh_port, source):
            raise FileNotFoundError(f"Remote source path does not exist: {host}:{source}")

    try:
        dest_path.mkdir(parents=True, exist_ok=True)

        rsync_command = ["rsync", "-a", "--info=progress2", "--delete"]

        if host != "local":
            ssh_command = f"/usr/bin/ssh -o Compression=no -x -p {ssh_port}"
            if ssh_key:
                ssh_command += f" -i {ssh_key}"
            rsync_command.extend(["-e", ssh_command])
            rsync_command.append(f"{ssh_user}@{host}:{source}/")
        else:
            rsync_command.append(f"{source}/")

        rsync_command.append(str(dest_path))

        if debug:
            rsync_command.append("-v")
            logger.debug(f"Running command: {' '.join(rsync_command)}")

        result = subprocess.run(
            rsync_command,
            check=True,
            text=True,
            capture_output=debug
        )
        logger.info(f"Backup complete: {dest_path}")
        if debug:
            if result.stdout:
                logger.debug(f"rsync stdout:\n{result.stdout}")
            if result.stderr:
                logger.debug(f"rsync stderr:\n{result.stderr}")
    except subprocess.CalledProcessError as e:
        sub = f"Backup error"
        msg = f"rsync failed for {container_id}: {e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)
        if debug and e.stdout:
            logger.debug(f"rsync stdout:\n{e.stdout}")
        if debug and e.stderr:
            logger.debug(f"rsync stderr:\n{e.stderr}")

def restore_container_appdata(backup_root, container_id, dest_path, host, ssh_user, ssh_key=None, ssh_port=22, dry_run=False, debug=False):
    src_path = Path(backup_root) / container_id
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Restoring data to {host}:{dest_path} from {src_path}")
    
    if dry_run:
        logger.info(f"- DRY RUN - Would rsync from {src_path} to {host}:{dest_path}")
        return

    if not src_path.exists():
        raise FileNotFoundError(f"Backup path does not exist: {src_path}")

    try:
        rsync_command = ["rsync", "-a", "--info=progress2", "--delete"]

        if host != "local":
            ssh_command = f"/usr/bin/ssh -o Compression=no -x -p {ssh_port}"
            if ssh_key:
                ssh_command += f" -i {ssh_key}"
            rsync_command.extend(["-e", ssh_command])
            rsync_command.append(f"{str(src_path)}/")
            rsync_command.append(f"{ssh_user}@{host}:{dest_path}/")
        else:
            rsync_command.append(f"{str(src_path)}/")
            rsync_command.append(str(dest_path))

        if debug:
            rsync_command.append("-v")
            logger.debug(f"Running restore command: {' '.join(rsync_command)}")

        result = subprocess.run(
            rsync_command,
            check=True,
            text=True,
            capture_output=debug
        )
        logger.info(f"Restore complete for appdata of {container_id}")
        if debug and result.stdout:
            logger.debug(result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"rsync failed during restore of {container_id}: {e}")
        if debug and e.stdout:
            logger.debug(e.stdout)
        if debug and e.stderr:
            logger.debug(e.stderr)
        notify_host("Restore error", str(e), icon="alert", dry_run=dry_run)

def backup_container_json(container_id, backup_root, docker_client, host, dry_run=False):
    json_path = Path(backup_root) / f"{container_id}.json"
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Saving container config to {json_path}")
    if dry_run:
        logger.info(f"- DRY RUN - Would write JSON config to {json_path}")
        return
    try:
        container = docker_client.containers.get(container_id)
        config_data = container.attrs
        with json_path.open('w') as f:
            json.dump(config_data, f, indent=2)
        logger.info(f"Saved config for {container_id} to {json_path}")
    except docker.errors.NotFound:
        logger.warning(f"Container {container_id} not found.")
    except docker.errors.APIError as e:
        sub = f"Backup error"
        msg = f"Failed to inspect container {container_id}: {e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)

def notify_host(subject, message, icon, dry_run=False):
    if dry_run:
        logger.info(f"- DRY RUN - Would send notification: [{subject}] {message}")
        return
    try:
        subprocess.run([
            "/usr/local/emhttp/webGui/scripts/notify",
            "-e", "Unraid Appdata Backup Routine",
            "-s", subject,
            "-d", message,
            "-i", icon
        ], check=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to send notification: {e}")

def main():
    parser = argparse.ArgumentParser(description="Unraid docker appdata backup tool")
    parser.add_argument("--group", type=str, help="Name of the group to back up (defaults to all groups)")
    parser.add_argument("--restore", action="store_true", help="Perform a restore operation (defaults to all groups)")
    parser.add_argument("--restore-group", type=str, help="Perform the restore of a specific group")
    parser.add_argument("--restore-container", type=str, help="Perform the restore of a specific container (requires --restore-group)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled.")

    try:
        with open(CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        notify_host("File not found Error", f"Config file '{CONFIG_FILE}' not found.", icon="alert", dry_run=args.dry_run)
        logger.critical(f"Config file '{CONFIG_FILE}' not found.")
        return
    except yaml.YAMLError as e:
        logger.critical(f"Failed to parse YAML config: {e}")
        return

    required_keys = ['backup_destination', 'groups']
    for key in required_keys:
        if key not in config:
            logger.critical(f"Missing key in config: {key}")
            return

    if args.group and args.group not in config["groups"]:
        notify_host("Backup error", f"Group '{args.group}' not found in config.", icon="alert", dry_run=args.dry_run)
        logger.error(f"Group '{args.group}' not found in config.")
        return

    groups_to_process = (
        {args.group: config["groups"][args.group]} if args.group else config["groups"]
    )
    store_by_group = config.get("store_by_group", False)

    # --------------------------
    # RESTORE BACKUP TO GROUP / GROUP + CONTAINER
    # --------------------------
    if args.restore:
        if args.restore_container and not args.restore_group:
            logger.error("Must specify --restore-group if using --restore-container")
            return

        restore_groups = (
            {args.restore_group: config["groups"][args.restore_group]} 
            if args.restore_group else config["groups"]
        )

        stopped_containers = set()

        for group_name, containers in restore_groups.items():
            backup_root = (
                Path(config["backup_destination"]) / group_name 
                if store_by_group else Path(config["backup_destination"])
            )
            logger.info(f"Restoring group: {group_name}")

            for container in containers:
                container_id = container["name"]
                host = container.get("host", "local")
                ssh_user = container.get("ssh_user")
                ssh_key = container.get("ssh_key")
                ssh_port = container.get("ssh_port", 22)
                appdata_path = container.get("appdata_path")
                client = get_docker_client(host)

                if args.restore_container and container_id != args.restore_container:
                    continue

                if is_container_running(container_id, host, client):
                    stop_container(container_id, client, host, dry_run=args.dry_run)
                    stopped_containers.add((container_id, host))

                if appdata_path:
                    try:
                        restore_container_appdata(
                            backup_root, container_id, appdata_path, host,
                            ssh_user, ssh_key, ssh_port,
                            dry_run=args.dry_run, debug=args.debug
                        )
                    except Exception as e:
                        logger.error(f"Appdata restore failed for {container_id}: {e}")
                        notify_host("Restore error", str(e), icon="alert", dry_run=args.dry_run)

                if (container_id, host) in stopped_containers:
                    start_container(container_id, client, host, dry_run=args.dry_run)

        return  # Done with restore, skip backup

    # --------------------------
    # PERFORM A BACKUP IF --restore IS NOT PASSED
    # --------------------------
    for group_name, containers in groups_to_process.items():
        backup_root = Path(config["backup_destination"]) / group_name if store_by_group else Path(config["backup_destination"])
        if args.dry_run:
            logger.info(f"- DRY RUN - Would create directory {backup_root} if it doesn't exist")
        else:
            backup_root.mkdir(parents=True, exist_ok=True)

        logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Processing group: {group_name}")
        containers_to_restart = []

        # Step 1: Stop containers marked for restart
        for container in containers:
            container_id = container["name"]
            host = container.get("host", "local")
            client = get_docker_client(host)
            restart_value = container.get("restart", False)
            should_restart = str(restart_value).lower() == "yes" if isinstance(restart_value, str) else bool(restart_value)

            if should_restart and is_container_running(container_id, host, client):
                containers_to_restart.append(container_id)
                stop_container(container_id, client, host, dry_run=args.dry_run)
            elif should_restart:
                logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}{container_id} was not running on {host}, skipping stop.")
            else:
                logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Skipping stop for {container_id} on {host} (restart=no).")

        # Step 2: Perform backup
        for container in containers:
            container_id = container["name"]
            host = container.get("host", "local")
            ssh_user = container.get("ssh_user")
            ssh_key = container.get("ssh_key")
            ssh_port = container.get("ssh_port", 22)
            client = get_docker_client(host)
            source_path = container.get("appdata_path")

            backup_container_json(container_id, backup_root, client, host, dry_run=args.dry_run)

            if not source_path:
                logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Skipping data backup for {container_id} (no path).")
                continue

            try:
                backup_container_appdata(
                    source_path, backup_root, container_id, host,
                    ssh_user, ssh_key, ssh_port,
                    dry_run=args.dry_run, debug=args.debug
                )
            except Exception as e:
                notify_host(f"Backup error for {container_id}", str(e), icon="alert", dry_run=args.dry_run)
                logger.error(f"{container_id} backup failed: {e}")

        # Step 3: Start previously stopped containers
        for container_id in reversed(containers_to_restart):
            start_container(container_id, client, host, dry_run=args.dry_run)

if __name__ == '__main__':
    main()

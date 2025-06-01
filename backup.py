import json
import os
import subprocess
import argparse
import logging
import docker
import yaml
from pathlib import Path
from colorlog import ColoredFormatter

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

CONFIG_FILE = 'config.yaml'
client = docker.from_env()

def is_container_running(container_id):
    try:
        container = client.containers.get(container_id)
        return container.status == 'running'
    except docker.errors.NotFound:
        logger.warning(f"Container not found: {container_id}")
        return False

def stop_container(container_id, dry_run=False):
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Stopping container: {container_id}")
    if dry_run:
        return
    try:
        container = client.containers.get(container_id)
        container.stop()
    except Exception as e:
        sub = f"Error stopping {container_id}"
        msg = f"{e}"
        notify_host(sub, msg, icon="alert")
        logger.error(msg)   

def start_container(container_id, dry_run=False):
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Starting container: {container_id}")
    if dry_run:
        return
    try:
        container = client.containers.get(container_id)
        container.start()
    except Exception as e:
        sub = f"Error starting {container_id}"
        msg = f"{e}"
        notify_host(sub, msg, icon="alert")
        logger.error(msg)         

def backup_container_appdata(source_path, dest_root, container_id, dry_run=False, debug=False):
    source = Path(source_path)
    dest_path = Path(dest_root) / container_id
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Backing up data from {source} to {dest_path}")
    if dry_run:
        return
    if not source.exists():
        raise FileNotFoundError(f"Source path does not exist: {source}")
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
        rsync_command = [
            "rsync", "-a", "--info=progress2", "--delete"
        ]
        if debug:
            rsync_command.append("-v")
            logger.debug(f"Running command: {' '.join(rsync_command)}")
        rsync_command.extend([f"{source}/", str(dest_path)])
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
        notify_host(sub, msg, icon="alert")
        logger.error(msg)       
        if debug and e.stdout:
            logger.debug(f"rsync stdout:\n{e.stdout}")
        if debug and e.stderr:
            logger.debug(f"rsync stderr:\n{e.stderr}")

def backup_container_json(container_id, backup_root, dry_run=False):
    json_path = Path(backup_root) / f"{container_id}.json"
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Saving container config to {json_path}")
    if dry_run:
        return
    try:
        container = client.containers.get(container_id)
        config_data = container.attrs
        with json_path.open('w') as f:
            json.dump(config_data, f, indent=2)
        logger.info(f"Saved config for {container_id} to {json_path}")
    except docker.errors.NotFound:
        logger.warning(f"Container {container_id} not found.")
    except docker.errors.APIError as e:
        sub = f"Backup error"
        msg = f"Failed to inspect container {container_id}: {e}"
        notify_host(sub, msg, icon="alert")
        logger.error(msg)

def notify_host(subject, message, icon):
    try:
        subprocess.run([
            "/usr/local/emhttp/webGui/scripts/notify",
            "-e", "Unraid Appdata Backup Routine",
            "-s", subject,
            "-d", message,
            "-i", icon
        ], check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to send notification: {e}")

def main():
    parser = argparse.ArgumentParser(description="Unraid docker appdata backup tool")
    parser.add_argument("--group", type=str, help="Name of the group to back up (defaults to all groups)")
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
        sub = "File not found Error"
        msg = f"Config file '{CONFIG_FILE}' not found."
        notify_host(sub, msg, icon="alert")
        logger.critical(msg)
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
        sub = f"Backup error"
        msg = f"Group '{args.group}' not found in config."
        notify_host(sub, msg, icon="alert")
        logger.error(msg)
        return

    groups_to_process = (
        {args.group: config["groups"][args.group]} if args.group else config["groups"]
    )

    backup_root = Path(config["backup_destination"])
    backup_root.mkdir(parents=True, exist_ok=True)

    for group_name, containers in groups_to_process.items():
        logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Processing group: {group_name}")
        containers_to_restart = []

        # Stop containers (if restart flag is set)
        for container_id, _, restart_flag in containers:
            should_restart = bool(restart_flag)
            if should_restart and is_container_running(container_id):
                containers_to_restart.append(container_id)
                stop_container(container_id, dry_run=args.dry_run)
            elif should_restart:
                logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}{container_id} was not running, skipping stop.")
            else:
                logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Skipping stop for {container_id} (restart=0).")

        # Backup container data
        for container_id, source_path, _ in containers:
            backup_container_json(container_id, backup_root, dry_run=args.dry_run)

            if not source_path:
                logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Skipping data backup for {container_id} (no path).")
                continue

            try:
                backup_container_appdata(source_path, backup_root, container_id, dry_run=args.dry_run, debug=args.debug)
            except Exception as e:
                sub = f"Backup error for {container_id}"
                msg = f"{e}"
                notify_host(sub, msg, icon="alert")
                logger.error(msg)

        # Restart containers in reverse order
        for container_id in reversed(containers_to_restart):
            start_container(container_id, dry_run=args.dry_run)

if __name__ == '__main__':
    main()

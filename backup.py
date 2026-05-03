import json
import os
import subprocess
import argparse
import logging
import sys
import time
import shlex
import docker
import yaml
from docker.errors import DockerException
from pathlib import Path
from colorlog import ColoredFormatter
from schema import Schema, And, Or, Use, Optional, SchemaError

CONFIG_FILE = 'config.yaml'
LOCK_FILE = '/tmp/unraid_appdata_backup.lock'

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

config_schema = Schema({
    'backup_destination': And(str, len),
    Optional('store_by_group'): Or(bool, And(str, lambda s: s.lower() in ['yes', 'no'])),
    'groups': {
        str: [
            {
                'name': And(str, len),
                Optional('host'): And(str, len),
                Optional('ssh_user'): And(str, len),
                Optional('ssh_key'): And(str, len),
                Optional('ssh_port'): And(Use(int), lambda n: 0 < n < 65536),
                Optional('appdata_path'): And(str, len),
                Optional('restart'): Or(bool, And(str, lambda s: s.lower() in ['yes', 'no'])),
                Optional('start_delay'): And(Use(int), lambda n: n >= 0)
            }
        ]
    }
})

def acquire_lock():
    try:
        # Atomically create the lock file with O_CREAT|O_EXCL
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        # Lock file already exists, check if it's stale
        try:
            with open(LOCK_FILE, 'r') as f:
                pid = int(f.read().strip())
            # Check if the process is still running
            os.kill(pid, 0)
            return False  # Process is still running
        except (OSError, ValueError, FileNotFoundError):
            # Stale lock file (process dead or invalid PID), try again
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass
            # Retry lock acquisition once
            try:
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                return False
    except Exception as e:
        logger.error(f"Unexpected error acquiring lock: {e}")
        return False

def release_lock():
    try:
        with open(LOCK_FILE, 'r') as f:
            pid = int(f.read().strip())
        # Only remove if we own the lock
        if pid == os.getpid():
            os.remove(LOCK_FILE)
    except (OSError, ValueError, FileNotFoundError) as e:
        logger.debug(f"Could not release lock: {e}")

def _log_summary(summary, operation='Backup', dry_run=False):
    ok      = sum(1 for _, _, s, _ in summary if s == 'ok')
    failed  = sum(1 for _, _, s, _ in summary if s == 'failed')
    skipped = sum(1 for _, _, s, _ in summary if s == 'skipped')

    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}{operation} summary: {ok} ok, {failed} failed, {skipped} skipped")
    for container_id, host, status, detail in summary:
        suffix = f" — {detail}" if detail else ""
        logger.info(f"  {container_id} on {host}: {status.upper()}{suffix}")

    if not dry_run:
        if failed:
            failed_names = ', '.join(f"{c} ({h})" for c, h, s, _ in summary if s == 'failed')
            msg = f"{ok} ok, {failed} failed, {skipped} skipped. Failed: {failed_names}"
            notify_host(f"{operation} complete", msg, icon="warning")
        else:
            msg = f"{ok} ok" + (f", {skipped} skipped" if skipped else "")
            notify_host(f"{operation} complete", msg, icon="normal")

    return 1 if failed else 0

def validate_remote_containers(config):
    for group_name, containers in config["groups"].items():
        for container in containers:
            # Only require ssh_user for remote containers that need SSH (have appdata_path)
            if (container.get("host", "local") != "local" and
                container.get("appdata_path") and
                not container.get("ssh_user")):
                raise ValueError(
                    f"Container '{container['name']}' in group '{group_name}' "
                    f"has a remote host but no 'ssh_user' defined."
                )

def get_docker_client(host='local'):
    if host in _docker_clients:
        try:
            _docker_clients[host].ping()
        except Exception:
            logger.warning(f"Cached Docker client for '{host}' is stale, reconnecting...")
            del _docker_clients[host]
    if host not in _docker_clients:
        client = set_docker_client(host)
        if client is None:
            logger.critical(f"Could not create Docker client for host: {host}")
            return None
        _docker_clients[host] = client
    return _docker_clients[host]

def set_docker_client(host='local', timeout=30):
    try:
        if host == 'local':
            logger.debug("Connecting to local Docker engine...")
            return docker.from_env(timeout=timeout)
        else:
            remote_docker_url = f'tcp://{host}:2375'
            logger.debug(f"Connecting to remote Docker at {remote_docker_url} with timeout={timeout}s...")
            return docker.DockerClient(base_url=remote_docker_url, timeout=timeout)
    except DockerException as e:
        logger.error(f"Failed to connect to Docker on host '{host}': {e}")
        return None

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
        return True
    try:
        container = docker_client.containers.get(container_id)
        container.stop()
        return True
    except Exception as e:
        sub = f"Error stopping {container_id}"
        msg = f"{e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)
        return False

def start_container(container_id, docker_client, host, dry_run=False):
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Starting container: {container_id} on {host}")
    if dry_run:
        return True
    try:
        container = docker_client.containers.get(container_id)
        container.start()
        return True
    except Exception as e:
        sub = f"Error starting {container_id}"
        msg = f"{e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)
        return False

def backup_container_appdata(source_path, dest_root, container_id, host, ssh_user, ssh_key=None, ssh_port=22, dry_run=False, debug=False):
    source = Path(source_path)
    dest_path = Path(dest_root) / container_id
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Backing up data from {host}:{source} to {dest_path}")

    if dry_run:
        logger.info(f"- DRY RUN - Would create directory {dest_path} if it doesn't exist")
        logger.info(f"- DRY RUN - Would rsync from {host}:{source} to {dest_path}")
        return True

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
        return True
    except subprocess.CalledProcessError as e:
        sub = f"Backup error"
        msg = f"rsync failed for {container_id}: {e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)
        if debug and e.stdout:
            logger.debug(f"rsync stdout:\n{e.stdout}")
        if debug and e.stderr:
            logger.debug(f"rsync stderr:\n{e.stderr}")
        return False

def restore_container_appdata(backup_root, container_id, dest_path, host, ssh_user, ssh_key=None, ssh_port=22, dry_run=False, debug=False):
    src_path = Path(backup_root) / container_id
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Restoring data to {host}:{dest_path} from {src_path}")

    if dry_run:
        logger.info(f"- DRY RUN - Would rsync from {src_path} to {host}:{dest_path}")
        return True

    if not src_path.exists():
        raise FileNotFoundError(f"Backup path does not exist: {src_path}")

    try:
        if host != "local":
            mkdir_cmd = ["ssh", "-o", "BatchMode=yes", "-p", str(ssh_port)]
            if ssh_key:
                mkdir_cmd.extend(["-i", ssh_key])
            mkdir_cmd.append(f"{ssh_user}@{host}")
            mkdir_cmd.append(f"mkdir -p {shlex.quote(str(dest_path))}")
            subprocess.run(mkdir_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"rsync failed during restore of {container_id}: {e}")
        if debug and e.stdout:
            logger.debug(e.stdout)
        if debug and e.stderr:
            logger.debug(e.stderr)
        notify_host("Restore error", str(e), icon="alert", dry_run=dry_run)
        return False

def backup_container_json(container_id, backup_root, docker_client, host, dry_run=False):
    json_path = Path(backup_root) / f"{container_id}.json"
    logger.info(f"{'- DRY RUN -  ' if dry_run else ''}Saving container config to {json_path}")
    if dry_run:
        logger.info(f"- DRY RUN - Would write JSON config to {json_path}")
        return True
    try:
        container = docker_client.containers.get(container_id)
        config_data = container.attrs
        with json_path.open('w') as f:
            json.dump(config_data, f, indent=2)
        logger.info(f"Saved config for {container_id} to {json_path}")
        return True
    except docker.errors.NotFound:
        logger.warning(f"Container {container_id} not found.")
        return False
    except docker.errors.APIError as e:
        sub = f"Backup error"
        msg = f"Failed to inspect container {container_id}: {e}"
        notify_host(sub, msg, icon="alert", dry_run=dry_run)
        logger.error(msg)
        return False

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
    parser.add_argument("--restore-container", type=str, help="Perform the restore of a specific container (requires --restore-group or --group)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled.")

    if not acquire_lock():
        logger.critical("Another instance of the backup script is already running. Exiting.")
        notify_host("Backup error", "Another instance is already running.", icon="alert")
        return 1

    try:
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = yaml.safe_load(f)
        except FileNotFoundError:
            notify_host("File not found Error", f"Config file '{CONFIG_FILE}' not found.", icon="alert", dry_run=args.dry_run)
            logger.critical(f"Config file '{CONFIG_FILE}' not found.")
            return 1
        except yaml.YAMLError as e:
            logger.critical(f"Failed to parse YAML config: {e}")
            return 1

        try:
            config_schema.validate(config)
            logger.info("Config schema validation successful.")
        except SchemaError as e:
            notify_host("Schema Error", f"Config validation error: {e}", icon="alert", dry_run=args.dry_run)
            logger.critical(f"Config schema validation failed: {e}")
            return 1

        try:
            validate_remote_containers(config)
        except ValueError as e:
            notify_host("Config Error", str(e), icon="alert", dry_run=args.dry_run)
            logger.critical(str(e))
            return 1

        if args.group and args.group not in config["groups"]:
            notify_host("Backup error", f"Group '{args.group}' not found in config.", icon="alert", dry_run=args.dry_run)
            logger.error(f"Group '{args.group}' not found in config.")
            return 1

        groups_to_process = (
            {args.group: config["groups"][args.group]} if args.group else config["groups"]
        )
        # Normalize store_by_group to a real boolean
        store_by_group_raw = config.get("store_by_group", False)
        if isinstance(store_by_group_raw, str):
            store_by_group = store_by_group_raw.lower() in ['yes', 'true', '1']
        else:
            store_by_group = bool(store_by_group_raw)

        summary = []

        # --------------------------
        # RESTORE BACKUP TO GROUP / GROUP + CONTAINER
        # --------------------------
        if args.restore:
            if args.restore_container and not args.restore_group and not args.group:
                logger.error("Must specify --restore-group or --group if using --restore-container")
                return 1

            effective_restore_group = args.restore_group or args.group
            if effective_restore_group and effective_restore_group not in config["groups"]:
                logger.error(f"Group '{effective_restore_group}' not found in config.")
                return 1
            restore_groups = (
                {effective_restore_group: config["groups"][effective_restore_group]}
                if effective_restore_group else config["groups"]
            )

            stopped_containers = set()
            failed_containers = set()
            container_matched = False

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
                    if client is None:
                        logger.error(f"Skipping container {container_id} due to Docker connection issue on {host}")
                        summary.append((container_id, host, 'skipped', 'Docker connection failed'))
                        continue
                    if args.restore_container and container_id != args.restore_container:
                        continue

                    container_matched = True
                    status = 'ok'
                    detail = ''

                    if is_container_running(container_id, host, client):
                        if not stop_container(container_id, client, host, dry_run=args.dry_run):
                            failed_containers.add((container_id, host))
                            status = 'failed'
                            detail = 'failed to stop container'
                            summary.append((container_id, host, status, detail))
                            continue
                        stopped_containers.add((container_id, host))

                    if appdata_path:
                        try:
                            if not restore_container_appdata(
                                backup_root, container_id, appdata_path, host,
                                ssh_user, ssh_key, ssh_port,
                                dry_run=args.dry_run, debug=args.debug
                            ):
                                status = 'failed'
                                detail = 'appdata restore failed'
                        except Exception as e:
                            status = 'failed'
                            detail = str(e)
                            logger.error(f"Appdata restore failed for {container_id}: {e}")
                            notify_host("Restore error", str(e), icon="alert", dry_run=args.dry_run)

                    if (container_id, host) in stopped_containers:
                        if not start_container(container_id, client, host, dry_run=args.dry_run):
                            if status == 'ok':
                                status = 'failed'
                                detail = 'failed to start container'

                    summary.append((container_id, host, status, detail))

            if args.restore_container and not container_matched:
                logger.warning(f"No container named '{args.restore_container}' found in the specified group(s).")
                _log_summary(summary, operation='Restore', dry_run=args.dry_run)
                return 1

            return _log_summary(summary, operation='Restore', dry_run=args.dry_run)

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
            failed_containers = set()

            # Step 1: Stop containers marked for restart
            for container in containers:
                container_id = container["name"]
                host = container.get("host", "local")
                client = get_docker_client(host)
                if client is None:
                    logger.error(f"Skipping container {container_id} due to Docker connection issue on {host}")
                    continue
                restart_value = container.get("restart", False)
                should_restart = str(restart_value).lower() == "yes" if isinstance(restart_value, str) else bool(restart_value)

                if should_restart and is_container_running(container_id, host, client):
                    if not stop_container(container_id, client, host, dry_run=args.dry_run):
                        failed_containers.add((container_id, host))
                        summary.append((container_id, host, 'failed', 'failed to stop container'))
                        continue
                    containers_to_restart.append(container_id)
                elif should_restart:
                    logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}{container_id} was not running on {host}, skipping stop.")
                else:
                    logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Skipping stop for {container_id} on {host} (restart=no).")

            # Step 2: Perform backup
            for container in containers:
                container_id = container["name"]
                host = container.get("host", "local")

                # Skip containers that failed to stop
                if (container_id, host) in failed_containers:
                    continue

                ssh_user = container.get("ssh_user")
                ssh_key = container.get("ssh_key")
                ssh_port = container.get("ssh_port", 22)
                client = get_docker_client(host)
                if client is None:
                    logger.error(f"Skipping container {container_id} due to Docker connection issue on {host}")
                    summary.append((container_id, host, 'skipped', 'Docker connection failed'))
                    continue

                status = 'ok'
                detail = ''
                source_path = container.get("appdata_path")

                if not backup_container_json(container_id, backup_root, client, host, dry_run=args.dry_run):
                    status = 'failed'
                    detail = 'JSON backup failed'

                if not source_path:
                    logger.info(f"{'- DRY RUN -  ' if args.dry_run else ''}Skipping data backup for {container_id} (no path).")
                    summary.append((container_id, host, status, detail or 'no appdata_path'))
                    continue

                try:
                    if not backup_container_appdata(
                        source_path, backup_root, container_id, host,
                        ssh_user, ssh_key, ssh_port,
                        dry_run=args.dry_run, debug=args.debug
                    ):
                        if status != 'failed':
                            status = 'failed'
                            detail = 'appdata backup failed'
                except Exception as e:
                    notify_host(f"Backup error for {container_id}", str(e), icon="alert", dry_run=args.dry_run)
                    logger.error(f"{container_id} backup failed: {e}")
                    status = 'failed'
                    detail = str(e)

                summary.append((container_id, host, status, detail))

            # Step 3: Start previously stopped containers
            for container_id in reversed(containers_to_restart):
                container_cfg = next((c for c in containers if c["name"] == container_id), {})
                host = container_cfg.get("host", "local")
                restart_client = get_docker_client(host)
                if restart_client is None:
                    logger.error(f"Skipping restart of container {container_id} due to Docker connection issue on {host}")
                    # Mark as failed in summary
                    for i, (cid, h, status, detail) in enumerate(summary):
                        if cid == container_id and h == host and status == 'ok':
                            summary[i] = (cid, h, 'failed', 'failed to restart container')
                            break
                    continue
                delay = container_cfg.get("start_delay", 0)
                if delay > 0:
                    logger.info(f"Waiting {delay} seconds before starting {container_id} on {host}")
                    if not args.dry_run:
                        time.sleep(delay)
                if not start_container(container_id, restart_client, host, dry_run=args.dry_run):
                    # Mark as failed in summary
                    for i, (cid, h, status, detail) in enumerate(summary):
                        if cid == container_id and h == host and status == 'ok':
                            summary[i] = (cid, h, 'failed', 'failed to start container')
                            break

        return _log_summary(summary, operation='Backup', dry_run=args.dry_run)

    finally:
        release_lock()

if __name__ == '__main__':
    sys.exit(main())

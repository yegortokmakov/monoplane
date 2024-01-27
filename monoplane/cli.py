import os
import pty
import subprocess
import sys
import termios
import threading
import readchar
import yaml
import psutil
import tabulate


manifest = {}


def load_manifest():
    global manifest
    with open("monoplane.yml", "r") as file:
        manifest = yaml.safe_load(file)


def print_process_status():
    statuses = []

    for service in manifest['services']:
        name = f"\033{service.get('color')}{service.get('name')}\033[0m "

        status = u"\033[92m\u2588\u2588\033[0m running" if service['process'].poll(
        ) is None else u"\033[91m\u2588\u2588\033[0m terminated"

        statuses.append([
            name,
            status,
            service['notes']
        ])

    print("\n\n\n")
    print(tabulate.tabulate(statuses, ['Service', 'Status',
          'Notes'], tablefmt="rounded_grid"))
    print("\n\n")


def stop_processes():
    for service in manifest['services']:
        try:
            process = psutil.Process(service['process'].pid)
            for child in process.children(recursive=True):
                child.kill()
            process.kill()
        except psutil.NoSuchProcess:
            pass

    _ = [s['process'].wait() for s in manifest['services']]

    for service in manifest['services']:
        del service['process']


def print_logs(prepend, stream):
    try:
        with open(stream) as f:
            while True:
                line = f.readline()
                if (line is None or line == ''):
                    continue
                line = line if '\n' in line else line + '\n'
                sys.stdout.write(prepend + line + '\033[0m')
                sys.stdout.flush()

    # Unfortunately with a pty, an
    # IOError will be thrown at EOF
    # On Python 2, OSError will be thrown instead.
    except (IOError, OSError) as e:
        print(e)
        pass


def start_processes():
    global manifest

    name_len = max([len(s['name']) for s in manifest['services']])

    for service in manifest['services']:
        envvars = {str(d['name']): str(d['value'])
                   for d in service.get("env", [])}

        stdout_upstream, stdout_downstream = pty.openpty()
        stderr_upstream, stderr_downstream = pty.openpty()

        # Run the command in the background
        process = subprocess.Popen(
            service.get("command"),
            shell=True,
            cwd=service.get("cwd"),
            stdout=stdout_downstream,
            stderr=stderr_downstream,
            stdin=subprocess.PIPE,
            env=dict(os.environ, **envvars),
            close_fds=True,
            text=True,
            preexec_fn=os.setsid
        )
        os.close(stdout_downstream)
        os.close(stderr_downstream)

        name = f"\033{service.get('color')}[{service.get('name').center(name_len)}]\033[0m "

        threading.Thread(target=print_logs, args=(
            name, stdout_upstream), daemon=True).start()

        color_stderr = '\033' + \
            manifest["color_stderr"] if "color_stderr" in manifest else '\033[0m'

        threading.Thread(target=print_logs, args=(
            name + color_stderr, stderr_upstream), daemon=True).start()

        print(
            f"** start {name} command \"{service.get('command')}\" in {service.get('cwd')} started with PID: {process.pid}")

        service['process'] = process


def build_processes():
    global manifest

    name_len = max([len(s['name']) for s in manifest['services']])

    for service in manifest['services']:
        if not service.get("build"):
            continue

        envvars = {str(d['name']): str(d['value'])
                   for d in service.get("env", [])}

        stdout_upstream, stdout_downstream = pty.openpty()
        stderr_upstream, stderr_downstream = pty.openpty()

        # Run the command in the background
        process = subprocess.Popen(
            "exec " + service.get("build"),
            shell=True,
            cwd=service.get("cwd"),
            stdout=stdout_downstream,
            stderr=stderr_downstream,
            stdin=subprocess.PIPE,
            env=dict(os.environ, **envvars),
            close_fds=True,
            text=True
        )
        os.close(stdout_downstream)
        os.close(stderr_downstream)

        name = f"** build \033{service.get('color')}[{service.get('name').center(name_len)}]\033[0m "

        threading.Thread(target=print_logs, args=(
            name, stdout_upstream), daemon=True).start()

        color_stderr = '\033' + \
            manifest["color_stderr"] if "color_stderr" in manifest else '\033[0m'

        threading.Thread(target=print_logs, args=(
            name + color_stderr, stderr_upstream), daemon=True).start()

        print(f"{name} build \"{service.get('build')}\" in {service.get('cwd')} started with PID: {process.pid}")


def cli():
    original_tty_state = termios.tcgetattr(sys.stdin)

    load_manifest()
    start_processes()

    while True:
        try:
            k = readchar.readkey()
            match k:
                case "r":
                    stop_processes()
                    load_manifest()
                    start_processes()
                case "s":
                    print_process_status()
                case "b":
                    build_processes()
                case "c":
                    os.system('clear')
        except KeyboardInterrupt:
            stop_processes()
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_tty_state)
            sys.exit(0)


if __name__ == "__main__":
    cli()

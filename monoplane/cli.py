import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

import readchar
import tabulate
import yaml


RESET = "\033[0m"

# How long a service gets to shut down on SIGTERM before it is SIGKILLed.
# Long enough for `docker compose` to stop containers and for skaffold to
# delete its deployments.
STOP_GRACE_SECONDS = 5

manifest = {}
build_processes = []

# Set while a deliberate stop is in progress, so the watcher threads do not
# report the resulting exits as service failures.
stopping = threading.Event()

# One line at a time reaches the terminal, whichever thread writes it.
output_lock = threading.Lock()

# Master fds of every live pty, so SIGWINCH can resize them all.
open_masters = set()
masters_lock = threading.Lock()


def load_manifest():
    global manifest
    with open("monoplane.yml", "r") as file:
        loaded = yaml.safe_load(file)

    if not isinstance(loaded, dict) or not loaded.get("services"):
        raise ValueError("monoplane.yml defines no services")

    manifest = loaded


def service_label(service, name_len):
    color = service.get("color", "[0m")
    return f"\033{color}[{service.get('name').center(name_len)}]\033[0m "


def stderr_color():
    return "\033" + manifest["color_stderr"] if "color_stderr" in manifest else RESET


def write_line(text):
    with output_lock:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def terminal_winsize():
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            return os.get_terminal_size(stream.fileno())
        except (OSError, ValueError, AttributeError):
            continue
    return os.terminal_size((80, 24))


def set_winsize(fd, columns, lines):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
    except OSError:
        pass


def resize_ptys(_signum=None, _frame=None):
    size = terminal_winsize()
    with masters_lock:
        fds = list(open_masters)
    for fd in fds:
        set_winsize(fd, size.columns, size.lines)


def open_log_pty(size):
    master, slave = pty.openpty()
    set_winsize(master, size.columns, size.lines)
    with masters_lock:
        open_masters.add(master)
    return master, slave


def print_logs(prefix, fd):
    buffer = b""
    try:
        while True:
            data = os.read(fd, 65536)
            # A pty master reads EOF on macOS once the child has closed the
            # slave; on Linux the same moment raises EIO.
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                write_line(prefix + line.decode("utf-8", "replace").rstrip("\r") + RESET)
    except OSError:
        pass
    finally:
        if buffer:
            write_line(prefix + buffer.decode("utf-8", "replace").rstrip("\r") + RESET)
        with masters_lock:
            open_masters.discard(fd)
        try:
            os.close(fd)
        except OSError:
            pass


def watch_service(service, label):
    code = service["process"].wait()
    if stopping.is_set():
        return
    write_line(f"** exit {label}exited with code {code}")


def watch_build(process, label):
    code = process.wait()
    write_line(f"** build {label}finished with code {code}")


def spawn(service, command, label):
    envvars = {str(d["name"]): str(d["value"]) for d in service.get("env", [])}
    size = terminal_winsize()

    stdout_upstream, stdout_downstream = open_log_pty(size)
    stderr_upstream, stderr_downstream = open_log_pty(size)

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=service.get("cwd"),
        stdout=stdout_downstream,
        stderr=stderr_downstream,
        stdin=subprocess.DEVNULL,
        env=dict(os.environ, **envvars),
        close_fds=True,
        # Puts the shell and everything it spawns in one process group, so a
        # pipeline like `skaffold dev | sed ...` can be signalled as a unit.
        # start_new_session rather than preexec_fn=os.setsid: the reader threads
        # below are already running by the second spawn, and preexec_fn is not
        # safe to use from a threaded parent.
        start_new_session=True,
    )
    os.close(stdout_downstream)
    os.close(stderr_downstream)

    threads = [
        threading.Thread(target=print_logs, args=(label, stdout_upstream), daemon=True),
        threading.Thread(
            target=print_logs, args=(label + stderr_color(), stderr_upstream), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    return process, threads


def start_processes():
    stopping.clear()
    name_len = max(len(s["name"]) for s in manifest["services"])

    for service in manifest["services"]:
        label = service_label(service, name_len)

        try:
            process, threads = spawn(service, service.get("command"), label)
        except OSError as e:
            write_line(f"** start {label}failed: {e}")
            continue

        service["process"] = process
        service["threads"] = threads

        write_line(
            f"** start {label}command \"{service.get('command')}\" in "
            f"{service.get('cwd')} started with PID: {process.pid}"
        )

        threading.Thread(
            target=watch_service, args=(service, label), daemon=True
        ).start()


def build_processes_run():
    name_len = max(len(s["name"]) for s in manifest["services"])

    for service in manifest["services"]:
        if not service.get("build"):
            continue

        label = f"\033{service.get('color', '[0m')}[{service['name'].center(name_len)}]\033[0m "

        try:
            # exec so the tracked pid is the build itself: `build` is a single
            # command, unlike `command`, which may be a shell pipeline.
            process, _ = spawn(service, "exec " + service["build"], "** build " + label)
        except OSError as e:
            write_line(f"** build {label}failed: {e}")
            continue

        build_processes.append(process)

        write_line(
            f"** build {label}\"{service['build']}\" in {service.get('cwd')} "
            f"started with PID: {process.pid}"
        )

        threading.Thread(target=watch_build, args=(process, label), daemon=True).start()


def signal_group(process, sig):
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except (ProcessLookupError, ValueError):
            pass


def terminate(processes):
    """SIGTERM the whole group, give it STOP_GRACE_SECONDS, then SIGKILL."""
    live = [p for p in processes if p.poll() is None]
    for process in live:
        signal_group(process, signal.SIGTERM)

    deadline = time.monotonic() + STOP_GRACE_SECONDS
    for process in live:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass

    for process in live:
        if process.poll() is None:
            signal_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def stop_services(services):
    services = [s for s in services if s.get("process")]
    terminate([s["process"] for s in services])

    # The readers exit on EOF once the children are gone, closing their pty as
    # they go. Joining them here is what keeps a restart from accumulating a
    # thread and two file descriptors per service.
    for service in services:
        for thread in service.pop("threads", []):
            thread.join(timeout=2)
        service.pop("process", None)


def stop_processes():
    stopping.set()
    stop_services(manifest.get("services", []))
    terminate(build_processes)
    build_processes.clear()


def reload_manifest():
    """Re-read monoplane.yml without disturbing what is already running.

    load_manifest replaces every service dict, so the handles for the running
    processes have to be carried across by name or there is nothing left to
    stop them with.
    """
    running = {s["name"]: s for s in manifest.get("services", []) if s.get("process")}
    load_manifest()

    for service in manifest["services"]:
        carried = running.pop(service["name"], None)
        if carried:
            service["process"] = carried["process"]
            service["threads"] = carried["threads"]

    if running:
        write_line(
            "** stop " + ", ".join(running) + ": no longer in monoplane.yml"
        )
        stopping.set()
        try:
            stop_services(running.values())
        finally:
            stopping.clear()


def print_process_status():
    statuses = []

    for service in manifest["services"]:
        name = f"\033{service.get('color', '[0m')}{service.get('name')}\033[0m "
        process = service.get("process")

        if process is None:
            status = "\033[91m██\033[0m not started"
        elif process.poll() is None:
            status = "\033[92m██\033[0m running"
        else:
            status = f"\033[91m██\033[0m exited ({process.returncode})"

        statuses.append([name, status, service.get("notes", "")])

    with output_lock:
        print("\n\n\n")
        print(
            tabulate.tabulate(
                statuses, ["Service", "Status", "Notes"], tablefmt="rounded_grid"
            )
        )
        print("\n\n")


def cli():
    try:
        original_tty_state = termios.tcgetattr(sys.stdin)
    except termios.error:
        original_tty_state = None

    # A closed terminal or a `kill` should tear the services down the same way
    # Ctrl-C does, rather than orphaning them.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    signal.signal(signal.SIGWINCH, resize_ptys)

    load_manifest()
    start_processes()

    try:
        while True:
            k = readchar.readkey()
            match k:
                case "r":
                    stop_processes()
                    try:
                        load_manifest()
                    except (OSError, yaml.YAMLError, ValueError) as e:
                        write_line(f"** monoplane.yml could not be reloaded: {e}")
                        continue
                    start_processes()
                case "s":
                    print_process_status()
                case "b":
                    # Picks up an edited build command or env without the
                    # restart that `r` would force on every service.
                    try:
                        reload_manifest()
                    except (OSError, yaml.YAMLError, ValueError) as e:
                        write_line(f"** monoplane.yml could not be reloaded: {e}")
                    build_processes_run()
                case "c":
                    os.system("clear")
                case "q":
                    break
    except KeyboardInterrupt:
        pass
    finally:
        stop_processes()
        if original_tty_state is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_tty_state)


if __name__ == "__main__":
    cli()

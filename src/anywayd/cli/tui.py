import asyncio
import functools
import random
import signal
import string

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from typing import Any, cast, final, override
from uuid import UUID

from dbus_fast import Variant
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.types import NoSelection
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.data_table import ColumnKey, RowDoesNotExist

from anywayd.cli.dbus_client import DBusClient, dbus_client
from anywayd.daemon.models import Process
from anywayd.constants import OBJECT_PATH, SERVICE_NAME

LOG_DIR_TEMPLATE = "/var/log/anywayd/{uuid}"


@final
class DBusClientLifecycleManager:
    """Manages a single shared DBusClient connection for the TUI application."""

    def __init__(
        self, service_name: str, object_path: str, interface_name: str
    ) -> None:
        self._service_name = service_name
        self._object_path = object_path
        self._interface_name = interface_name
        self._client: DBusClient | None = None
        self._cm: Any = None  # pyright: ignore[reportExplicitAny]

    async def connect(self) -> None:
        """Establish the DBus connection."""
        if self._client is None:
            self._cm = dbus_client(
                self._service_name, self._object_path, self._interface_name
            )
            self._client = await self._cm.__aenter__()  # pyright: ignore[reportAny]

    async def disconnect(self) -> None:
        """Close the DBus connection."""
        if self._client is not None:
            await self._cm.__aexit__(None, None, None)  # pyright: ignore[reportAny]
            self._client = None
            self._cm = None

    def get_client(self) -> DBusClient:
        """Get the active client; raises RuntimeError if not connected."""
        if self._client is None:
            raise RuntimeError("DBusClient not connected. Call connect() first.")
        return self._client

    @asynccontextmanager
    async def use_client(self) -> AsyncGenerator[DBusClient, None]:
        """Context manager for using the client (ensures connection is active)."""
        if self._client is None:
            raise RuntimeError("DBusClient not connected. Call connect() first.")
        try:
            yield self._client
        except Exception:
            raise


def _unwrap(value: Any) -> Any:  # pyright: ignore[reportAny, reportExplicitAny]
    """Recursively unwrap dbus_fast Variants into plain Python values."""
    if isinstance(value, Variant):
        return _unwrap(value.value)  # pyright: ignore[reportAny]
    if isinstance(value, dict):
        return {
            k: _unwrap(v)
            for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(value, list):
        return [_unwrap(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value  # pyright: ignore[reportAny]


def _fmt_timestamp(ts: float) -> str:
    if ts < 0:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _read_file_sync(path: str) -> str:
    with open(path, "r", errors="replace") as f:
        return f.read()


async def _read_log_files_via_pkexec(
    stdout_path: str, stderr_path: str, run_as_user: str
) -> tuple[str, str]:
    rand_suffix = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    separator = f"---SEP_{rand_suffix}---"

    try:
        script = f"""
echo "STDOUT_START"
cat "{stdout_path}" 2>/dev/null || echo "(no stdout yet)"
echo "{separator}"
echo "STDERR_START"
cat "{stderr_path}" 2>/dev/null || echo "(no stderr yet)"
"""

        proc = await asyncio.create_subprocess_exec(
            "pkexec",
            "--user",
            run_as_user,
            "sh",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        return (
            "(pkexec not available on this system)",
            "(pkexec not available on this system)",
        )

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        error_msg = f"(failed to read as {run_as_user}: {err or 'permission denied'})"
        return error_msg, error_msg

    output = stdout.decode(errors="replace")

    sep_pos = output.find(separator)
    if sep_pos != -1:
        stdout_text = output[:sep_pos].strip()
        if stdout_text.startswith("STDOUT_START"):
            stdout_text = stdout_text[len("STDOUT_START") :].lstrip()

        stderr_text = output[sep_pos + len(separator) :].strip()
        if stderr_text.startswith("STDERR_START"):
            stderr_text = stderr_text[len("STDERR_START") :].lstrip()

        return stdout_text, stderr_text
    else:
        output_clean = output.strip()
        if output_clean.startswith("STDOUT_START"):
            output_clean = output_clean[len("STDOUT_START") :].lstrip()
        return output_clean, "(no stderr content)"


async def _read_log_files_consolidated(
    stdout_path: str, stderr_path: str, run_as_user: str
) -> tuple[str, str]:
    """Read both log files, using a single pkexec invocation if needed."""
    try:
        stdout_text = await asyncio.to_thread(_read_file_sync, stdout_path)
        stderr_text = await asyncio.to_thread(_read_file_sync, stderr_path)
        return stdout_text, stderr_text
    except FileNotFoundError:
        return "(no output yet)", "(no output yet)"
    except PermissionError:
        return await _read_log_files_via_pkexec(stdout_path, stderr_path, run_as_user)
    except Exception as exc:
        return f"(error reading stdout: {exc})", f"(error reading stderr: {exc})"


@final
class LogModalScreen(ModalScreen[None]):
    """Modal showing stdout/stderr for a single process, opened by clicking
    its row in the dashboard's process table."""

    BINDINGS = [("escape", "dismiss", "Close")]

    CSS = """
    LogModalScreen {
        align: center middle;
    }

    #log-modal-box {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $surface;
    }

    #log-modal-title {
        text-style: bold;
        background: $panel;
        padding: 0 1;
        width: 100%;
    }

    #log-modal-tabs {
        height: 1fr;
    }

    #modal-stdout, #modal-stderr {
        height: 1fr;
        border: none;
    }
    """

    def __init__(self, uuid: UUID, command: str, run_as_user: str) -> None:
        super().__init__()
        self._uuid = uuid
        self._command = command
        self._run_as_user = run_as_user

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="log-modal-box"):
            yield Static(f"{self._command}  ({self._uuid})", id="log-modal-title")
            with TabbedContent(id="log-modal-tabs"):
                with TabPane("stdout", id="modal-tab-stdout"):
                    yield RichLog(
                        id="modal-stdout", highlight=False, markup=False, wrap=True
                    )
                with TabPane("stderr", id="modal-tab-stderr"):
                    yield RichLog(
                        id="modal-stderr", highlight=False, markup=False, wrap=True
                    )

    async def on_mount(self) -> None:
        stdout_log = self.query_one("#modal-stdout", RichLog)
        stderr_log = self.query_one("#modal-stderr", RichLog)

        log_dir = LOG_DIR_TEMPLATE.format(uuid=self._uuid)
        stdout_text, stderr_text = await _read_log_files_consolidated(
            f"{log_dir}/stdout", f"{log_dir}/stderr", self._run_as_user
        )
        _ = stdout_log.write(stdout_text)
        _ = stderr_log.write(stderr_text)



@final
class SignalPickerScreen(ModalScreen[int]):
    """Modal screen for selecting a signal to send to a process."""

    BINDINGS = [("escape", "dismiss", "Cancel")]

    CSS = """
    SignalPickerScreen {
        align: center middle;
    }

    #signal-picker-box {
        width: 40;
        height: 20;
        border: round $accent;
        background: $surface;
        padding: 1;
    }

    #signal-picker-title {
        text-style: bold;
        background: $panel;
        padding: 0 1;
        width: 100%;
        margin-bottom: 1;
    }

    #signal-select {
        width: 100%;
        margin-bottom: 1;
    }

    #signal-picker-buttons {
        width: 100%;
        height: 3;
        align: center middle;
    }

    #signal-picker-buttons Button {
        margin: 0 1;
    }
    """

    # All available signals (excluding ones that might be problematic)
    SIGNALS = [
        ("SIGABRT", signal.SIGABRT),
        ("SIGALRM", signal.SIGALRM),
        ("SIGBUS", signal.SIGBUS),
        ("SIGCHLD", signal.SIGCHLD),
        ("SIGCONT", signal.SIGCONT),
        ("SIGFPE", signal.SIGFPE),
        ("SIGHUP", signal.SIGHUP),
        ("SIGILL", signal.SIGILL),
        ("SIGINT", signal.SIGINT),
        ("SIGKILL", signal.SIGKILL),
        ("SIGPIPE", signal.SIGPIPE),
        ("SIGPOLL", signal.SIGPOLL),
        ("SIGPROF", signal.SIGPROF),
        ("SIGPWR", signal.SIGPWR),
        ("SIGQUIT", signal.SIGQUIT),
        ("SIGSEGV", signal.SIGSEGV),
        ("SIGSTOP", signal.SIGSTOP),
        ("SIGSYS", signal.SIGSYS),
        ("SIGTERM", signal.SIGTERM),
        ("SIGTRAP", signal.SIGTRAP),
        ("SIGTSTP", signal.SIGTSTP),
        ("SIGTTIN", signal.SIGTTIN),
        ("SIGTTOU", signal.SIGTTOU),
        ("SIGURG", signal.SIGURG),
        ("SIGUSR1", signal.SIGUSR1),
        ("SIGUSR2", signal.SIGUSR2),
        ("SIGVTALRM", signal.SIGVTALRM),
        ("SIGWINCH", signal.SIGWINCH),
        ("SIGXCPU", signal.SIGXCPU),
        ("SIGXFSZ", signal.SIGXFSZ),
    ]

    def __init__(self, uuid: UUID, command: str, pid: int) -> None:
        super().__init__()
        self._uuid = uuid
        self._command = command
        self._pid = pid
        self._selected_signal: int | None = None

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="signal-picker-box"):
            yield Label(
                f"Select signal for {self._command} (PID={self._pid})",
                id="signal-picker-title",
            )
            yield Select(
                [(name, sig) for name, sig in self.SIGNALS],
                prompt="Choose a signal...",
                id="signal-select",
            )
            with Horizontal(id="signal-picker-buttons"):
                yield Button("Send", variant="primary", id="signal-send-btn")
                yield Button("Cancel", variant="default", id="signal-cancel-btn")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "signal-send-btn":
            select = cast(Select[int], self.query_one("#signal-select"))
            if select.value != Select.NULL and not isinstance(select.value, NoSelection):
                self._selected_signal = select.value
                _ = self.dismiss(self._selected_signal)
        elif event.button.id == "signal-cancel-btn":
            _ = self.dismiss(None)

    async def on_select_changed(self, event: Select.Changed) -> None:
        """Enable/disable send button based on selection."""
        send_btn = self.query_one("#signal-send-btn", Button)
        send_btn.disabled = event.value is None

    def on_mount(self) -> None:
        """Initialize the send button as disabled."""
        send_btn = self.query_one("#signal-send-btn", Button)
        send_btn.disabled = True
        
@final
class DashboardView(Static):
    """Daemon stats up top, tracked processes in a scrollable table below.

    Click (or select + Enter) a row to open a modal with that process's
    stdout/stderr.
    """

    BINDINGS = [
        ("k", "kill", "Kill (SIGKILL)"),
        ("t", "terminate", "Terminate (SIGTERM)"),
        ("i", "interrupt", "Interrupt (SIGINT)"),
        ("s", "signal_picker", "Send signal ...")
    ]

    def __init__(self) -> None:
        super().__init__()
        self._procs: dict[UUID, Process] = {}
        self._tbl_columns = ("UUID", "PID", "Command", "Run As", "Started", "Exit Code")
        self._update_lock = asyncio.Lock()

    @override
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dashboard-scroll"):
            yield Static("", id="dashboard-error")
            with Grid(id="dashboard-grid"):
                yield Static("...", id="stat-version", classes="stat-box")
                yield Static("...", id="stat-tracked", classes="stat-box")
                yield Static("...", id="stat-monitoring", classes="stat-box")
                yield Static("...", id="stat-total", classes="stat-box")
                yield Static("...", id="stat-running", classes="stat-box")
            yield DataTable(id="process-table", cursor_type="row")

    async def on_mount(self) -> None:
        app = cast(AnywaydApp, self.app)
        await app._client_manager.connect()  # pyright: ignore[reportPrivateUsage]
        client = app._client_manager._client  # pyright: ignore[reportPrivateUsage]
        assert client is not None
        await self._build()
        client.on("Changed", functools.partial(DashboardView._build, self))

    async def _send_signal_to_selected(self, signal_num: int) -> None:
        """Send a signal to the currently selected process."""
        table = cast(DataTable[str], self.query_one("#process-table", DataTable))
        try:
            row_key = table.get_row_at(table.cursor_row)[0]
        except RowDoesNotExist:
            return

        uuid = UUID(row_key)
        proc = self._procs.get(uuid)
        if proc is None:
            return

        app = cast(AnywaydApp, self.app)
        if proc.pid is None:
            app.notify(
                f"Process {uuid} is not running (no PID)",
                title="Signal Failed",
                severity="warning",
            )
            return

        try:
            async with (
                app._client_manager.use_client() as client  # pyright: ignore[reportPrivateUsage]
            ):
                success = await client.stop_process(  # pyright: ignore[reportAny]
                    uuid.hex, signal_num
                )
                if success:
                    signal_name = signal.Signals(signal_num).name
                    app.notify(
                        f"Sent {signal_name} to {proc.command} (PID={proc.pid})",
                        title="Signal Sent",
                        severity="information",
                    )
                else:
                    app.notify(
                        f"Failed to send signal to {proc.command} (PID={proc.pid})",
                        title="Signal Failed",
                        severity="error",
                    )
        except Exception as e:
            app.notify(
                f"Error sending signal: {e}",
                title="Signal Failed",
                severity="error",
            )

    async def action_kill(self) -> None:
        """Send SIGKILL to selected process."""
        await self._send_signal_to_selected(signal.SIGKILL)

    async def action_terminate(self) -> None:
        """Send SIGTERM to selected process."""
        await self._send_signal_to_selected(signal.SIGTERM)

    async def action_interrupt(self) -> None:
        """Send SIGINT to selected process."""
        await self._send_signal_to_selected(signal.SIGINT)


    async def action_signal_picker(self) -> None:
        """Open signal picker modal to send any signal."""
        table = cast(DataTable[str], self.query_one("#process-table", DataTable))
        row_key = table.get_row_at(table.cursor_row)[0]
        uuid = UUID(row_key)
        proc = self._procs.get(uuid)
        if proc is None:
            return

        app = cast(AnywaydApp, self.app)
        if proc.pid is None:
            app.notify(
                f"Process {uuid} is not running (no PID)",
                title="Signal Failed",
                severity="warning",
            )
            return


        async def show_picker():
            signal_num = await app.push_screen_wait(
                SignalPickerScreen(uuid, f"{proc.command} {proc.arguments}", proc.pid)  # pyright: ignore[reportArgumentType]
            )
            if signal_num is not None:  # pyright: ignore[reportUnnecessaryComparison]
                await self._send_signal_to_selected(signal_num)

        _ = app.run_worker(show_picker, exclusive=True)


    async def _build(self, uuids: list[str] | set[UUID] | None = None) -> None:
        async with self._update_lock:
            table = cast(DataTable[str], self.query_one("#process-table", DataTable))
            if uuids is not None:
                uuids = set(
                    UUID(uuid) if isinstance(uuid, str) else uuid for uuid in uuids
                )
            else:
                _ = table.add_columns(*[(x, x) for x in self._tbl_columns])

            if not uuids:
                uuids = None

            error = self.query_one("#dashboard-error", Static)

            try:
                async with cast(
                    AnywaydApp, self.app
                )._client_manager.use_client() as client:  # pyright: ignore[reportPrivateUsage]
                    version = cast(
                        str, await client.get_version()  # pyright: ignore[reportAny]
                    )
                    stats = cast(
                        dict[str, int],
                        _unwrap(await client.get_stats()),  # pyright: ignore[reportAny]
                    )
                    if uuids is None:
                        procs_data = await client.get_processes_by_user(  # pyright: ignore[reportAny]
                            stats.get("total_processes", 100)
                        )
                    else:
                        procs_data = (  # pyright: ignore[reportAny]
                            await client.get_processes(  # pyright: ignore[reportAny]
                                [uuid.hex for uuid in uuids]
                            )
                        )
                    procs = {
                        UUID(k): Process.from_dict(**v)  # pyright: ignore[reportAny]
                        for k, v in cast(  # pyright: ignore[reportAny]
                            dict[str, Any],  # pyright: ignore[reportExplicitAny]
                            _unwrap(procs_data),
                        ).items()
                    }
            except Exception as exc:
                error.update(f"[red]Failed to reach daemon:[/] {exc}")
                return

            self.query_one("#stat-version", Static).update(
                f"Version\n[bold]{version}[/]"
            )
            self.query_one("#stat-tracked", Static).update(
                f"Tracking\n[bold]{stats.get('tracked_processes', '-')}[/]"
            )
            self.query_one("#stat-monitoring", Static).update(
                f"Monitoring\n[bold]{stats.get('monitoring_tasks', '-')}[/]"
            )
            self.query_one("#stat-total", Static).update(
                f"Total\n[bold]{stats.get('total_processes', '-')}[/]"
            )
            self.query_one("#stat-running", Static).update(
                f"Running\n[bold]{stats.get('running_processes', '-')}[/]"
            )

            def add_row(proc: Process):
                _ = table.add_row(
                    *[self.get_proc_val(proc, column) for column in self._tbl_columns],
                    key=proc.uuid.hex,
                )

            if uuids is None:
                for proc in procs.values():
                    add_row(proc)
            else:
                _procs_set = set(self._procs)
                procs_set = set(procs)

                for uuid in uuids:
                    is_in_procs = uuid in procs_set
                    is_in_old = uuid in _procs_set

                    if is_in_procs and not is_in_old:
                        add_row(procs[uuid])
                    elif not is_in_procs and is_in_old:
                        table.remove_row(uuid.hex)
                        _ = self._procs.pop(uuid, None)
                    elif is_in_procs and is_in_old:
                        for column in table.columns:
                            table.update_cell(
                                uuid.hex, column, self.get_proc_val(procs[uuid], column)
                            )

            self._procs.update(procs)
            if not procs:
                error.update("No tracked processes.")

    def get_proc_val(self, proc: Process, column: ColumnKey | str) -> str:
        if isinstance(column, ColumnKey):
            assert column.value is not None
            column = column.value
        assert column in self._tbl_columns
        if column == "UUID":
            return str(proc.uuid)
        elif column == "PID":
            return str(proc.pid) if proc.pid is not None else "-"
        elif column == "Command":
            return f"{proc.command} {proc.arguments}"
        elif column == "Run As":
            return proc.run_as_user
        elif column == "Started":
            return _fmt_timestamp(proc.started_at.timestamp())
        elif column == "Exit Code":
            return str(proc.exit_code) if proc.exit_code is not None else "-"

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return
        uuid = UUID(event.row_key.value)
        proc = self._procs.get(uuid)
        if proc is None:
            return
        _ = cast(AnywaydApp, self.app).push_screen(
            LogModalScreen(uuid, f"{proc.command} {proc.arguments}", proc.run_as_user)
        )


@final
class AnywaydApp(App[None]):
    CSS = """
    #dashboard-grid {
        grid-size: 5;
        grid-gutter: 1;
        height: auto;
        margin: 1;
    }

    .stat-box {
        border: round $accent;
        padding: 1;
        text-align: center;
        height: 10;
    }

    #dashboard-error {
        margin: 1;
    }

    #dashboard-scroll {
        height: 1fr;
        margin: 0 1 1 1;
    }

    #process-table {
        height: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._client_manager = DBusClientLifecycleManager(
            SERVICE_NAME, OBJECT_PATH, SERVICE_NAME
        )

    @override
    def compose(self) -> ComposeResult:
        yield Header()
        yield DashboardView()
        yield Footer()

    @override
    async def action_quit(self) -> None:
        await self._client_manager.disconnect()
        await super().action_quit()

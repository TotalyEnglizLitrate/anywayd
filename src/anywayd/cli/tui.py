import asyncio
import random
import string

from datetime import datetime, UTC
from typing import Any, cast, final, override
from uuid import UUID

from dbus_fast import Variant
from textual.app import App, ComposeResult
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from anywayd.cli.dbus_client import dbus_client
from anywayd.daemon.models import Process
from anywayd.daemon.service import OBJECT_PATH, SERVICE_NAME

LOG_DIR_TEMPLATE = "/var/log/anywayd/{uuid}"


def _client():
    return dbus_client(SERVICE_NAME, OBJECT_PATH, SERVICE_NAME)


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


class DashboardView(Static):
    """Daemon stats up top, tracked processes in a scrollable table below.

    Click (or select + Enter) a row to open a modal with that process's
    stdout/stderr.
    """

    def __init__(self) -> None:
        super().__init__()
        self._procs: dict[UUID, Process] = {}

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
        table = cast(DataTable[str], self.query_one("#process-table", DataTable))
        _ = table.add_columns(
            "UUID", "PID", "Command", "Run As", "Started", "Exit Code"
        )

        error = self.query_one("#dashboard-error", Static)

        try:
            async with _client() as client:
                version = cast(
                    str, await client.get_version()  # pyright: ignore[reportAny]
                )
                stats = cast(
                    dict[str, int],
                    _unwrap(await client.get_stats()),  # pyright: ignore[reportAny]
                )
                procs = {
                    UUID(k): Process.from_dict(**v)  # pyright: ignore[reportAny]
                    for k, v in cast(  # pyright: ignore[reportAny]
                        dict[str, Any],  # pyright: ignore[reportExplicitAny]
                        _unwrap(
                            await client.get_processes_by_user(100)
                        ),  # pyright: ignore[reportAny]
                    ).items()
                }
        except Exception as exc:
            error.update(f"[red]Failed to reach daemon:[/] {exc}")
            return

        self.query_one("#stat-version", Static).update(f"Version\n[bold]{version}[/]")
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

        self._procs = procs
        for uuid, proc in procs.items():
            _ = table.add_row(
                str(uuid),
                str(proc.pid) if proc.pid is not None else "-",
                f"{proc.command} {proc.arguments}",
                proc.run_as_user,
                _fmt_timestamp(proc.started_at.timestamp()),
                str(proc.exit_code) if proc.exit_code is not None else "-",
                key=uuid.hex,
            )

        if not procs:
            error.update("No tracked processes.")

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return
        uuid = UUID(event.row_key.value)
        proc = self._procs.get(uuid)
        if proc is None:
            return
        self.app.push_screen(
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

    @override
    def compose(self) -> ComposeResult:
        yield Header()
        yield DashboardView()
        yield Footer()

import asyncio
import contextvars
import logging
import os
import pwd
import shlex
import signal

from typing import Annotated, Any, cast, final, override
from datetime import datetime, UTC
from uuid import UUID

from dbus_fast import DBusError, Variant
from dbus_fast.annotations import DBusBool, DBusSignature, DBusStr, DBusDict, DBusUInt32
from dbus_fast.constants import BusType, MessageType, NameFlag, RequestNameReply
from dbus_fast.service import ServiceInterface, dbus_method
from dbus_fast.aio.message_bus import MessageBus
from dbus_fast.message import Message
import psutil
from rich.console import Console
from rich.logging import RichHandler


from anywayd.daemon.models import Process
from anywayd.daemon.process_manager import ProcessManager, process_manager
from anywayd import __version__

SERVICE_NAME = "com.anywayd.daemon"
OBJECT_PATH = "/com/anywayd/daemon"
DB_PATH = "/var/lib/anywayd/anywayd.db"
DB_DIR = "/var/lib/anywayd"

curr_msg: contextvars.ContextVar[Message] = contextvars.ContextVar("message")

log = logging.getLogger("anywayd")


def setup_logging(level: int = logging.INFO) -> None:
    if logging.getLogger().handlers:
        return

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=Console(),
                rich_tracebacks=True,
                show_path=False,
                markup=True,
            )
        ],
    )


class AnywaydMessageBus(MessageBus):
    @override
    def _process_message(self, msg: Message) -> None:
        if msg.message_type is MessageType.METHOD_CALL and msg.path == OBJECT_PATH:
            token = curr_msg.set(msg)
            try:
                super()._process_message(msg)
            finally:
                curr_msg.reset(token)
        else:
            super()._process_message(msg)


@final
class AnywaydService(ServiceInterface):
    def __init__(
        self,
        process_manager: ProcessManager,
    ):
        super().__init__(SERVICE_NAME)
        self.process_manager = process_manager
        self._start_time = datetime.now(UTC)

    @dbus_method()
    async def StartProcess(
        self,
        command: DBusStr,
        env: DBusDict,
        working_dir: DBusStr,
        run_as: DBusStr = "",
    ) -> DBusStr:
        """
        Start a background process.

        Args:
            command: The command to run (full command line string)
            run_as: User to run as (username or UID), empty for current user
            shell: Which Shell to run the command via, empty for nothing
            env: Environment variables as dict {key: value}
            working_dir: Working directory for the process

        Returns:
            UUID of the started process
        """
        if run_as:
            try:
                _ = pwd.getpwnam(run_as)
            except KeyError:
                log.error("Unknown run_as user %r, falling back to caller", run_as)
                raise

        user = (await self.get_caller_info(curr_msg.get())).pw_name
        cmd = shlex.split(command)
        log.info(
            "StartProcess requested by [bold]%s[/] as [bold]%s[/]: %r (cwd=%r)",
            user,
            run_as or user,
            cmd,
            working_dir,
        )
        try:
            uuid = await self.process_manager.spawn(
                command=cmd,
                invoked_by_user=user,
                run_as_user=run_as or user,
                env={k: cast(str, v.value) for (k, v) in env.items()},
                cwd=working_dir,
            )
        except Exception:
            log.exception("Failed to spawn process %r for user %s", cmd, user)
            raise

        log.info("Started process [bold]%s[/] (%s)", uuid, cmd[0] if cmd else "")
        return str(uuid)

    @dbus_method()
    async def StopProcess(
        self, uuid: DBusStr, signal_num: DBusUInt32 = signal.SIGTERM
    ) -> DBusBool:
        """
        Stop a running process by UUID.

        Args:
            uuid: Process UUID
            signal_num: Signal to send (default: SIGTERM)

        Returns:
            True if process was stopped, Errors if process doesn't exist/not invoked by user
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        log.info(
            "StopProcess requested by %s: uuid=%s signal=%s", user, uuid, signal_num
        )
        try:
            ret = await self.process_manager.kill(UUID(uuid), user, signal_num)
        except psutil.NoSuchProcess:
            log.error("StopProcess: process %s not found for user %s", uuid, user)
            self.process_not_found(user, uuid=UUID(uuid))

        if not ret:
            log.error("Failed to kill process uuid=%s", uuid)
            raise DBusError(
                "com.anywayd.KillFailed", f"Failed to kill process with {uuid=}"
            )

        log.info("Stopped process %s", uuid)
        return True

    @dbus_method()
    async def GetProcesses(
        self, uuids: Annotated[list[str], DBusSignature("as")]
    ) -> Annotated[dict[str, DBusDict], DBusSignature("a{sa{sv}}")]:
        """
        Get information about processes.

        Args:
            uuids: List of process UUIDs

        Returns:
            List of Dicts representing a managed Process
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        log.info("GetProcesses requested by %s: %s", user, uuids)
        procs = await self.process_manager.get_processes_by_uuid(
            set(UUID(uuid) for uuid in uuids), user
        )
        return {str(proc.uuid): self._format_process(proc) for proc in procs}

    @dbus_method()
    async def GetProcessesByUser(
        self, limit: DBusUInt32 = 100
    ) -> Annotated[dict[str, DBusDict], DBusSignature("a{sa{sv}}")]:
        """
        List all tracked processes.

        Args:
            limit: Maximum number of processes to return

        Returns:
            List of Dicts representing managed Processes
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        log.info("GetProcessesByUser requested by %s: limit=%s", user, limit)
        procs = await self.process_manager.get_process_by_user(user, limit)
        return {str(proc.uuid): self._format_process(proc) for proc in procs}

    @dbus_method()
    async def GetStats(self) -> DBusDict:
        """
        List Process stats concerning calling user
        """
        user = (await self.get_caller_info(curr_msg.get())).pw_name
        log.info("GetStats requested by %s", user)
        return {
            k: Variant("x", v)
            for (k, v) in (await self.process_manager.get_stats(user)).items()
        }

    @dbus_method()
    async def GetVersion(self) -> DBusStr:
        """Get daemon version."""
        return __version__

    @dbus_method()
    async def GetCallerInfo(self) -> DBusUInt32:
        """
        Debug method to get caller information.
        Useful for testing credential retrieval.
        """
        return (await self.get_caller_info(curr_msg.get())).pw_uid

    async def get_caller_info(self, msg: Message) -> pwd.struct_passwd:
        bus = await MessageBus(
            bus_type=BusType.SYSTEM, negotiate_unix_fd=True
        ).connect()
        try:
            creds = await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="GetConnectionCredentials",
                    signature="s",
                    body=[msg.sender],
                )
            )
        finally:
            bus.disconnect()

        uid: int = cast(int, creds.body[0]["UnixUserID"].value)
        return pwd.getpwuid(uid)

    def _format_process(self, proc: Process) -> DBusDict:
        """Format process status for D-Bus response."""
        return {
            "uuid": Variant("s", str(proc.uuid)),
            "pid": Variant("x", proc.pid if proc.pid is not None else -1),
            "command": Variant("s", f"{proc.command} {proc.arguments}"),
            "env": Variant("s", proc.env),
            "cwd": Variant("s", proc.cwd),
            "invoked_by_user": Variant("s", proc.invoked_by_user),
            "run_as_user": Variant("s", proc.run_as_user),
            "started_at": Variant("d", proc.started_at.timestamp()),
            "ended_at": Variant(
                "d", proc.ended_at.timestamp() if proc.ended_at is not None else -1.0
            ),
            "exit_code": Variant(
                "x", proc.exit_code if proc.exit_code is not None else -1
            ),
            "boot_id": Variant("s", str(proc.boot_id)),
        }

    def process_not_found(
        self, user: str, pid: int | None = None, uuid: UUID | None = None
    ):
        if (pid, uuid) == (None, None):
            raise ValueError("One of uuid, pid must be specified")
        identifier = f"PID={pid}" if pid is not None else f"UUID={uuid}"
        raise DBusError(
            "com.anywayd.Error.ProcessNotFound",
            f"Process with {identifier} not found or not owned by {user=}",
        )


def asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],  # pyright: ignore[reportExplicitAny]
):
    exc: Exception | None = context.get("exception")
    if exc is not None:
        Console().print_exception()
    else:
        loop.default_exception_handler(context)


def install_asyncio_handler(loop: asyncio.AbstractEventLoop | None = None):
    loop = loop or asyncio.get_event_loop()
    loop.set_exception_handler(asyncio_exception_handler)


async def service(log_level: int = logging.INFO):
    setup_logging(log_level)
    install_asyncio_handler()
    os.makedirs(DB_DIR, exist_ok=True)
    bus = await AnywaydMessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        async with process_manager(f"sqlite+aiosqlite:///{DB_PATH}") as pm:
            service = AnywaydService(pm)
            bus.export(OBJECT_PATH, service)
            name_resp = await bus.request_name(
                SERVICE_NAME, flags=NameFlag.DO_NOT_QUEUE
            )

            if name_resp == RequestNameReply.EXISTS:
                log.critical("Unable to acquire %s, name already in use.", SERVICE_NAME)
                raise RuntimeError(
                    f"Unable to acquire {SERVICE_NAME=}, name already in use."
                )

            log.info("Anywayd D-Bus service running on [bold]%s[/]", OBJECT_PATH)
            log.info("Database: %s", DB_PATH)
            log.info("PID: %s", os.getpid())

            await bus.wait_for_disconnect()
    except asyncio.CancelledError:
        log.info("Received asyncio.CancelledError, shutting down")
    except Exception:
        log.exception("Bus disconnected unexpectedly")
        raise
    finally:
        bus.disconnect()
        log.info("Shutdown complete")

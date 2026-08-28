import asyncio
import json
import logging
import os
import signal
import sys

from collections.abc import Iterable
from datetime import datetime, UTC, timedelta
from typing import final
from contextlib import asynccontextmanager
from uuid import UUID

import psutil
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from anywayd.daemon.models import Base, Process, get_boot_id, get_uid_gid

log = logging.getLogger("anywayd")


@final
class ProcessManager:
    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False)
        self.async_session = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._processes: dict[UUID, asyncio.subprocess.Process | psutil.Process] = {}
        self._process_tasks: dict[UUID, asyncio.Task[None]] = {}

    async def initialize(self):
        log.debug("Initializing process manager, creating tables if needed")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        to_mark_dead: set[UUID] = set()
        boot_id: UUID = get_boot_id()
        async with self.async_session() as session:
            result = await session.execute(select(Process))
            rows = result.scalars().all()
            log.debug("Found %d process record(s) from previous run", len(rows))
            for process in rows:
                if (
                    process.boot_id == boot_id
                    and process.pid is not None
                    and await self.is_same_process(process)
                ):
                    log.info(
                        "Reattaching to still-running process %s (pid=%s)",
                        process.uuid,
                        process.pid,
                    )
                    process_ps = psutil.Process(process.pid)
                    self._processes[process.uuid] = process_ps
                    task = asyncio.create_task(self._monitor_process(process.uuid))
                    self._process_tasks[process.uuid] = task
                else:
                    to_mark_dead.add(process.uuid)

        if to_mark_dead:
            log.info("Marking %d stale process record(s) as dead", len(to_mark_dead))
        await self.mark_dead(to_mark_dead)

    async def mark_dead(self, uuid: set[UUID] | UUID):
        if isinstance(uuid, UUID):
            uuid = set((uuid,))
        if not uuid:
            return
        async with self.async_session() as session:
            _ = await session.execute(
                update(Process)
                .where(Process.uuid.in_(uuid) & (Process.pid.is_not(None)))
                .values(pid=None)
            )
            await session.commit()
        log.debug("Marked dead: %s", uuid)

    async def is_same_process(self, process_db: Process):
        if process_db.pid is None:
            return True
        if psutil.pid_exists(process_db.pid):
            curr_process = psutil.Process(process_db.pid)
            if abs(
                process_db.started_at
                - datetime.fromtimestamp(curr_process.create_time(), tz=UTC)
            ) > timedelta(seconds=1.0):
                log.debug(
                    "PID %s reused by a different process than uuid=%s (create_time mismatch)",
                    process_db.pid,
                    process_db.uuid,
                )
                return False

            return True
        await self.mark_dead(process_db.uuid)
        return False

    async def log_process_start(
        self,
        command: str,
        arguments: str,
        env: dict[str, str],
        working_dir: str,
        invoked_by_user: str,
        run_as_user: str,
    ) -> UUID:
        async with self.async_session() as session:
            process = Process(
                command=command,
                arguments=arguments,
                env=json.dumps(env),
                cwd=working_dir,
                invoked_by_user=invoked_by_user,
                run_as_user=run_as_user,
            )
            session.add(process)
            await session.commit()
            await session.refresh(process)
        log.debug("Recorded new process row uuid=%s command=%r", process.uuid, command)
        return process.uuid

    async def update_process_pid(self, uuid: UUID, pid: int):
        async with self.async_session() as session:
            result = await session.execute(select(Process).where(Process.uuid == uuid))
            process = result.scalar_one_or_none()
            if process:
                process.pid = pid
                await session.commit()
                log.debug("Updated uuid=%s with pid=%s", uuid, pid)
            else:
                log.warning("update_process_pid: no row found for uuid=%s", uuid)

    async def _log_process_end(self, uuid: UUID, exit_code: int):
        async with self.async_session() as session:
            result = await session.execute(select(Process).where(Process.uuid == uuid))
            process = result.scalar_one_or_none()
            if process:
                process.ended_at = datetime.now(UTC)
                process.exit_code = exit_code
                await session.commit()
                log.info("Process %s exited with code %s", uuid, exit_code)
            else:
                log.warning("_log_process_end: no row found for uuid=%s", uuid)

    async def spawn(
        self,
        command: list[str],
        invoked_by_user: str,
        run_as_user: str,
        cwd: str,
        env: dict[str, str],
    ) -> UUID:
        invoked_uid, invoked_gid = get_uid_gid(invoked_by_user)
        run_as_uid, run_as_gid = get_uid_gid(run_as_user)

        process_uuid = await self.log_process_start(
            command=command[0] if command else "",
            arguments=" ".join(command[1:]) if len(command) > 1 else "",
            env=env,
            working_dir=cwd,
            invoked_by_user=invoked_by_user,
            run_as_user=run_as_user,
        )

        log_dir = f"/var/log/anywayd/{process_uuid}"
        os.makedirs(log_dir, mode=0o700, exist_ok=True)

        stdout_path = os.path.join(log_dir, "stdout")
        stderr_path = os.path.join(log_dir, "stderr")
        privdrop = os.path.join(os.path.dirname(__file__), "_privdrop_exec.py")

        if invoked_by_user == run_as_user:
            dropper_cmd = [
                sys.executable,
                privdrop,
                "--uid",
                str(run_as_uid),
                "--gid",
                str(run_as_gid),
                "--ruid",
                str(run_as_uid),
                "--rgid",
                str(run_as_gid),
                "--stdout",
                stdout_path,
                "--stderr",
                stderr_path,
                "--",
                *command,
            ]
        else:
            log.info(
                "Cross-user spawn: %s -> %s via pkexec (uuid=%s)",
                invoked_by_user,
                run_as_user,
                process_uuid,
            )
            dropper_cmd = [
                sys.executable,
                privdrop,
                "--uid",
                str(invoked_uid),
                "--gid",
                str(invoked_gid),
                "--ruid",
                str(run_as_uid),
                "--rgid",
                str(run_as_gid),
                "--stdout",
                stdout_path,
                "--stderr",
                stderr_path,
                "--",
                "pkexec",
                "--user",
                run_as_user,
                *command,
            ]

        log.debug("Launching dropper: %s", dropper_cmd)
        try:
            process = await asyncio.create_subprocess_exec(
                *dropper_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except Exception:
            log.exception("Failed to launch process for uuid=%s", process_uuid)
            raise

        await self.update_process_pid(process_uuid, process.pid)
        self._processes[process_uuid] = process
        task = asyncio.create_task(self._monitor_process(process_uuid))
        self._process_tasks[process_uuid] = task

        log.info(
            "Spawned uuid=%s pid=%s as %s (invoked by %s)",
            process_uuid,
            process.pid,
            run_as_user,
            invoked_by_user,
        )
        return process_uuid

    async def _monitor_process(self, uuid: UUID):
        process = self._processes.get(uuid)
        if not process:
            log.warning(
                "_monitor_process: uuid=%s not tracked, nothing to monitor", uuid
            )
            return

        exited = False
        try:
            if isinstance(process, psutil.Process):
                # psutil.Process.wait() blocks a real OS thread with no way to
                # interrupt it from outside; asyncio cancellation only detaches
                # from the future, it doesn't stop the thread. Poll instead so
                # this coroutine is actually cancellable (needed for shutdown).
                exit_code = None
                while True:
                    try:
                        exit_code = await asyncio.to_thread(process.wait, timeout=1)
                        break
                    except psutil.TimeoutExpired:
                        continue
                exited = True
                await self._log_process_end(uuid, exit_code)
            else:
                exit_code = await process.wait()
                exited = True
                await self._log_process_end(uuid, exit_code)
        except asyncio.CancelledError:
            log.debug(
                "_monitor_process: monitoring for %s cancelled (not exited)", uuid
            )
            raise
        except Exception:
            log.exception("Error monitoring process %s", uuid)
        finally:
            _ = self._processes.pop(uuid, None)
            _ = self._process_tasks.pop(uuid, None)
            # Only mark dead if we actually observed the process exit. If we
            # were cancelled (e.g. daemon shutdown), the process is likely
            # still running and should stay tracked for reattachment on
            # the next startup.
            if exited:
                await self.mark_dead(uuid)

    async def kill(
        self, uuid: UUID, user: str, signal_num: int = signal.SIGTERM
    ) -> bool:
        process = self._processes.get(uuid)
        if not process:
            log.warning("kill: uuid=%s not tracked", uuid)
            raise psutil.NoSuchProcess(-1)

        process_db = await self.get_processes_by_uuid(uuid, user)
        if not process_db:
            log.warning("kill: uuid=%s not owned by %s or not found", uuid, user)
            raise psutil.NoSuchProcess(-1)

        log.info(
            "Killing uuid=%s pid=%s with signal=%s (by %s)",
            uuid,
            process.pid,
            signal_num,
            user,
        )
        try:
            os.killpg(process.pid, signal_num)
            return True
        except ProcessLookupError:
            log.warning("kill: process group for pid=%s already gone", process.pid)
            raise psutil.NoSuchProcess(-1)
        except PermissionError:
            log.debug("killpg denied for pid=%s, falling back to kill()", process.pid)
            try:
                os.kill(process.pid, signal_num)
                return True
            except (ProcessLookupError, PermissionError):
                log.warning("kill: fallback kill() failed for pid=%s", process.pid)
                return False
        except Exception:
            log.exception("Error killing process %s", uuid)
            return False

    async def get_processes_by_uuid(
        self, uuid: set[UUID] | UUID, user: str
    ) -> list[Process]:
        if isinstance(uuid, UUID):
            uuid = set((uuid,))
        async with self.async_session() as session:
            processes = (
                (
                    await session.execute(
                        select(Process).where(
                            (Process.uuid.in_(uuid)) & (Process.invoked_by_user == user)
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [process for process in processes if await self.is_same_process(process)]

    async def get_process_by_user(self, user: str, limit: int) -> list[Process]:
        async with self.async_session() as session:
            processes = (
                (
                    await session.execute(
                        select(Process)
                        .where(Process.invoked_by_user == user)
                        .order_by(Process.started_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        return [process for process in processes if await self.is_same_process(process)]

    async def get_process_by_pid(self, pid: int, user: str) -> Process | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(Process).where(
                    (Process.pid == pid) & (Process.invoked_by_user == user)
                )
            )
            candidates = result.scalars().all()

        for process in candidates:
            if await self.is_same_process(process):
                return process
        return None

    async def list_processes(
        self,
        user: str,
        limit: int = 100,
        offset: int = 0,
        running_only: bool = False,
    ) -> Iterable[Process]:
        async with self.async_session() as session:
            query = (
                select(Process)
                .order_by(Process.started_at.desc())
                .where(Process.invoked_by_user == user)
            )

            if running_only:
                query = query.where(Process.ended_at.is_(None))

            query = query.offset(offset).limit(limit)

            result = await session.execute(query)
            return result.scalars().all()

    async def count_processes(self, user: str, running_only: bool = False) -> int:
        async with self.async_session() as session:
            from sqlalchemy import func

            query = (
                select(func.count())
                .select_from(Process)
                .where(Process.invoked_by_user == user)
            )

            if running_only:
                query = query.where(Process.ended_at.is_(None))

            result = await session.execute(query)
            ret = result.scalar()
            assert ret is not None
            return ret

    async def cleanup_finished(self):
        finished_uuids: list[UUID] = []
        for uuid, task in self._process_tasks.items():
            if task.done():
                finished_uuids.append(uuid)

        for uuid in finished_uuids:
            _ = self._processes.pop(uuid, None)
            _ = self._process_tasks.pop(uuid, None)

        if finished_uuids:
            log.debug("cleanup_finished: reaping %d task(s)", len(finished_uuids))
        await self.mark_dead(set(finished_uuids))

    async def cleanup_orphaned(self):
        async with self.async_session() as session:
            result = await session.execute(
                select(Process).where(Process.ended_at.is_(None))
            )
            processes = result.scalars().all()

            for process in processes:
                if process.pid is not None and await self.is_same_process(process):
                    try:
                        await asyncio.to_thread(os.kill, process.pid, 0)
                        continue
                    except (ProcessLookupError, PermissionError):
                        log.info(
                            "cleanup_orphaned: marking uuid=%s (pid=%s) as ended, process is gone",
                            process.uuid,
                            process.pid,
                        )
                        process.ended_at = datetime.now(UTC)
                        process.exit_code = -1
                        await session.commit()

    async def get_stats(self, user: str) -> dict[str, int]:
        return {
            "tracked_processes": len(self._processes),
            "monitoring_tasks": len(self._process_tasks),
            "total_processes": await self.count_processes(user),
            "running_processes": await self.count_processes(user, running_only=True),
        }

    async def close(self):
        if self._process_tasks:
            log.debug(
                "Cancelling %d outstanding monitor task(s)", len(self._process_tasks)
            )
            tasks = list(self._process_tasks.values())
            for task in tasks:
                _ = task.cancel()
            _ = await asyncio.gather(*tasks, return_exceptions=True)

        log.debug("Closing process manager, disposing engine")
        await self.engine.dispose()


@asynccontextmanager
async def process_manager(database_url: str):
    manager = ProcessManager(database_url)
    await manager.initialize()
    try:
        yield manager
    finally:
        await manager.close()

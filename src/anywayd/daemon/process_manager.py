import asyncio
import json
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

from .models import Base, Process, get_uid_gid


@final
class ProcessManager:
    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False)
        self.async_session = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._processes: dict[UUID, psutil.Process] = {}
        self._process_tasks: dict[UUID, asyncio.Task[None]] = {}

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        to_mark_dead: set[UUID] = set()
        async with self.async_session() as session:
            result = await session.execute(select(Process))
            for process in result.scalars().all():
                if self.is_same_process(process):
                    process_ps = psutil.Process(process.pid)
                    self._processes[process.uuid] = process_ps
                    await self._monitor_process(process.uuid)
                else:
                    to_mark_dead.add(process.uuid)

        await self.mark_dead(to_mark_dead)

    async def mark_dead(self, uuid: set[UUID] | UUID):
        if isinstance(uuid, UUID):
            uuid = set((uuid,))
        async with self.async_session() as session:
            _ = await session.execute(
                update(Process)
                .where(Process.uuid.in_(uuid) & (Process.pid.is_not(None)))
                .values(pid=None)
            )
            await session.commit()

    def is_same_process(self, process_db: Process):
        if process_db.pid is not None and psutil.pid_exists(process_db.pid):
            curr_process = psutil.Process(process_db.pid)
            if abs(
                process_db.started_at
                - datetime.fromtimestamp(curr_process.create_time(), tz=UTC)
            ) > timedelta(seconds=1.0):
                return False

            return True
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
        return process.uuid

    async def update_process_pid(self, uuid: UUID, pid: int):
        async with self.async_session() as session:
            result = await session.execute(select(Process).where(Process.uuid == uuid))
            process = result.scalar_one_or_none()
            if process:
                process.pid = pid
                await session.commit()

    async def _log_process_end(self, uuid: UUID, exit_code: int):
        async with self.async_session() as session:
            result = await session.execute(select(Process).where(Process.uuid == uuid))
            process = result.scalar_one_or_none()
            if process:
                process.ended_at = datetime.now(UTC)
                process.exit_code = exit_code
                await session.commit()

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

        process = await asyncio.create_subprocess_exec(
            *dropper_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

        await self.update_process_pid(process_uuid, process.pid)
        self._processes[process_uuid] = psutil.Process(process.pid)

        task = asyncio.create_task(self._monitor_process(process_uuid))
        self._process_tasks[process_uuid] = task

        return process_uuid

    async def _monitor_process(self, uuid: UUID):
        process = self._processes.get(uuid)
        if not process:
            return

        try:
            exit_code = await asyncio.to_thread(process.wait)
            await self._log_process_end(uuid, exit_code)
        except Exception as e:
            print(f"Error monitoring process {uuid}: {e}")
        finally:
            _ = self._processes.pop(uuid, None)
            _ = self._process_tasks.pop(uuid, None)
            await self.mark_dead(uuid)

    async def kill(
        self, uuid: UUID, user: str, signal_num: int = signal.SIGTERM
    ) -> bool:
        process = self._processes.get(uuid)
        if not process:
            return False

        process_db = await self.get_process_by_uuid(uuid, user)
        if process_db is None:
            raise psutil.NoSuchProcess(
                -1,
                msg="Process not found, ensure it's managed by anywayd/started by you.",
            )

        try:
            os.killpg(process.pid, signal_num)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            try:
                os.kill(process.pid, signal_num)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        except Exception as e:
            print(f"Error killing process {uuid}: {e}")
            return False

    async def get_process_by_uuid(self, uuid: UUID, user: str) -> Process | None:
        async with self.async_session() as session:
            process = (
                await session.execute(
                    select(Process).where(
                        (Process.uuid == uuid) & (Process.invoked_by_user == user)
                    )
                )
            ).scalar_one_or_none()
        if process is not None and self.is_same_process(process):
            return process
        return None

    async def get_process_by_pid(self, pid: int, user: str) -> Process | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(Process).where(
                    (Process.pid == pid) & (Process.invoked_by_user == user)
                )
            )
            candidates = result.scalars().all()

        for process in candidates:
            if self.is_same_process(process):
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

    async def cleanup_orphaned(self):
        async with self.async_session() as session:
            result = await session.execute(
                select(Process).where(Process.ended_at.is_(None))
            )
            processes = result.scalars().all()

            for process in processes:
                if process.pid is not None and self.is_same_process(process):
                    try:
                        await asyncio.to_thread(os.kill, process.pid, 0)
                        continue
                    except (ProcessLookupError, PermissionError):
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
        await self.engine.dispose()


@asynccontextmanager
async def process_manager(database_url: str):
    manager = ProcessManager(database_url)
    await manager.initialize()
    try:
        yield manager
    finally:
        await manager.close()

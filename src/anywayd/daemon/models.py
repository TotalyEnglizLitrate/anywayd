import pwd

from datetime import datetime, UTC
from typing import Any, Optional, Self, final, override
from uuid import uuid4, UUID as UUID_py

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime, String, TypeDecorator, UUID


def get_boot_id() -> UUID_py:
    with open("/proc/sys/kernel/random/boot_id") as f:
        return UUID_py(f.read().strip())


def get_uid_gid(name: str) -> tuple[int, int]:
    try:
        user = pwd.getpwnam(name)
        return user.pw_uid, user.pw_gid
    except KeyError:
        raise ValueError(f"No user found with {name=}")


@final
class TZDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    @override
    def process_result_value(
        self,
        value: Optional[Any],  # pyright: ignore[reportDeprecated, reportExplicitAny]
        dialect: Dialect,
    ) -> Optional[datetime]:  # pyright: ignore[reportDeprecated]
        assert isinstance(value, datetime) or value is None
        if value is not None:
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class Process(Base):
    __tablename__: str = "processes"

    uuid: Mapped[UUID_py] = mapped_column(UUID, primary_key=True, default=uuid4)
    pid: Mapped[int | None] = mapped_column(default=None)
    command: Mapped[str] = mapped_column(String(30))
    arguments: Mapped[str] = mapped_column(String(300))
    env: Mapped[str] = mapped_column(String(10000))
    cwd: Mapped[str] = mapped_column(String(100))
    invoked_by_user: Mapped[str] = mapped_column(String(30))
    run_as_user: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(
        TZDateTime, default=lambda: datetime.now(UTC)
    )
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    exit_code: Mapped[int | None] = mapped_column(default=None)
    boot_id: Mapped[UUID_py] = mapped_column(UUID, default=get_boot_id)

    @classmethod
    def from_dict(
        cls,
        uuid: str,
        pid: int,
        command: str,
        arguments: str,
        env: str,
        cwd: str,
        invoked_by_user: str,
        run_as_user: str,
        started_at: float,
        ended_at: float,
        exit_code: int,
        boot_id: str,
    ) -> Self:
        return cls(
            uuid=UUID_py(uuid),
            pid=pid if pid != -1 else None,
            command=command,
            arguments=arguments,
            env=env,
            cwd=cwd,
            invoked_by_user=invoked_by_user,
            run_as_user=run_as_user,
            started_at=datetime.fromtimestamp(started_at, tz=UTC),
            ended_at=(
                datetime.fromtimestamp(ended_at, tz=UTC) if ended_at != -1 else None
            ),
            exit_code=exit_code if exit_code != -1 else None,
            boot_id=UUID_py(boot_id),
        )

import pwd

from datetime import datetime, UTC
from uuid import uuid4, UUID as UUID_py

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime, String, UUID


def get_boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


def get_uid_gid(name: str) -> tuple[int, int]:
    try:
        user = pwd.getpwnam(name)
        return user.pw_uid, user.pw_gid
    except KeyError:
        raise ValueError(f"No user found with {name=}")


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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    exit_code: Mapped[int | None] = mapped_column(default=None)
    boot_id: Mapped[str] = mapped_column(String(36), default=get_boot_id)

import argparse
import os
import sys


class Args(argparse.Namespace):
    uid: int = -1
    gid: int = -1
    ruid: int = -1
    rgid: int = -1
    stdout: str | None = None
    stderr: str | None = None


def parse_args(opts: list[str]) -> Args:
    p = argparse.ArgumentParser()
    _ = p.add_argument("--uid", type=int, required=True)
    _ = p.add_argument("--gid", type=int, required=True)
    _ = p.add_argument("--ruid", type=int, required=True)
    _ = p.add_argument("--rgid", type=int, required=True)
    _ = p.add_argument("--stdout", type=str, default=None)
    _ = p.add_argument("--stderr", type=str, default=None)
    return p.parse_args(opts, namespace=Args())


def open_and_lock_down(path: str, fd_target: int, uid: int, gid: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        _ = os.dup2(fd, fd_target)
    finally:
        os.close(fd)


def main() -> None:
    assert os.geteuid() == 0
    argv = sys.argv[1:]
    sep = argv.index("--")
    opts, command = argv[:sep], argv[sep + 1 :]
    if not command:
        sys.exit(2)

    args = parse_args(opts)
    assert args.uid >= 0 and args.gid >= 0 and args.ruid >= 0 and args.rgid >= 0

    if args.stdout:
        open_and_lock_down(args.stdout, 1, args.ruid, args.rgid)
    if args.stderr:
        open_and_lock_down(args.stderr, 2, args.ruid, args.rgid)

    os.setgroups([args.gid])
    os.setresgid(args.gid, args.gid, args.gid)
    os.setresuid(args.uid, args.uid, args.uid)
    assert os.getuid() == os.geteuid() == args.uid
    assert os.getgid() == os.getegid() == args.gid

    os.execvp(command[0], command)


if __name__ == "__main__":
    main()

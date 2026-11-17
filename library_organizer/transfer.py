"""Explicit hardlink -> Linux reflink -> streamed copy, without replacing targets."""

import errno
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .planner import fingerprint

UNSUPPORTED = {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS, errno.ENOTTY, errno.EINVAL}


def reflink(src, dst):
    if not sys.platform.startswith("linux"):
        raise OSError(errno.EOPNOTSUPP, "Linux FICLONE unavailable")
    import fcntl

    with open(src, "rb") as source, open(dst, "xb") as target:
        fcntl.ioctl(target.fileno(), 0x40049409, source.fileno())  # FICLONE


def materialize(src, stage):
    try:
        os.link(src, stage, follow_symlinks=False)
        return "hardlink"
    except OSError as e:
        if e.errno not in {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS}:
            raise
    try:
        reflink(src, stage)
        shutil.copystat(src, stage, follow_symlinks=False)
        return "reflink"
    except OSError as e:
        if e.errno not in UNSUPPORTED:
            raise
        stage.unlink(missing_ok=True)
    # Explicit streaming avoids copy2/copy_file_range silently cloning while we
    # report "copy". ENOSPC, EIO and permission failures must never be downgraded.
    with open(src, "rb") as source, open(stage, "xb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    shutil.copystat(src, stage, follow_symlinks=False)
    return "copy"


def publish(src, dst, source_fingerprint, claim):
    # Keep a private staging directory on the destination filesystem. All three
    # methods finish before an atomic, no-clobber link publishes the final name.
    with tempfile.TemporaryDirectory(prefix=".ab-organizer-", dir=dst.parent) as folder:
        stage = Path(folder) / "file"
        method = materialize(src, stage)
        target_fingerprint = fingerprint(stage)
        if fingerprint(src) != source_fingerprint:
            raise ValueError("源文件在整理期间变化，请重新预览")
        if stage.stat().st_size != src.stat().st_size:
            raise ValueError("目标文件大小校验失败")
        claim(target_fingerprint, method)  # durable intent before publishing
        os.link(stage, dst, follow_symlinks=False)
        if fingerprint(dst) != target_fingerprint:
            raise ValueError("目标文件校验失败")
        return method

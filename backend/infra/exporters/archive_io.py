"""ZIP 归档和备份共用的底层安全写入器。"""

import hashlib
import zipfile
from pathlib import Path


def add_archive_dir(archive: zipfile.ZipFile, path: str, reserved: set[str]) -> None:
    """写入明确目录条目，保留空问题及空文件夹证据的归档结构。"""
    normalized = path.strip("/")
    if normalized and normalized not in reserved:
        archive.writestr(f"{normalized}/", "")
        reserved.add(normalized)


def reserve_archive_path(parent: str, safe_name: str, reserved: set[str]) -> str:
    """为已净化的文件名分配 ZIP 内唯一相对路径。"""
    candidate = f"{parent.rstrip('/')}/{safe_name}"
    if candidate not in reserved:
        reserved.add(candidate)
        return candidate
    suffix = Path(safe_name).suffix
    stem = safe_name[:-len(suffix)] if suffix else safe_name
    for index in range(1, 10_000):
        candidate = f"{parent.rstrip('/')}/{stem}_{index}{suffix}"
        if candidate not in reserved:
            reserved.add(candidate)
            return candidate
    raise RuntimeError(f"无法生成唯一归档路径：{safe_name}")


def write_streamed_archive_file(archive: zipfile.ZipFile, source: Path, archive_path: str) -> tuple[int, str]:
    """单次读取源文件，写入 ZIP 的同时返回字节数与 SHA-256。"""
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as src, archive.open(archive_path, "w", force_zip64=True) as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()

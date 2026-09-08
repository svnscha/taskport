"""Portable, immutable task ZIP packages. No extraction on the server."""

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

from taskport.protocol import Manifest

MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_FILES = 2048
BAD_COMPONENT = re.compile(r'[<>:"\\|?*\x00-\x1f]')
DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


def safe_name(name: str) -> str:
    """Accept only relative paths that work on both Windows and POSIX."""
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or len(name) > 500
        or name != path.as_posix()
        or path.is_absolute()
        or any(
            part in {".", ".."}
            or BAD_COMPONENT.search(part)
            or DEVICE.match(part)
            or part.endswith((" ", "."))
            for part in path.parts
        )
    ):
        raise ValueError(f"Unsafe or non-portable relative filename: {name!r}")
    return name


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_package(path: Path) -> Manifest:
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("Task package exceeds 64 MiB")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_PACKAGE_FILES:
            raise ValueError("Too many files in task package")
        seen = set()
        files = set()
        total = 0
        for info in entries:
            if info.orig_filename != info.filename:
                raise ValueError("Package contains a normalized or truncated filename")
            name = safe_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
            key = name.casefold()
            if key in seen:
                raise ValueError(f"Duplicate package path: {name}")
            seen.add(key)
            if not info.is_dir():
                files.add(key)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Task packages cannot contain symbolic links")
            if info.flag_bits & 1:
                raise ValueError("Encrypted task packages are not supported")
            total += info.file_size
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("Expanded task package exceeds 64 MiB")
        for name in seen:
            if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
                raise ValueError("Package path is both a file and a directory")
        # Check CRCs, rather than accepting a manifest alongside corrupt scripts.
        if archive.testzip() is not None:
            raise ValueError("Task package checksum is invalid")
        try:
            raw = archive.read("task.json")
        except KeyError as exc:
            raise ValueError("Package must contain task.json at its root") from exc
        if len(raw) > 256 * 1024:
            raise ValueError("Task manifest exceeds 256 KiB")
        return Manifest.model_validate(json.loads(raw))


def pack_directory(directory: Path, destination: Path) -> Manifest:
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("A task package must be a directory")
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    count = total = 0
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if ignored.intersection(relative.parts):
                continue
            if is_link(path):
                raise ValueError(f"Task packages cannot include links: {relative}")
            if not path.is_file():
                continue
            name = safe_name(relative.as_posix())
            total += path.stat().st_size
            count += 1
            if total > MAX_PACKAGE_BYTES or count > MAX_PACKAGE_FILES:
                raise ValueError("Task package is too large")
            # Fixed ZIP metadata gives identical package bytes on each publication.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes())
    return inspect_package(destination)


def extract_package(archive_path: Path, destination: Path) -> Manifest:
    manifest = inspect_package(archive_path)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            target = root.joinpath(*PurePosixPath(safe_name(name)).parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError("Package path escapes its destination")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    while block := source.read(1024 * 1024):
                        output.write(block)
    return manifest

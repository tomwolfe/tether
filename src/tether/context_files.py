"""Bounded context files for mission contracts.

A mission may declare top-level ``context_files`` (a list of relative
paths). At mission start Tether reads each file and embeds its content into
the prompt context, delimited with headers naming the file. All hard limits
live here so they are documented in exactly one place:

- at most ``CONTEXT_FILES_MAX_COUNT`` files,
- at most ``CONTEXT_FILES_MAX_FILE_BYTES`` per file,
- at most ``CONTEXT_FILES_TOTAL_MAX_BYTES`` across all files,
- binary content is refused: any NUL byte within the first
  ``BINARY_SNIFF_BYTES`` bytes marks a file as binary.

Path rules: paths must be relative and must not escape the project
directory — no absolute paths and no ``..`` components after normalization.
Every violation (path policy, missing file, size, binary) fails the mission
before execution; all violations are collected and reported together.

Existence/size/binary checks run against the *target* project at run time;
mission-file validation only checks structure (list of strings).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Hard limits (documented here by design; do not duplicate elsewhere).
CONTEXT_FILES_MAX_COUNT = 32                 # max number of context files
CONTEXT_FILES_MAX_FILE_BYTES = 256 * 1024    # max size per file (256 KiB)
CONTEXT_FILES_TOTAL_MAX_BYTES = 512 * 1024   # max total context (512 KiB)
BINARY_SNIFF_BYTES = 8192                    # NUL byte within this prefix => binary


class ContextFilesError(ValueError):
    """Raised when declared context_files violate path/size/binary policy."""


@dataclass(frozen=True)
class ContextFile:
    relpath: str        # normalized relative path inside the project dir
    content: str        # decoded text content (post-load, pre-redaction)
    size_bytes: int     # raw byte size on disk


def _normalize_relpath(raw: str) -> str:
    """Validate one declared path; return its normalized relative form."""
    candidate = Path(raw)
    # The startswith check also rejects POSIX-style absolute paths where
    # Path.is_absolute() alone would not (e.g. on Windows).
    if candidate.is_absolute() or raw.replace("\\", "/").startswith("/"):
        raise ContextFilesError(
            f"context_files entry must be relative, got absolute path: {raw!r}"
        )
    normalized = Path(os.path.normpath(candidate))
    parts = normalized.as_posix().split("/")
    if any(part == ".." for part in parts):
        raise ContextFilesError(
            f"context_files entry escapes the project directory "
            f"(contains '..' after normalization): {raw!r}"
        )
    return normalized.as_posix()


def load_context_files(project_dir: Path,
                       relpaths: list[str]) -> list[ContextFile]:
    """Read and validate every declared context file against ``project_dir``.

    Collects all violations and raises :class:`ContextFilesError` listing
    each explicit reason; returns the loaded files in declaration order on
    success.
    """
    errors: list[str] = []
    if len(relpaths) > CONTEXT_FILES_MAX_COUNT:
        errors.append(
            f"context_files declares {len(relpaths)} entries; the limit is "
            f"{CONTEXT_FILES_MAX_COUNT} files"
        )

    loaded: list[ContextFile] = []
    total = 0
    for raw in relpaths:
        try:
            relpath = _normalize_relpath(raw)
        except ContextFilesError as e:
            errors.append(str(e))
            continue
        target = project_dir / relpath
        if not target.exists():
            errors.append(f"context file not found: {relpath}")
            continue
        if not target.is_file():
            errors.append(f"context file is not a regular file: {relpath}")
            continue
        try:
            data = target.read_bytes()
        except OSError as e:
            errors.append(f"context file cannot be read: {relpath}: {e}")
            continue
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            errors.append(
                f"context file looks binary (NUL byte within first "
                f"{BINARY_SNIFF_BYTES} bytes): {relpath}"
            )
            continue
        size = len(data)
        if size > CONTEXT_FILES_MAX_FILE_BYTES:
            errors.append(
                f"context file {relpath} is {size} bytes; the per-file limit "
                f"is {CONTEXT_FILES_MAX_FILE_BYTES} bytes"
            )
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(
                f"context file is not valid UTF-8 text: {relpath}"
            )
            continue
        total += size
        loaded.append(ContextFile(relpath=relpath, content=text, size_bytes=size))

    if total > CONTEXT_FILES_TOTAL_MAX_BYTES:
        errors.append(
            f"context files total {total} bytes; the limit is "
            f"{CONTEXT_FILES_TOTAL_MAX_BYTES} bytes of context"
        )
    if errors:
        raise ContextFilesError("invalid context_files:\n" + "\n".join(
            f"- {e}" for e in errors))
    return loaded


def render_context_block(files: list[ContextFile]) -> str:
    """Render loaded context files as one prompt block.

    Each file gets a header naming it plus clear begin/end delimiters so the
    agent can tell embedded file content apart from the mission summary.
    Returns an empty string when no files are given.
    """
    lines: list[str] = ["# Reference context files"]
    for f in files:
        lines.append(f"## Context file: {f.relpath} ({f.size_bytes} bytes)")
        lines.append(f"<<<BEGIN {f.relpath}>>>")
        lines.append(f.content.rstrip("\n"))
        lines.append(f"<<<END {f.relpath}>>>")
    return "\n".join(lines)

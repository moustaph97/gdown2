"""Filesystem path sanitization, collision resolution, and traversal guards.

Google Drive allows arbitrary characters in filenames (slashes, colons, trailing
dots, duplicate names in the same folder, or names like ``CON`` that break on
Windows). This module normalizes remote names into safe local paths before any
worker touches the disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from gdown2.domain.errors import PathTraversalError

# Characters forbidden on Windows (and '/' + NUL on POSIX).
_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Reserved DOS/Windows device names (case-insensitive, with or without extension).
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


@dataclass(slots=True)
class PathPolicy:
    """Rules for converting remote Drive item names into safe local paths."""

    max_segment_length: int = 200
    max_total_path_length: int = 240
    replacement_char: str = "_"
    _seen_paths: dict[PurePosixPath, set[str]] = field(default_factory=dict, repr=False)

    def sanitize_segment(self, raw_name: str, fallback: str = "unnamed") -> str:
        """Sanitize a single path component (file or directory name)."""
        cleaned = _FORBIDDEN_CHARS_RE.sub(self.replacement_char, raw_name)
        # Strip leading/trailing whitespace and trailing dots/spaces (Windows quirk).
        cleaned = cleaned.strip().rstrip(". ")

        if not cleaned or cleaned in {".", ".."}:
            cleaned = fallback

        stem, dot, ext = self._split_extension(cleaned)
        first_token = cleaned.partition(".")[0]
        if first_token.upper() in _WINDOWS_RESERVED_NAMES:
            cleaned = f"_{cleaned}"
            stem, dot, ext = self._split_extension(cleaned)

        if len(cleaned) > self.max_segment_length:
            suffix = f"{dot}{ext}"
            budget = max(1, self.max_segment_length - len(suffix))
            cleaned = f"{stem[:budget].rstrip('. ')}{suffix}"

        return cleaned or fallback

    def allocate_child_path(
        self,
        parent: PurePosixPath,
        raw_name: str,
        *,
        forced_extension: str | None = None,
    ) -> PurePosixPath:
        """Sanitize ``raw_name`` and resolve collisions among siblings under ``parent``."""
        base_name = self.sanitize_segment(raw_name)
        if forced_extension:
            ext_norm = (
                forced_extension if forced_extension.startswith(".") else f".{forced_extension}"
            )
            if not base_name.lower().endswith(ext_norm.lower()):
                base_name = self.sanitize_segment(f"{base_name}{ext_norm}")

        used = self._seen_paths.setdefault(parent, set())
        candidate = base_name
        counter = 1
        stem, dot, ext = self._split_extension(base_name)
        suffix = f"{dot}{ext}"

        while candidate.lower() in used:
            tag = f" ({counter})"
            budget = max(1, self.max_segment_length - len(suffix) - len(tag))
            candidate = f"{stem[:budget]}{tag}{suffix}"
            counter += 1

        used.add(candidate.lower())
        return parent / candidate

    def resolve_local_path(self, root_dir: Path, relative_path: PurePosixPath) -> Path:
        """Join ``root_dir`` and ``relative_path``, verifying no directory traversal."""
        for part in relative_path.parts:
            if part in {"..", ""} or "/" in part or "\\" in part:
                raise PathTraversalError(
                    f"Unsafe relative path component {part!r} in {relative_path}"
                )

        resolved_root = root_dir.resolve()
        target = (resolved_root / Path(*relative_path.parts)).resolve()

        try:
            target.relative_to(resolved_root)
        except ValueError as exc:
            raise PathTraversalError(
                f"Resolved path {target} escapes destination root {resolved_root}"
            ) from exc

        # Enforce total path length budget, reserving room for the ".part" suffix.
        target_str = os.fspath(target)
        part_suffix = ".part"
        if (
            len(target_str) + len(part_suffix) > self.max_total_path_length
            and len(relative_path.parts) > 0
        ):
            overrun = len(target_str) + len(part_suffix) - self.max_total_path_length
            leaf = target.name
            stem, dot, ext = self._split_extension(leaf)
            if len(stem) > overrun + 4:
                short_leaf = f"{stem[: len(stem) - overrun]}{dot}{ext}"
                target = target.with_name(short_leaf)

        return target

    @staticmethod
    def _split_extension(name: str) -> tuple[str, str, str]:
        """Split ``name`` into ``(stem, dot, ext)`` preserving leading dotfiles."""
        if name.startswith(".") and "." not in name[1:]:
            return (name, "", "")
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            return (name, "", "")
        return (stem, dot, ext)

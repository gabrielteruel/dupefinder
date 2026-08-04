"""Path validation, the actual move/trash operations, and the JSON audit report."""

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime

from dupefinder.hashing import HashCache
from dupefinder.models import NoisyDir


@dataclass
class MoveResult:
    moved: list[dict] = field(default_factory=list)  # {rel_path, dest_path, size}
    skipped_identical: list[dict] = field(default_factory=list)  # {rel_path, dest_path}
    renamed: list[dict] = field(default_factory=list)  # {rel_path, dest_path, original_name}
    trashed: list[dict] = field(default_factory=list)  # {rel_path, dest_path}
    errors: list[dict] = field(default_factory=list)  # {path, error}


def validate_sources(a: str, b: str) -> None:
    """Validate folder A and folder B alone, without a destination.

    Used by the pre-scan endpoint, which runs before a destination is chosen.
    Raises ValueError with a user-facing English message on any violation.
    """
    if not os.path.isdir(a):
        raise ValueError(f"folder A does not exist or is not a directory: {a}")
    if not os.path.isdir(b):
        raise ValueError(f"folder B does not exist or is not a directory: {b}")

    real_a = os.path.realpath(a)
    real_b = os.path.realpath(b)

    if real_a == real_b:
        raise ValueError("folder A and folder B must be different")
    if _is_inside(real_a, real_b):
        raise ValueError("folder A cannot be inside folder B")
    if _is_inside(real_b, real_a):
        raise ValueError("folder B cannot be inside folder A")


def validate_paths(a: str, b: str, dest: str) -> None:
    """Validate that A, B and the destination are a safe combination.

    Raises ValueError with a user-facing English message on any violation.
    `dest` is allowed not to exist yet -- the caller creates it.
    """
    validate_sources(a, b)

    real_a = os.path.realpath(a)
    real_b = os.path.realpath(b)
    real_dest = os.path.realpath(dest)

    if real_dest == real_a or real_dest == real_b:
        raise ValueError("the destination folder must be different from folder A and folder B")
    if _is_inside(real_dest, real_a):
        raise ValueError("the destination folder cannot be inside folder A")
    if _is_inside(real_dest, real_b):
        raise ValueError("the destination folder cannot be inside folder B")


def _is_inside(child: str, parent: str) -> bool:
    if child == parent:
        return False
    return os.path.commonpath([child, parent]) == parent


def apply_moves(entries: list[tuple[str, str]], dest: str, cache: HashCache) -> MoveResult:
    """Move the selected (abs_path, rel_path) entries into `dest`.

    Preserves the relative directory structure. On a name collision at the
    destination: identical content is skipped (source left untouched),
    different content is moved under a numeric suffix. Any OSError is
    captured into `errors` and the loop continues.
    """
    result = MoveResult()

    for abs_path, rel_path in entries:
        try:
            target = os.path.join(dest, *rel_path.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)

            if not os.path.exists(target):
                size = os.stat(abs_path).st_size
                shutil.move(abs_path, target)
                result.moved.append({"rel_path": rel_path, "dest_path": target, "size": size})
            elif cache.full(target) == cache.full(abs_path):
                result.skipped_identical.append({"rel_path": rel_path, "dest_path": target})
            else:
                free_target = _first_free_name(target)
                shutil.move(abs_path, free_target)
                result.renamed.append(
                    {
                        "rel_path": rel_path,
                        "dest_path": free_target,
                        "original_name": os.path.basename(target),
                    }
                )
        except OSError as exc:
            result.errors.append({"path": abs_path, "error": str(exc)})

    return result


def _first_free_name(target: str) -> str:
    stem, suffix = os.path.splitext(target)
    i = 1
    while True:
        candidate = f"{stem}_{i}{suffix}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def move_to_trash(noisy_dirs: list[NoisyDir], dest: str) -> list[dict]:
    """Move the given (A-only) noisy directories under `<dest>/_trash/`.

    Passing a directory whose root is "B" is a programming error, since
    folder B must never be modified.
    """
    trashed: list[dict] = []
    for noisy_dir in noisy_dirs:
        if noisy_dir.root != "A":
            raise ValueError(
                f"refusing to trash a directory belonging to folder B: {noisy_dir.abs_path}"
            )
        target = os.path.join(dest, "_trash", *noisy_dir.rel_path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(noisy_dir.abs_path, target)
        trashed.append({"rel_path": noisy_dir.rel_path, "dest_path": target})
    return trashed


def write_report(dest: str, payload: dict) -> str:
    """Write the audit report as `<dest>/_report_<timestamp>.json` and return its path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = os.path.join(dest, f"_report_{timestamp}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return report_path

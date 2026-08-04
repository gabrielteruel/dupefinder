"""Shared test fixtures: build small file trees inside a temp directory."""

import os
import tempfile
import unittest


def build_tree(root: str, spec: dict[str, bytes]) -> None:
    """Materialise nested files from a dict of {"rel/path": b"content"}."""
    for rel_path, content in spec.items():
        full_path = os.path.join(root, *rel_path.split("/"))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)


class TempTreeCase(unittest.TestCase):
    """Base test case that provides a fresh temporary directory per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

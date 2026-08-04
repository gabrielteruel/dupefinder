"""Tests for dupefinder.hashing."""

import hashlib
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from dupefinder.hashing import CHUNK_SIZE, HashCache, ReadError, full_hash, partial_hash
from tests.helpers import TempTreeCase, build_tree


class FullHashTests(TempTreeCase):
    def test_matches_known_sha256_vector(self) -> None:
        build_tree(self.root, {"greeting.txt": b"hello world"})
        path = os.path.join(self.root, "greeting.txt")
        expected = hashlib.sha256(b"hello world").hexdigest()
        self.assertEqual(full_hash(path), expected)

    def test_hashes_files_larger_than_chunk_size_correctly(self) -> None:
        content = os.urandom(CHUNK_SIZE + 12345)
        build_tree(self.root, {"big.bin": content})
        path = os.path.join(self.root, "big.bin")
        expected = hashlib.sha256(content).hexdigest()
        self.assertEqual(full_hash(path), expected)


class PartialHashTests(TempTreeCase):
    def test_equal_prefix_yields_equal_partial_hash_even_if_tail_differs(self) -> None:
        prefix = os.urandom(65536)
        build_tree(
            self.root,
            {
                "a.bin": prefix + b"AAAA",
                "b.bin": prefix + b"BBBB",
            },
        )
        hash_a = partial_hash(os.path.join(self.root, "a.bin"))
        hash_b = partial_hash(os.path.join(self.root, "b.bin"))
        self.assertEqual(hash_a, hash_b)


class HashCacheTests(TempTreeCase):
    def test_computes_digest_once_and_serves_repeats_from_cache(self) -> None:
        build_tree(self.root, {"file.txt": b"content"})
        path = os.path.join(self.root, "file.txt")
        cache = HashCache()

        first = cache.full(path)
        second = cache.full(path)

        self.assertEqual(first, second)
        self.assertEqual(cache.full_calls, 1)


class ConcurrentAccessTests(TempTreeCase):
    def test_thread_pool_over_distinct_files_has_no_lost_counter_updates(self) -> None:
        spec = {f"file{i}.bin": os.urandom(1024) for i in range(20)}
        build_tree(self.root, spec)
        cache = HashCache()
        paths = [os.path.join(self.root, name) for name in spec]

        with ThreadPoolExecutor(max_workers=8) as pool:
            digests = list(pool.map(cache.full, paths))

        for path, digest in zip(paths, digests):
            with open(path, "rb") as f:
                expected = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(digest, expected)
        self.assertEqual(cache.full_calls, len(paths))


class ReadErrorTests(TempTreeCase):
    def test_unreadable_file_raises_read_error_with_path(self) -> None:
        # Opening a directory as a file always fails with OSError, regardless
        # of the user's privilege level (unlike a permission-bits test).
        directory_path = os.path.join(self.root, "not_a_file")
        os.makedirs(directory_path)

        with self.assertRaises(ReadError) as ctx:
            full_hash(directory_path)

        self.assertEqual(ctx.exception.path, directory_path)


if __name__ == "__main__":
    unittest.main()

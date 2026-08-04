"""Tests for dupefinder.hashing."""

import hashlib
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from dupefinder.hashing import (
    CHUNK_SIZE,
    SAMPLE_SIZE,
    HashCache,
    PersistentHashCache,
    ReadError,
    full_hash,
    partial_hash,
    sampled_hash,
)
from dupefinder.store import HashRow, Store
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


class SampledHashTests(TempTreeCase):
    def test_same_prefix_same_size_different_tail_yields_different_sampled_hash(self) -> None:
        # This is the case a prefix-only filter cannot see: identical first
        # 64 KiB, identical size, different content later in the file.
        prefix = os.urandom(65536)
        build_tree(self.root, {"a.bin": prefix + b"A" * 4096, "b.bin": prefix + b"B" * 4096})
        path_a = os.path.join(self.root, "a.bin")
        path_b = os.path.join(self.root, "b.bin")
        size = os.stat(path_a).st_size

        self.assertEqual(partial_hash(path_a), partial_hash(path_b))  # prefix filter is blind
        self.assertNotEqual(sampled_hash(path_a, size), sampled_hash(path_b, size))

    def test_byte_identical_files_yield_the_same_sampled_hash(self) -> None:
        content = os.urandom(65536) + b"tail"
        build_tree(self.root, {"a.bin": content, "b.bin": content})
        path_a = os.path.join(self.root, "a.bin")
        path_b = os.path.join(self.root, "b.bin")
        size = os.stat(path_a).st_size

        self.assertEqual(sampled_hash(path_a, size), sampled_hash(path_b, size))

    def test_reads_far_less_than_the_whole_file(self) -> None:
        build_tree(self.root, {"big.bin": os.urandom(1024 * 1024)})
        path = os.path.join(self.root, "big.bin")
        size = os.stat(path).st_size
        cache = HashCache()

        cache.sampled(path)

        self.assertEqual(cache.sampled_calls, 1)
        self.assertLessEqual(cache.bytes_read, SAMPLE_SIZE * 2)

    def test_unreadable_file_raises_read_error_with_path(self) -> None:
        directory_path = os.path.join(self.root, "not_a_file")
        os.makedirs(directory_path)

        with self.assertRaises(ReadError) as ctx:
            sampled_hash(directory_path, 100)

        self.assertEqual(ctx.exception.path, directory_path)


class PersistentHashCacheTests(TempTreeCase):
    def test_serves_a_stored_digest_without_recomputing(self) -> None:
        build_tree(self.root, {"file.bin": b"content"})
        path = os.path.join(self.root, "file.bin")
        st = os.stat(path)

        db_path = os.path.join(self.root, "hashes.db")
        store = Store(db_path)
        store.put_hash(
            HashRow(
                path=path, size=st.st_size, mtime_ns=st.st_mtime_ns, partial=None, full="precomputed"
            )
        )
        store.flush()

        cache = PersistentHashCache(store)
        with mock.patch(
            "dupefinder.hashing.full_hash", side_effect=AssertionError("must not read")
        ):
            digest = cache.full(path)

        self.assertEqual(digest, "precomputed")
        self.assertEqual(cache.cache_hits, 1)
        self.assertEqual(cache.cache_misses, 0)
        # A store hit must not count as a computed hash -- comparer.py feeds
        # full_calls straight into the report's "N full hashes" stat, and a
        # store hit did no hashing this run.
        self.assertEqual(cache.full_calls, 0)
        cache.close()

    def test_a_miss_computes_and_persists_for_the_next_run(self) -> None:
        build_tree(self.root, {"file.bin": b"content"})
        path = os.path.join(self.root, "file.bin")
        db_path = os.path.join(self.root, "hashes.db")

        store = Store(db_path)
        cache = PersistentHashCache(store)
        digest = cache.full(path)
        self.assertEqual(cache.cache_misses, 1)
        # A miss actually computed a digest this run -- full_calls must count it.
        self.assertEqual(cache.full_calls, 1)
        cache.close()  # flush -- the resumability guarantee

        reopened_store = Store(db_path)
        reopened_cache = PersistentHashCache(reopened_store)
        with mock.patch(
            "dupefinder.hashing.full_hash", side_effect=AssertionError("must not read")
        ):
            second = reopened_cache.full(path)
        self.assertEqual(second, digest)
        self.assertEqual(reopened_cache.cache_hits, 1)
        # The hit on the reopened cache must not be counted as a computed hash.
        self.assertEqual(reopened_cache.full_calls, 0)
        reopened_cache.close()

    def test_sampled_cache_miss_preserves_a_previously_persisted_full_digest(self) -> None:
        # Regression: PersistentHashCache.sampled() must pass through the
        # sibling `partial`/`full` digests when writing its row on a cache
        # miss, or computing .sampled() after .full() silently overwrites
        # (destroys) the full digest already persisted for this file version.
        build_tree(self.root, {"file.bin": b"content"})
        path = os.path.join(self.root, "file.bin")
        db_path = os.path.join(self.root, "hashes.db")

        store = Store(db_path)
        cache = PersistentHashCache(store)
        full_digest = cache.full(path)
        cache.close()  # flush -- the full digest now lives only in the DB

        reopened_store = Store(db_path)
        reopened_cache = PersistentHashCache(reopened_store)
        sampled_digest = reopened_cache.sampled(path)
        reopened_cache.close()  # flush -- the sampled write must not clobber `full`

        final_store = Store(db_path)
        st = os.stat(path)
        row = final_store.get_hash(path, st.st_size, st.st_mtime_ns)
        self.assertIsNotNone(row)
        self.assertEqual(row.full, full_digest)
        self.assertEqual(row.sampled, sampled_digest)
        final_store.close()

    def test_full_cache_miss_preserves_a_previously_persisted_sampled_digest(self) -> None:
        # Mirror of the above, exercising the reverse order: .sampled() first,
        # then .full() -- the full write must not drop the sampled digest.
        build_tree(self.root, {"file.bin": b"content"})
        path = os.path.join(self.root, "file.bin")
        db_path = os.path.join(self.root, "hashes.db")

        store = Store(db_path)
        cache = PersistentHashCache(store)
        sampled_digest = cache.sampled(path)
        cache.close()  # flush -- the sampled digest now lives only in the DB

        reopened_store = Store(db_path)
        reopened_cache = PersistentHashCache(reopened_store)
        full_digest = reopened_cache.full(path)
        reopened_cache.close()  # flush -- the full write must not clobber `sampled`

        final_store = Store(db_path)
        st = os.stat(path)
        row = final_store.get_hash(path, st.st_size, st.st_mtime_ns)
        self.assertIsNotNone(row)
        self.assertEqual(row.sampled, sampled_digest)
        self.assertEqual(row.full, full_digest)
        final_store.close()

    def test_sampled_write_passes_through_the_sibling_partial_and_full_digests(self) -> None:
        # Store.put_hash()/flush() already merge sibling digest fields for a
        # matching (size, mtime_ns) version (see SiblingDigestMergeTests in
        # test_store.py), so an end-to-end round trip cannot tell a call site
        # that drops sibling fields apart from one that passes them through
        # -- the Store-level merge quietly repairs the omission either way.
        # This test instead pins the call site's own responsibility directly:
        # the HashRow PersistentHashCache.sampled() hands to put_hash() must
        # itself already carry the previously stored partial/full digests, so
        # correctness never depends solely on that downstream repair.
        build_tree(self.root, {"file.bin": b"content"})
        path = os.path.join(self.root, "file.bin")
        st = os.stat(path)
        db_path = os.path.join(self.root, "hashes.db")

        store = Store(db_path)
        store.put_hash(
            HashRow(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns, partial="p1", full="f1")
        )
        store.flush()

        cache = PersistentHashCache(store)
        with mock.patch.object(store, "put_hash", wraps=store.put_hash) as spy:
            digest = cache.sampled(path)
        cache.close()

        self.assertEqual(spy.call_count, 1)
        written_row = spy.call_args[0][0]
        self.assertEqual(written_row.partial, "p1")
        self.assertEqual(written_row.sampled, digest)
        self.assertEqual(written_row.full, "f1")


if __name__ == "__main__":
    unittest.main()

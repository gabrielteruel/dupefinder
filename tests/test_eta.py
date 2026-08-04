"""Tests for dupefinder.eta. Pure arithmetic -- timestamps injected, no sleeping."""

import unittest

from dupefinder.eta import EtaEstimator, format_duration


class EtaEstimatorTests(unittest.TestCase):
    def test_steady_throughput_yields_expected_remaining_time(self) -> None:
        estimator = EtaEstimator(window_seconds=30.0)
        estimator.observe(0.0, 0)
        estimator.observe(10.0, 100_000_000)  # 10 MB/s over 10 s

        remaining = estimator.seconds_remaining(10.0, bytes_total=200_000_000)

        self.assertIsNotNone(remaining)
        self.assertAlmostEqual(remaining, 10.0, delta=0.5)

    def test_returns_none_with_under_five_seconds_of_history(self) -> None:
        estimator = EtaEstimator()
        estimator.observe(0.0, 0)
        estimator.observe(3.0, 30_000_000)

        self.assertIsNone(estimator.seconds_remaining(3.0, bytes_total=100_000_000))

    def test_returns_none_when_throughput_is_zero(self) -> None:
        estimator = EtaEstimator()
        estimator.observe(0.0, 1_000)
        estimator.observe(10.0, 1_000)  # no progress at all

        self.assertIsNone(estimator.seconds_remaining(10.0, bytes_total=100_000_000))

    def test_rolling_window_reflects_recent_slow_rate_not_the_startup_burst(self) -> None:
        # Simulates a resumed scan: a fast burst of cache hits, then a
        # sustained slow phase reading real disk. A cumulative average would
        # stay anchored to the fast early phase and badly under-predict.
        estimator = EtaEstimator(window_seconds=20.0)
        estimator.observe(0.0, 0)
        estimator.observe(1.0, 500_000_000)  # burst: 500 MB in 1 s (cache hits)
        for t in range(2, 32):
            estimator.observe(float(t), 500_000_000 + (t - 1) * 1_000_000)  # 1 MB/s after

        remaining = estimator.seconds_remaining(31.0, bytes_total=600_000_000)

        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 50.0)  # a cumulative average would predict far less

    def test_samples_older_than_the_window_are_discarded(self) -> None:
        estimator = EtaEstimator(window_seconds=10.0)
        estimator.observe(0.0, 0)     # falls outside the window well before t=100
        estimator.observe(90.0, 0)
        estimator.observe(100.0, 100_000_000)

        remaining = estimator.seconds_remaining(100.0, bytes_total=200_000_000)

        fresh = EtaEstimator(window_seconds=10.0)
        fresh.observe(90.0, 0)
        fresh.observe(100.0, 100_000_000)
        expected = fresh.seconds_remaining(100.0, bytes_total=200_000_000)

        self.assertAlmostEqual(remaining, expected, delta=0.01)

    def test_throughput_bps_matches_a_manual_calculation(self) -> None:
        estimator = EtaEstimator(window_seconds=30.0)
        estimator.observe(0.0, 0)
        estimator.observe(10.0, 50_000_000)

        self.assertAlmostEqual(estimator.throughput_bps(10.0), 5_000_000.0, delta=1.0)


class FormatDurationTests(unittest.TestCase):
    def test_under_a_minute(self) -> None:
        self.assertEqual(format_duration(45), "less than a minute")

    def test_a_few_minutes(self) -> None:
        self.assertEqual(format_duration(250), "about 4 minutes")

    def test_hours_and_minutes(self) -> None:
        self.assertEqual(format_duration(4800), "about 1 h 20 min")


if __name__ == "__main__":
    unittest.main()

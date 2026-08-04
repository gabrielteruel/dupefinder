"""Tests for dupefinder.diskinfo. All hardware/subprocess access is mocked."""

import unittest
from unittest import mock

from dupefinder.diskinfo import VolumeInfo, combine, detect


class WslDetectionTests(unittest.TestCase):
    def test_detects_wsl_from_proc_version(self) -> None:
        fake_version = "Linux version 5.15.153.1-microsoft-standard-WSL2"
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("builtins.open", mock.mock_open(read_data=fake_version)),
            mock.patch(
                "dupefinder.diskinfo._query_windows_physical_disk",
                return_value=("hdd", "usb"),
            ),
        ):
            info = detect("/mnt/d/photos")
        self.assertEqual(info.kind, "hdd")
        self.assertEqual(info.suggested_workers, 1)


class WorkerSuggestionTests(unittest.TestCase):
    def test_hdd_under_mnt_suggests_one_worker(self) -> None:
        with (
            mock.patch("dupefinder.diskinfo.is_wsl", return_value=True),
            mock.patch(
                "dupefinder.diskinfo._query_windows_physical_disk",
                return_value=("hdd", "usb"),
            ),
        ):
            info = detect("/mnt/d/photos")
        self.assertEqual(info.kind, "hdd")
        self.assertEqual(info.suggested_workers, 1)

    def test_ssd_under_mnt_suggests_eight_workers(self) -> None:
        with (
            mock.patch("dupefinder.diskinfo.is_wsl", return_value=True),
            mock.patch(
                "dupefinder.diskinfo._query_windows_physical_disk",
                return_value=("ssd", "nvme"),
            ),
        ):
            info = detect("/mnt/c/users")
        self.assertEqual(info.kind, "ssd")
        self.assertEqual(info.suggested_workers, 8)


class FailureModeTests(unittest.TestCase):
    def test_powershell_returning_nothing_yields_unknown_and_one_worker(self) -> None:
        with (
            mock.patch("dupefinder.diskinfo.is_wsl", return_value=True),
            mock.patch("dupefinder.diskinfo._query_windows_physical_disk", return_value=None),
        ):
            info = detect("/mnt/d/photos")
        self.assertEqual(info.kind, "unknown")
        self.assertEqual(info.suggested_workers, 1)

    def test_powershell_unavailable_yields_unknown(self) -> None:
        with (
            mock.patch("dupefinder.diskinfo.is_wsl", return_value=True),
            mock.patch("subprocess.run", side_effect=OSError("powershell.exe not found")),
        ):
            info = detect("/mnt/d/photos")
        self.assertEqual(info.kind, "unknown")
        self.assertEqual(info.suggested_workers, 1)


class RotationalNotConsultedOnWslTests(unittest.TestCase):
    def test_rotational_file_is_never_read_when_wsl_is_detected(self) -> None:
        # Pins the corrected finding: WSL's virtual disks all report
        # rotational=1 even when backed by NVMe, so it must never be
        # consulted under WSL -- it would recommend the worst setting.
        def _fail_on_rotational(path, *args, **kwargs):
            if "rotational" in path:
                raise AssertionError("must not read rotational under WSL")
            raise OSError("unexpected path in this test")

        with (
            mock.patch("dupefinder.diskinfo.is_wsl", return_value=True),
            mock.patch(
                "dupefinder.diskinfo._query_windows_physical_disk",
                return_value=("ssd", "nvme"),
            ),
            mock.patch("builtins.open", side_effect=_fail_on_rotational),
        ):
            detect("/mnt/c/users")  # must not raise


class CombineTests(unittest.TestCase):
    def test_combining_hdd_and_ssd_folders_suggests_the_minimum(self) -> None:
        hdd = VolumeInfo(path="/a", kind="hdd", transport="usb", label="HDD", suggested_workers=1)
        ssd = VolumeInfo(path="/b", kind="ssd", transport="nvme", label="SSD", suggested_workers=4)
        self.assertEqual(combine([hdd, ssd]), 1)


if __name__ == "__main__":
    unittest.main()

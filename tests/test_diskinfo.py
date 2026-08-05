"""Tests for dupefinder.diskinfo. All hardware/subprocess access is mocked."""

import ntpath
import unittest
from unittest import mock

from dupefinder.diskinfo import VolumeInfo, _detect_windows, _query_windows_physical_disk, combine, detect


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


class WindowsDriveLetterInjectionTests(unittest.TestCase):
    """POST /api/volumes reaches _detect_windows with an attacker-controlled
    path and no validation. A crafted UNC-style path must never let shell
    metacharacters reach subprocess.run's PowerShell -Command string.
    """

    def test_non_single_letter_drive_is_rejected_before_subprocess_run(self) -> None:
        with mock.patch("subprocess.run") as run:
            result = _query_windows_physical_disk("C; calc")

        run.assert_not_called()
        self.assertIsNone(result)

    def test_unc_style_path_with_shell_metacharacter_never_reaches_subprocess_run(self) -> None:
        # ntpath.splitdrive has no sanitization: for a malformed UNC-style
        # path it returns the entire path (including a `;`, PowerShell's
        # statement separator) as the "drive". This is the exact shape of
        # POST /api/volumes {"a": "\\\\x\\y; calc"} on native Windows.
        malicious_path = r"\\x\y; calc"
        drive, _ = ntpath.splitdrive(malicious_path)
        self.assertIn(";", drive)  # sanity: confirms this reproduces the vulnerable shape

        with (
            mock.patch("os.path.splitdrive", return_value=(drive, "")),
            mock.patch("subprocess.run") as run,
        ):
            info = _detect_windows(malicious_path)

        run.assert_not_called()
        self.assertEqual(info.kind, "unknown")
        self.assertEqual(info.suggested_workers, 1)


class CombineTests(unittest.TestCase):
    def test_combining_hdd_and_ssd_folders_suggests_the_minimum(self) -> None:
        hdd = VolumeInfo(path="/a", kind="hdd", transport="usb", label="HDD", suggested_workers=1)
        ssd = VolumeInfo(path="/b", kind="ssd", transport="nvme", label="SSD", suggested_workers=4)
        self.assertEqual(combine([hdd, ssd]), 1)


if __name__ == "__main__":
    unittest.main()

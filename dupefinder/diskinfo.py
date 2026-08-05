"""Disk-type detection and suggested io_workers derivation.

Never guesses upward: any detection failure resolves to kind="unknown",
suggested_workers=1.
"""

import os
import platform
import plistlib
import re
import subprocess
from dataclasses import dataclass

POWERSHELL_TIMEOUT = 10  # seconds; a hung call must never stall the UI


@dataclass
class VolumeInfo:
    path: str
    kind: str          # "ssd" | "hdd" | "network" | "unknown"
    transport: str      # "nvme" | "usb" | "sata" | "9p" | ""
    label: str          # human string for the UI
    suggested_workers: int


def is_wsl() -> bool:
    if "WSL_DISTRO_NAME" in os.environ:
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _suggest_workers(kind: str, transport: str) -> int:
    if kind == "hdd":
        return 1
    if kind == "ssd":
        return 8 if transport == "9p" else 4
    if kind == "network":
        return 8
    return 1


def _query_windows_physical_disk(drive_letter: str) -> tuple[str, str] | None:
    """Run the two-step PowerShell query for one Windows drive letter.

    Returns (media_type, bus_type) lowercase, or None on any failure --
    timeout, missing powershell.exe, non-zero exit, or unparsable output.
    Never raises.

    `drive_letter` must be exactly one letter. It is interpolated directly
    into a PowerShell -Command string, so anything else (e.g. a UNC path
    fragment smuggled in through a malformed path) is rejected before it can
    reach the shell -- never guessed at or partially sanitized.
    """
    if not re.fullmatch(r"[A-Za-z]", drive_letter):
        return None
    script = (
        f"$n = (Get-Volume -DriveLetter {drive_letter} | Get-Partition | Get-Disk).Number; "
        "$d = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq $n }; "
        "Write-Output ($d.MediaType.ToString() + ',' + $d.BusType.ToString())"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=POWERSHELL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or "," not in result.stdout:
        return None
    media, bus = result.stdout.strip().split(",", 1)
    return media.strip().lower(), bus.strip().lower()


def detect(path: str) -> VolumeInfo:
    """Detect the disk type backing `path` and suggest an io_workers value."""
    if is_wsl():
        return _detect_wsl(path)
    if os.name == "nt":
        return _detect_windows(path)
    if platform.system() == "Darwin":
        return _detect_macos(path)
    return _detect_linux(path)


def _unknown(path: str, transport: str = "") -> VolumeInfo:
    return VolumeInfo(path=path, kind="unknown", transport=transport, label="Unknown", suggested_workers=1)


def _detect_wsl(path: str) -> VolumeInfo:
    match = re.match(r"^/mnt/([a-zA-Z])(/|$)", path)
    if match:
        drive = match.group(1).upper()
        drive_label = f"{drive}:"
    else:
        # Anything else under WSL (e.g. /home) lives on the VHDX on the
        # Windows system drive -- query that drive instead. `rotational` is
        # never trusted here: WSL's virtual disks all report rotational=1
        # even when backed by NVMe.
        drive = "C"
        drive_label = "C: (WSL system disk)"

    queried = _query_windows_physical_disk(drive)
    if queried is None:
        return _unknown(path, transport="9p")

    media, bus = queried
    kind = "hdd" if media == "hdd" else "ssd" if media == "ssd" else "unknown"
    if kind == "unknown":
        return _unknown(path, transport="9p")
    prefix = "USB " if bus == "usb" else ""
    label = f"{prefix}{kind.upper()} (via WSL 9p, {drive_label})"
    return VolumeInfo(
        path=path, kind=kind, transport=bus, label=label, suggested_workers=_suggest_workers(kind, "9p")
    )


def _detect_windows(path: str) -> VolumeInfo:
    drive = os.path.splitdrive(path)[0].rstrip(":\\").rstrip(":") or "C"
    queried = _query_windows_physical_disk(drive)
    if queried is None:
        return _unknown(path)
    media, bus = queried
    kind = "hdd" if media == "hdd" else "ssd" if media == "ssd" else "unknown"
    if kind == "unknown":
        return _unknown(path)
    return VolumeInfo(
        path=path, kind=kind, transport=bus, label=f"{kind.upper()} ({bus.upper()})",
        suggested_workers=_suggest_workers(kind, bus),
    )


def _detect_linux(path: str) -> VolumeInfo:
    try:
        st = os.stat(path)
        sys_path = f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        real = os.path.realpath(sys_path)
        block_name = os.path.basename(real)
        rotational_path = f"/sys/block/{block_name}/queue/rotational"
        if not os.path.exists(rotational_path):
            # A partition device (e.g. sda1) -- the parent directory name is
            # the whole disk, which is where queue/rotational actually lives.
            parent_name = os.path.basename(os.path.dirname(real))
            rotational_path = f"/sys/block/{parent_name}/queue/rotational"
        with open(rotational_path, encoding="utf-8") as f:
            rotational = f.read().strip()
    except OSError:
        return _unknown(path)

    kind = "hdd" if rotational == "1" else "ssd" if rotational == "0" else "unknown"
    if kind == "unknown":
        return _unknown(path)
    return VolumeInfo(
        path=path, kind=kind, transport="", label=kind.upper(), suggested_workers=_suggest_workers(kind, "")
    )


def _detect_macos(path: str) -> VolumeInfo:
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", "/"], capture_output=True, timeout=POWERSHELL_TIMEOUT
        )
        if result.returncode != 0:
            raise OSError("diskutil failed")
        info = plistlib.loads(result.stdout)
        is_solid_state = info.get("SolidState")
    except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException):
        return _unknown(path)

    if is_solid_state is None:
        return _unknown(path)
    kind = "ssd" if is_solid_state else "hdd"
    return VolumeInfo(
        path=path, kind=kind, transport="", label=kind.upper(), suggested_workers=_suggest_workers(kind, "")
    )


def combine(volumes: list[VolumeInfo]) -> int:
    """Suggested worker count for two folders together: the minimum of the two."""
    if not volumes:
        return 1
    return min(v.suggested_workers for v in volumes)

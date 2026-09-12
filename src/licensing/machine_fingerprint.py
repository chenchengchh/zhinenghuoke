from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from typing import Mapping

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None


FINGERPRINT_VERSION = "fp_v2"
FINGERPRINT_COMPONENT_KEYS = (
    "baseboard_serial",
    "bios_serial",
    "processor_id",
    "system_uuid",
    "disk_serial",
    "machine_guid",
)
INVALID_HARDWARE_VALUES = {
    "",
    "0",
    "UNKNOWN",
    "DEFAULT STRING",
    "SYSTEM SERIAL NUMBER",
    "TO BE FILLED BY O.E.M.",
    "TO BE FILLED BY OEM",
    "NOT SPECIFIED",
    "NONE",
    "NULL",
}
MIN_REQUIRED_FINGERPRINT_COMPONENTS = 3


class MachineFingerprintError(RuntimeError):
    pass


def normalize_hardware_value(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized:
        return ""
    normalized = " ".join(normalized.split())
    if normalized in INVALID_HARDWARE_VALUES:
        return ""
    if set(normalized) == {"0"}:
        return ""
    return normalized


def _safe_read_machine_guid() -> str:
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return normalize_hardware_value(value)
    except Exception:
        return ""


def _run_powershell_json(command: str) -> dict[str, object]:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if completed.returncode != 0:
        raise MachineFingerprintError((completed.stderr or completed.stdout or "powershell_error").strip())
    raw = (completed.stdout or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MachineFingerprintError(f"hardware_json_decode_failed:{exc}") from exc
    if not isinstance(parsed, dict):
        raise MachineFingerprintError("hardware_json_invalid")
    return parsed


def collect_hardware_identifiers() -> dict[str, str]:
    command = r"""
$result = [ordered]@{
  baseboard_serial = ""
  bios_serial = ""
  processor_id = ""
  system_uuid = ""
  disk_serial = ""
}
try { $result.baseboard_serial = (Get-CimInstance Win32_BaseBoard | Select-Object -First 1 -ExpandProperty SerialNumber) } catch {}
try { $result.bios_serial = (Get-CimInstance Win32_BIOS | Select-Object -First 1 -ExpandProperty SerialNumber) } catch {}
try { $result.processor_id = (Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty ProcessorId) } catch {}
try { $result.system_uuid = (Get-CimInstance Win32_ComputerSystemProduct | Select-Object -First 1 -ExpandProperty UUID) } catch {}
try {
  $disk = Get-CimInstance Win32_DiskDrive |
    Where-Object { $_.SerialNumber -and $_.SerialNumber.Trim() -ne "" } |
    Select-Object -First 1 -ExpandProperty SerialNumber
  $result.disk_serial = $disk
} catch {}
$result | ConvertTo-Json -Compress
"""
    raw_identifiers = _run_powershell_json(command)
    return {
        key: normalize_hardware_value(raw_identifiers.get(key, ""))
        for key in ("baseboard_serial", "bios_serial", "processor_id", "system_uuid", "disk_serial")
    }


def collect_machine_fingerprint_components() -> dict[str, str]:
    components = collect_hardware_identifiers()
    components["machine_guid"] = _safe_read_machine_guid()
    return {key: normalize_hardware_value(components.get(key, "")) for key in FINGERPRINT_COMPONENT_KEYS}


def get_machine_fingerprint_profile(components: Mapping[str, str]) -> dict[str, object]:
    available = [key for key in FINGERPRINT_COMPONENT_KEYS if normalize_hardware_value(components.get(key, ""))]
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "available_keys": available,
        "available_count": len(available),
        "minimum_required": MIN_REQUIRED_FINGERPRINT_COMPONENTS,
        "has_baseboard_serial": "baseboard_serial" in available,
        "has_bios_serial": "bios_serial" in available,
        "has_processor_id": "processor_id" in available,
        "has_system_uuid": "system_uuid" in available,
        "has_disk_serial": "disk_serial" in available,
        "has_machine_guid": "machine_guid" in available,
    }


def build_machine_fingerprint_from_components(components: Mapping[str, str]) -> str:
    normalized_components = {
        key: normalize_hardware_value(components.get(key, ""))
        for key in FINGERPRINT_COMPONENT_KEYS
    }
    profile = get_machine_fingerprint_profile(normalized_components)
    if int(profile["available_count"]) < MIN_REQUIRED_FINGERPRINT_COMPONENTS:
        raise MachineFingerprintError("hardware_identifier_insufficient")
    raw = "|".join(normalized_components[key] for key in FINGERPRINT_COMPONENT_KEYS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def build_machine_fingerprint() -> str:
    return build_machine_fingerprint_from_components(collect_machine_fingerprint_components())

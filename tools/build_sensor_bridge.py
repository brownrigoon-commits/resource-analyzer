"""Fetch pinned, unmodified sensor dependencies and build the .NET Framework bridge.

This builds user-space files only. It never installs PawnIO or starts the helper.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.6"
LHM_URL = f"https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v{VERSION}/LibreHardwareMonitor.zip"
LHM_SHA256 = "086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001"
PAWN_URL = f"https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/v{VERSION}/LibreHardwareMonitor/Resources/PawnIO_setup.exe"
PAWN_SHA256 = "a3a46226c5e2824f4cdd42be0eecbabfc672c86f7889710f5ab1e6ad385b47a0"


def write_if_changed(path: Path, data: bytes) -> None:
    # Permit rebuilding while the unchanged official installer is open.
    if path.is_file() and path.read_bytes() == data:
        return
    path.write_bytes(data)


def verified_bytes(url: str, expected: str, local: Path | None = None) -> bytes:
    if local is not None:
        data = local.read_bytes()
    else:
        request = Request(url, headers={"User-Agent": "ResourceAnalyzer-sensor-build/0.3"})
        with urlopen(request, timeout=60) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise RuntimeError("Dependency download exceeded the size limit")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f"SHA-256 mismatch for {local or url}: {actual}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use an already downloaded, checksum-verified release ZIP")
    parser.add_argument("--pawnio-installer", type=Path, help="Use an already downloaded, checksum-verified PawnIO installer")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("The sensor bridge is built on Windows with .NET Framework 4.7.2 or later.")
    destination = ROOT / "vendor" / "sensors"
    destination.mkdir(parents=True, exist_ok=True)
    archive = verified_bytes(LHM_URL, LHM_SHA256, args.archive)
    installer = verified_bytes(PAWN_URL, PAWN_SHA256, args.pawnio_installer)
    with tempfile.TemporaryDirectory(prefix="resource-sensor-build-") as temporary:
        temporary = Path(temporary)
        zip_path = temporary / "LibreHardwareMonitor.zip"
        zip_path.write_bytes(archive)
        with zipfile.ZipFile(zip_path) as package:
            # Copy all release DLLs, including transitive .NET dependencies;
            # no original GUI, PDBs, or driver is executed by this build step.
            names = [item for item in package.infolist() if not item.is_dir() and item.filename.lower().endswith(".dll")]
            seen = set()
            for item in names:
                relative = Path(item.filename)
                target = (destination / relative).resolve()
                if relative.is_absolute() or not target.is_relative_to(destination.resolve()):
                    raise RuntimeError("The release contains an unsafe DLL path")
                if str(relative).lower() in seen:
                    raise RuntimeError("The release contains duplicate DLL paths")
                seen.add(str(relative).lower())
                target.parent.mkdir(parents=True, exist_ok=True)
                write_if_changed(target, package.read(item))
        if not (destination / "LibreHardwareMonitorLib.dll").is_file():
            raise RuntimeError("The pinned release did not contain LibreHardwareMonitorLib.dll")
    write_if_changed(destination / "PawnIO_setup.exe", installer)
    compiler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise RuntimeError("The x64 .NET Framework compiler was not found")
    output = destination / "SensorBridge.exe"
    subprocess.run([
        str(compiler), "/nologo", "/optimize+", "/platform:x64", "/target:exe",
        "/out:" + str(output), "/reference:" + str(destination / "LibreHardwareMonitorLib.dll"),
        "/reference:System.Web.Extensions.dll", str(ROOT / "tools" / "SensorBridge.cs"),
    ], check=True)
    (destination / "SensorBridge.exe.config").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<configuration><startup><supportedRuntime version="v4.0" '
        'sku=".NETFramework,Version=v4.7.2" /></startup></configuration>\n', encoding="utf-8")
    notices = ROOT / "third_party"
    for source in notices.glob("*"):
        if source.is_file():
            shutil.copy2(source, destination / source.name)
    print(f"Built {output}")
    print(f"LibreHardwareMonitor {VERSION} SHA-256 {LHM_SHA256}")
    print(f"PawnIO installer SHA-256 {PAWN_SHA256}; not installed or executed")


if __name__ == "__main__":
    main()

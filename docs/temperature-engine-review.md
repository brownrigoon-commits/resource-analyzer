# Temperature engine review

## Scope and identity

Pinned upstream: LibreHardwareMonitor/LibreHardwareMonitor tag v0.9.6.
Official release: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/tag/v0.9.6

- Release ZIP SHA-256: `086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001`.
- LibreHardwareMonitorLib.dll SHA-256: `6ebc194316536ba61af5be24508ad9fcbb2ecc685e716c12e787c79530f66bf0`.
- Module MVID: `0283f14a-f91d-4577-8628-42076a45e683`.
- REA observed managed x64 CIL, .NET Framework 4.7.2, assembly 0.9.6.0, complete metadata within the parser's scope.

## REA evidence

- Identity/metadata: `ev_3981e99893e0793c9e4024ba14865414479643a7a609d1756d1838bd6fe82613`;
  derived summary `ev_2d0ef18f1a2dc0bc17ab29935fb72646af4fa5fccdad48aa3065a7f5e52eabab`.
- Selected Computer, DimmMemory, PawnIo and RAMSPDToolkitDriver members:
  `ev_6ca9dc53bff1b01e95d39c00181bf0028a3b22eb0dd3d08ad116eab880971826`.
  The initially requested standalone SpdThermalSensor full name was unmatched; upstream defines it as a nested type.
- Managed/native declarations: `ev_51308ff44f0fc59b04643734ebc1c778e55a233fb22b73c02da7eb031468493c`.
  Kernel/device and vendor-library imports are declarations, not observations that each function executed.

These are bounded static metadata/CIL inspections, not a full security audit, proof of no malicious behavior,
or proof of sensor availability. The upstream versioned source and separate real hardware tests corroborate integration behavior.

## Integration decisions from upstream source

- CPU and SMBus modules use already-installed official PawnIO; no WinRing0 fallback or protection bypass is added.
- RAMSPDToolkit-NDD uses the LHM PawnIO adapter rather than shipping its own WinRing0 implementation.
- DIMM `/temperature/0` is the live module temperature; later indices describe resolution and alarm limits.
  Taking the maximum of every Temperature-typed DIMM field would produce false readings.
- StorageDevice temperature indices 0-8 are live composite/probe readings. Indices 10/11 are warning/critical
  setpoints and are excluded even though upstream categorizes them as temperatures. The selected probe name
  accompanies each reading so the dashboard tooltip identifies which measurement supplied the maximum.
- Only temperature readings are serialized. Hardware control setters are not called by the bridge.
- Sampling uses the upstream sensor API. Some sensor drivers can retain their last valid value after a hardware
  read fails; a bridge timestamp establishes successful bridge polling, not an independent timestamp per sensor.
- The elevated bridge emits JSON on a random, current-user-scoped, output-only named pipe. It accepts no commands,
  writes no client-selected files, and exits when its parent or client disappears. The HTTP monitor is not elevated.
- Sensor work is separate from the one-second collection loop. Stalled bridge data expires after eight seconds.
- Official PawnIO installer redistribution and third-party notices are preserved. Source versions and licenses ship with the app.

## Source anchors

- https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/v0.9.6/LibreHardwareMonitor/UI/MainForm.cs
- https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/v0.9.6/LibreHardwareMonitorLib/Hardware/Cpu/Amd17Cpu.cs
- https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/v0.9.6/LibreHardwareMonitorLib/Hardware/Memory/DimmMemory.cs
- https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/v0.9.6/LibreHardwareMonitorLib/Hardware/Storage/StorageDevice.cs
- https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/v0.9.6/LibreHardwareMonitorLib/RAMSPDToolkitDriver.cs
- https://github.com/namazso/PawnIO.Modules/wiki/Using-PawnIO-Modules#licensing-considerations

## Validation status

- Built the sensor bridge and packaged Windows executable successfully.
- Checked the live dashboard at 700px: four cards retain their layout and display small temperature badges.
- Verified live GPU and storage readings through the packaged app. Storage alarm setpoints are excluded;
  tooltips identify the selected live probe. Missing CPU/DIMM prerequisites produce unavailable values, not zeroes.
- The five temperature data/freshness/nonblocking unit tests pass. The broader suite excluding the pre-existing
  concurrent file-IPC timing failure passes (81 tests, one platform privilege skip). The excluded test also fails
  on the unchanged baseline under the same environment.
- CPU/DIMM hardware validation remains pending completion of the official PawnIO installer's interactive dialog.
  After installation, restart the monitor to reinitialize the sensor bridge. DIMM readings additionally require
  physically supported, accessible module sensors.

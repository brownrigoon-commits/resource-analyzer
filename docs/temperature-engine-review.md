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
- Version 0.5.2 enables AMD, Intel and NVIDIA GPU sensors. GPU driver APIs do not inherit the
  CPU/DIMM PawnIO prerequisite gate; diagnostics are separated by hardware category.
  GPU sensor identifiers are retained independently from DXGI identifiers. The UI shows the highest
  available GPU sensor temperature and a named sensor list, without guessing adapter matches.
- The Windows sampler also reads D3DKMT_ADAPTER_PERFDATA by exact DXGI LUID. SDK x64 layout,
  NTSTATUS results, 0.1°C units and handle cleanup are checked; unsupported/invalid values stay missing.
  A zero reading is rejected as the app's conservative plausibility policy, not a documented API sentinel.
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

Current PC validation (2026-10-20, version 0.5.2): official PawnIO 2.1.0 installer checksum and
Authenticode signature verified, installation exit code 0, driver service Running. After restarting
the monitor, CPU, both DIMMs, integrated AMD GPU and storage temperatures were observed in Chrome.
The discrete AMD GPU rejected temperature requests through the current AMD ADL path; a separate
Windows D3DKMT probe successfully read both discrete and integrated GPU temperatures by exact LUID.
The explicit Python suite passes 60 tests, including 26 GPU tests and
8 temperature tests. Personal sensor readings and device identifiers are not committed.

Earlier validation, on the previous environment:

- Built the sensor bridge and packaged Windows executable successfully.
- Checked the live dashboard at 700px: four cards retain their layout and display small temperature badges.
- Verified live GPU and storage readings through the packaged app. Storage alarm setpoints are excluded;
  tooltips identify the selected live probe. Missing CPU/DIMM prerequisites produce unavailable values, not zeroes.
- The five temperature data/freshness/nonblocking unit tests pass. The broader suite excluding the pre-existing
  concurrent file-IPC timing failure passes (81 tests, one platform privilege skip). The excluded test also fails
  on the unchanged baseline under the same environment.
- Completed the official PawnIO installer and confirmed the driver service is running. After restarting the
  packaged monitor, verified live CPU and four DIMM temperature sensors alongside GPU and storage readings.
  All four dashboard badges display measured temperatures. DIMM readings on other machines additionally require
  physically supported, accessible module sensors. No local hardware inventory or raw sensor logs are committed.

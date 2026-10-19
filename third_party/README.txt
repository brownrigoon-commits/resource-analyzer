Resource Analyzer sensor support - third-party notices

LibreHardwareMonitor / LibreHardwareMonitorLib 0.9.6
Copyright LibreHardwareMonitor and Contributors.
Portions Copyright Michael Moeller and Open Hardware Monitor Contributors.
License: Mozilla Public License 2.0 (see LibreHardwareMonitor-LICENSE.txt).
Unmodified release and corresponding source:
https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/tag/v0.9.6
https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/v0.9.6
Source commit: 3d331e3370efb858411f19511373eff65a218701
Included PawnIO modules: LGPL 2.1; see LibreHardwareMonitor-THIRD-PARTY-NOTICES.txt.
These components remain separate, unmodified DLLs and retain their own licenses.

PawnIO official installer (the unchanged installer embedded in LHM v0.9.6)
Copyright namazso. Official binary distribution: proprietary; installer
redistribution is allowed by the author's published licensing information.
Installer SHA-256:
a3a46226c5e2824f4cdd42be0eecbabfc672c86f7889710f5ab1e6ad385b47a0
https://pawnio.eu/
https://github.com/namazso/PawnIO.Modules/wiki/Using-PawnIO-Modules#licensing-considerations
https://github.com/namazso/PawnIO
The source edition has GPL 2 (or later) with an IO-control linking exception;
this does not relicense the official signed binary installer as GPL.
Only the signed Official edition is intended for use by Resource Analyzer.
The build does not install a driver, change security settings, or run the installer.

Other release DLL dependencies (unmodified):
RAMSPDToolkit-NDD 1.4.2 / BlackSharp.Core: MPL 2.0
https://github.com/Blacktempel/RAMSPDToolkit/tree/1.4.2
https://github.com/Blacktempel/BlackSharp
DiskInfoToolkit 1.1.2: MPL 2.0
https://github.com/Blacktempel/DiskInfoToolkit
HidSharp 2.6.4: Apache License 2.0 (HidSharp-LICENSE.txt and HidSharp-NOTICE.txt)
https://github.com/IntergatedCircuits/HidSharp/tree/main-2.6
https://software.seekye.com/hidsharp
Microsoft .NET support assemblies: MIT and their included third-party terms
https://github.com/dotnet/runtime
See Microsoft-DotNet-LICENSE.txt and Microsoft-DotNet-THIRD-PARTY-NOTICES.txt.
Aga.Controls: BSD notice included in LibreHardwareMonitor-THIRD-PARTY-NOTICES.txt.
OxyPlot / OxyPlot.WindowsForms: MIT (OxyPlot-LICENSE.txt)
https://github.com/oxyplot/oxyplot/tree/v2.2.0
TaskScheduler: MIT (TaskScheduler-LICENSE.txt)
https://github.com/dahall/TaskScheduler

SensorBridge source and build instructions are part of Resource Analyzer.
Its implementation reads sensor values only. It does not invoke fan controls,
sensor limit setters, SPD programming, or an unrestricted driver edition.

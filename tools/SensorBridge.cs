// Read-only sensor adapter for LibreHardwareMonitor 0.9.6. No control APIs.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using LibreHardwareMonitor.Hardware;
using LibreHardwareMonitor.PawnIo;

internal static class SensorBridge
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static volatile bool stopping;

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 1 && args[0] == "--once")
                return Run(null, null, true);

            int parentId;
            if (args.Length != 4 || args[0] != "--pipe" || args[2] != "--parent" ||
                !Regex.IsMatch(args[1], @"\AResourceAnalyzer\.Temp\.[0-9a-fA-F]{32}\z") ||
                !Int32.TryParse(args[3], out parentId) || parentId <= 0)
            {
                Console.Error.WriteLine("Use --once or --pipe ResourceAnalyzer.Temp.<32 hex digits> --parent <PID>.");
                return 2;
            }

            using (Process parent = Process.GetProcessById(parentId))
            {
                if (parent.HasExited) return 0;
                return Run(args[1], parent, false);
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Sensor bridge: " + ex.GetType().Name + ": " + ex.Message);
            return 1;
        }
    }

    private static int Run(string pipeName, Process parent, bool once)
    {
        bool pawnInstalled = PawnIo.IsInstalled;
        bool admin;
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            admin = new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);

        // Without both prerequisites, AMD SMN reads can appear as zero instead
        // of reporting failure. Do not publish those as measured temperatures.
        bool lowLevelReady = pawnInstalled && admin;
        string prerequisite = !pawnInstalled ? "PawnIO is not installed." :
            !admin ? "Administrator permission is required for CPU and DIMM temperatures." : null;

        Computer computer = new Computer
        {
            IsCpuEnabled = true,
            IsMemoryEnabled = true,
            IsStorageEnabled = true,
            IsMotherboardEnabled = false,
            IsControllerEnabled = false,
            IsGpuEnabled = false,
            IsNetworkEnabled = false,
            IsPsuEnabled = false,
            IsBatteryEnabled = false
        };
        NamedPipeServerStream pipe = null;
        Timer parentWatch = null;
        StreamWriter writer = null;
        try
        {
            if (!once)
            {
                SecurityIdentifier sid;
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent()) sid = identity.User;
                if (sid == null) throw new InvalidOperationException("The current user has no SID.");
                PipeSecurity security = new PipeSecurity();
                security.SetAccessRuleProtection(true, false);
                security.SetOwner(sid);
                security.AddAccessRule(new PipeAccessRule(sid, PipeAccessRights.FullControl, AccessControlType.Allow));
                pipe = new NamedPipeServerStream(pipeName, PipeDirection.Out, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 0, 16384, security);
                parentWatch = new Timer(delegate
                {
                    try { if (!parent.HasExited) return; } catch { }
                    stopping = true;
                    try { pipe.Dispose(); } catch { }
                }, null, 0, 500);

                IAsyncResult pending = pipe.BeginWaitForConnection(null, null);
                using (WaitHandle ready = pending.AsyncWaitHandle)
                {
                    if (!ready.WaitOne(60000) || stopping) return 0;
                    pipe.EndWaitForConnection(pending);
                }
                writer = new StreamWriter(pipe, new UTF8Encoding(false), 4096);
                writer.AutoFlush = true;
            }

            computer.Open();
            // DIMM discovery retries asynchronously on some boards. One-shot
            // mode still samples actual sensors only; it does not wait forever.
            do
            {
                List<Dictionary<string, object>> sensors = new List<Dictionary<string, object>>();
                List<string> errors = new List<string>();
                if (prerequisite != null) errors.Add(prerequisite);
                foreach (IHardware hardware in computer.Hardware)
                    ReadHardware(hardware, lowLevelReady, sensors, errors);

                Dictionary<string, object> sample = new Dictionary<string, object>
                {
                    { "sampled_at", (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds },
                    { "sensors", sensors },
                    { "error", errors.Count == 0 ? null : String.Join(" ", errors.ToArray()) },
                    { "pawnio_installed", pawnInstalled }
                };
                string line = Json.Serialize(sample);
                if (once) { Console.WriteLine(line); break; }
                if (stopping) break;
                writer.WriteLine(line);
                for (int i = 0; i < 20 && !stopping; ++i) Thread.Sleep(100);
            } while (!stopping);
            return 0;
        }
        catch (IOException) { return once ? 1 : 0; }
        catch (ObjectDisposedException) { if (stopping) return 0; throw; }
        finally
        {
            stopping = true;
            if (parentWatch != null) parentWatch.Dispose();
            try { computer.Close(); } catch { }
            if (writer != null) { try { writer.Dispose(); } catch { } }
            if (pipe != null) pipe.Dispose();
        }
    }

    private static void ReadHardware(IHardware hardware, bool lowLevelReady,
        List<Dictionary<string, object>> output, List<string> errors)
    {
        try
        {
            hardware.Update();
            string kind = hardware.HardwareType == HardwareType.Cpu ? "cpu" :
                hardware.HardwareType == HardwareType.Memory ? "ram" :
                hardware.HardwareType == HardwareType.Storage ? "disk" : null;
            if (kind != null && (kind == "disk" || lowLevelReady))
            {
                float? maximum = null;
                foreach (ISensor sensor in hardware.Sensors)
                {
                    if (sensor.SensorType != SensorType.Temperature || !sensor.Value.HasValue) continue;
                    string name = sensor.Name ?? "";
                    if (Regex.IsMatch(name, @"(?:limit|critical|threshold|resolution|\bmax(?:imum)?\b|\bmin(?:imum)?\b)", RegexOptions.IgnoreCase)) continue;
                    if (kind == "ram" && !Regex.IsMatch(sensor.Identifier.ToString(), @"\A/memory/dimm/[0-9]+/temperature/0\z")) continue;
                    float value = sensor.Value.Value;
                    if (Single.IsNaN(value) || Single.IsInfinity(value) || value < -20 || value > 150) continue;
                    // Zero is the failure result of an unavailable AMD PawnIO
                    // read and is not a plausible running Ryzen temperature.
                    if (kind == "cpu" && value == 0) continue;
                    if (!maximum.HasValue || value > maximum.Value) maximum = value;
                }
                if (maximum.HasValue)
                    output.Add(new Dictionary<string, object>
                    {
                        { "kind", kind }, { "name", hardware.Name },
                        { "temp_c", Math.Round(maximum.Value, 2) },
                        { "source", "LibreHardwareMonitor" }
                    });
            }
        }
        catch (Exception ex)
        {
            if (errors.Count < 4) errors.Add("Sensor update failed: " + ex.GetType().Name + ".");
        }
        foreach (IHardware child in hardware.SubHardware)
            ReadHardware(child, lowLevelReady, output, errors);
    }
}

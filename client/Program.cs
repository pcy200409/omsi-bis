using System.Diagnostics;
using System.Net.Http;
using System.Net.Http.Json;
using OmsiHook;

// ── OMSI BIS client ────────────────────────────────────────────────────
// Reads the player bus position from a running OMSI 2 (via OmsiHook, memory
// read-only — no OMSI plugin needed) and POSTs it to the BIS server.
//
// Requires running elevated (app.manifest requests admin) because OMSI is
// usually elevated and reading its memory needs matching integrity.
//
// Args (all optional):
//   [serverUrl] [nick] [line] [map]
// e.g.  OmsiBisClient.exe http://127.0.0.1:8000 홍길동 143 "Segang Alpha"

string server = Arg(0, "http://127.0.0.1:8000").TrimEnd('/');
string nick   = Arg(1, "player");
string line   = Arg(2, "");
string map    = Arg(3, "Segang Alpha");
string id     = $"{Environment.MachineName}-{Environment.UserName}".ToLowerInvariant();
string updateUrl = server + "/api/update";

string logPath = Path.Combine(Path.GetTempPath(), "omsibis_client.log");
try { File.WriteAllText(logPath, ""); } catch { }
void Log(string s) { Console.WriteLine(s); try { File.AppendAllText(logPath, s + Environment.NewLine); } catch { } }

Log($"OMSI BIS client → {updateUrl}  (id={id}, nick={nick}, line={line}, map={map})");
Log($"elevated: {IsElevated()}   log: {logPath}");

var http = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };

var omsi = new OmsiHook.OmsiHook();
try { await omsi.AttachToOMSI(false); Log("OmsiHook attached (read-only)."); }
catch (Exception ex) { Log($"attach failed: {ex.Message}"); }

int sent = 0, tick = 0;
string lastState = "";
void State(string s) { if (s != lastState) { Log(s); lastState = s; } }

// The marker is placed from OMSI's schedule (next stop + prev/next distance), which
// changes slowly, and the web strip eases motion with CSS — so a low send rate looks
// identical while cutting the shared server's CPU load ~5x. We POST at ~2 Hz, but
// send IMMEDIATELY whenever the next-stop id changes so stop transitions are instant.
const int HEARTBEAT_MS = 500;      // ~2 Hz baseline
var lastPost = DateTime.MinValue;
uint lastIdCode = uint.MaxValue;

// A non-elevated process can't kill this elevated one, so allow a graceful
// self-quit: touching this sentinel file makes the client exit and release
// its exe lock — lets tooling redeploy without the user closing the window.
string stopPath = Path.Combine(Path.GetTempPath(), "omsibis_stop");
try { if (File.Exists(stopPath)) File.Delete(stopPath); } catch { }

while (true)
{
    if (File.Exists(stopPath)) { try { File.Delete(stopPath); } catch { } Log("stop signal — exiting."); return; }
    tick++;
    float x = 0, y = 0, z = 0; int kachel = -1; bool havePos = false;
    // OMSI's own schedule view of where the bus is on its line (the ground truth)
    int nextIdx = -1; uint nextIdCode = 0; float nextDist = 0, prevDist = 0, atStation = 0;
    string nextName = ""; bool schedValid = false;
    try
    {
        var pv = omsi.Globals.PlayerVehicle;
        if (pv is not null)
        {
            var p = pv.Position; x = p.x; y = p.y; z = p.z;
            try { kachel = pv.Kachel; } catch { kachel = -1; }
            try { schedValid = pv.AI_Scheduled_Info_Valid; } catch { }
            try { nextIdx = pv.AI_Scheduled_NextBusstopIndex; } catch { }
            try { nextIdCode = pv.AI_Scheduled_NextBusstopIDCode; } catch { }
            try { nextDist = pv.AI_Scheduled_NextBusstopDist; } catch { }
            try { prevDist = pv.AI_Scheduled_PrevBusstopDist; } catch { }
            try { atStation = pv.AI_Scheduled_AtStation; } catch { }
            try { nextName = pv.AI_Scheduled_NextBusstopName ?? ""; } catch { }
            havePos = true;
        }
    }
    catch { /* menu / OMSI closed / not readable yet */ }

    if (!havePos) { State("waiting for a loaded map + bus (or OMSI/elevation)…"); await Task.Delay(500); continue; }
    State("streaming position ✓");

    // send on next-stop change, else at the heartbeat rate — bounds load to ~2 Hz
    bool changed = nextIdCode != lastIdCode;
    if (changed || (DateTime.UtcNow - lastPost).TotalMilliseconds >= HEARTBEAT_MS)
    {
        try
        {
            await http.PostAsJsonAsync(updateUrl, new { id, nick, line, map,
                nextIdx, nextIdCode, nextDist, prevDist, atStation, nextName, schedValid });
            sent++; lastPost = DateTime.UtcNow; lastIdCode = nextIdCode;
            if (sent % 25 == 0) Log($"sent {sent}  nextIdx={nextIdx} name='{nextName}' nextDist={nextDist:F0} prevDist={prevDist:F0} valid={schedValid} idcode={nextIdCode}");
        }
        catch (Exception ex) { State($"server unreachable: {ex.Message}"); }
    }

    await Task.Delay(100); // poll OMSI at 10 Hz to catch stop changes fast; POST is throttled above
}

string Arg(int i, string def) { var a = Environment.GetCommandLineArgs(); return a.Length > i + 1 ? a[i + 1] : def; }

static bool IsElevated()
{
    try
    {
        using var wi = System.Security.Principal.WindowsIdentity.GetCurrent();
        return new System.Security.Principal.WindowsPrincipal(wi)
            .IsInRole(System.Security.Principal.WindowsBuiltInRole.Administrator);
    }
    catch { return false; }
}

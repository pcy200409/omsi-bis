using System.Diagnostics;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Windows.Forms;
using OmsiHook;

// ── OMSI BIS client (GUI) ──────────────────────────────────────────────
// A window to stream the player bus position to the BIS server: fill in the
// server address / nickname / line / map and press 시작. Reads OMSI memory via
// OmsiHook (read-only, no plugin). Needs admin (app.manifest requests it) because
// OMSI is usually elevated.
//
// Two editions from one source (csproj: -p:BisEdition=User|Admin):
//   USER_BUILD  = 기사용. 운행 정보 보내기만. 로컬 서버 조작·로그창 없음.
//   (default)   = 관리자용. 로컬 서버 켜기/끄기 + 상세 로그.

static class Program
{
    [STAThread]
    static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new MainForm());
    }
}

class Settings
{
    public string Server { get; set; } = "https://omsi-bis.onrender.com";
    public string Nick { get; set; } = "";
    public string Line { get; set; } = "124";
    public string Map { get; set; } = "Segang Alpha";
    public string VehNo { get; set; } = "";
    public string Company { get; set; } = "";

    static string Path_ => System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "OmsiBisClient", "settings.json");

    public static Settings Load()
    {
        try { return JsonSerializer.Deserialize<Settings>(File.ReadAllText(Path_)) ?? new Settings(); }
        catch { return new Settings(); }
    }
    public void Save()
    {
        try
        {
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path_)!);
            File.WriteAllText(Path_, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { }
    }
}

class MainForm : Form
{
    readonly TextBox txtServer = new(), txtNick = new(), txtLine = new(), txtMap = new(),
                     txtVeh = new(), txtCompany = new();
    readonly Button btnStart = new(), btnServer = new();
    readonly Label lblStatus = new(), lblInfo = new(), lblAdmin = new(), lblServer = new();
    readonly TextBox txtLog = new();

    readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(4) };
    CancellationTokenSource? cts;
    Process? serverProc;
    int sent;

#if USER_BUILD
    const bool UserEdition = true;
#else
    const bool UserEdition = false;
#endif

    public MainForm()
    {
        var s = Settings.Load();
        Text = UserEdition ? "OMSI BIS 클라이언트 (기사용)" : "OMSI BIS 클라이언트 (관리자용)";
        Font = new System.Drawing.Font("Malgun Gothic", 9f);
        ClientSize = new System.Drawing.Size(470, UserEdition ? 400 : 574);
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;

        Label L(string t, int x, int y, int w = 70) => new() { Text = t, Left = x, Top = y + 3, Width = w, AutoSize = false };

        lblAdmin.SetBounds(300, 12, 160, 18);
        lblAdmin.TextAlign = System.Drawing.ContentAlignment.MiddleRight;
        bool admin = IsElevated();
        // 기사용은 정상일 때 아무 말 없이 (권한 없을 때만 경고 — 그땐 메모리를 못 읽는다)
        lblAdmin.Text = UserEdition ? (admin ? "" : "⚠ 관리자 권한으로 실행하세요")
                                    : (admin ? "관리자 권한 ✓" : "⚠ 관리자 아님");
        lblAdmin.ForeColor = admin ? System.Drawing.Color.SeaGreen : System.Drawing.Color.OrangeRed;
        if (UserEdition) lblAdmin.SetBounds(240, 12, 220, 18);

        Controls.Add(L("서버 주소", 16, 14, 70));
        txtServer.SetBounds(92, 12, 360, 24); txtServer.Text = s.Server;

        Controls.Add(L("닉네임", 16, 48));
        txtNick.SetBounds(92, 46, 150, 24); txtNick.Text = s.Nick;
        Controls.Add(L("노선", 262, 48, 40));
        txtLine.SetBounds(306, 46, 60, 24); txtLine.Text = s.Line;

        Controls.Add(L("맵", 16, 82));
        txtMap.SetBounds(92, 80, 200, 24); txtMap.Text = s.Map;

        Controls.Add(L("차량번호", 16, 116, 70));
        txtVeh.SetBounds(92, 114, 150, 24); txtVeh.Text = s.VehNo;
        Controls.Add(L("운행회사", 250, 116, 55));
        txtCompany.SetBounds(308, 114, 144, 24); txtCompany.Text = s.Company;

        btnStart.SetBounds(16, 150, 436, 40);
        btnStart.Text = "▶  시작";
        btnStart.Font = new System.Drawing.Font("Malgun Gothic", 11f, System.Drawing.FontStyle.Bold);
        btnStart.Click += (_, __) => Toggle();

        lblStatus.SetBounds(16, 200, 436, 22);
        lblStatus.Text = "대기 중";
        lblStatus.Font = new System.Drawing.Font("Malgun Gothic", 9.5f, System.Drawing.FontStyle.Bold);
        lblInfo.SetBounds(16, 224, 436, 20);
        lblInfo.ForeColor = System.Drawing.Color.DimGray;

        var ctrls = new List<Control> { lblAdmin, txtServer, txtNick, txtLine, txtMap, txtVeh, txtCompany,
            btnStart, lblStatus, lblInfo, txtLog };

        if (!UserEdition)      // 관리자용에만: 로컬 서버 켜기/끄기
        {
            var sep = new Label { Text = "─────  로컬 서버 (관리자)  ─────", Left = 16, Top = 256, Width = 436,
                TextAlign = System.Drawing.ContentAlignment.MiddleCenter, ForeColor = System.Drawing.Color.Silver };
            btnServer.SetBounds(16, 280, 150, 30); btnServer.Text = "로컬 서버 켜기";
            btnServer.Click += (_, __) => ToggleServer();
            lblServer.SetBounds(178, 280, 280, 30); lblServer.TextAlign = System.Drawing.ContentAlignment.MiddleLeft;
            lblServer.Text = "꺼짐";
            ctrls.Add(sep); ctrls.Add(btnServer); ctrls.Add(lblServer);

            txtLog.SetBounds(16, 322, 436, 236);
            // enable the local-server button only if we can find server/.venv nearby
            (serverDir, pythonExe) = FindServer();
            if (serverDir == null)
            {
                btnServer.Enabled = false;
                lblServer.Text = "서버 폴더 없음 (배포본은 불필요)";
                lblServer.ForeColor = System.Drawing.Color.Silver;
            }
        }
        else
        {
            txtLog.SetBounds(16, 252, 436, 132);       // 기사용은 작은 안내 로그만
        }
        txtLog.Multiline = true; txtLog.ReadOnly = true; txtLog.ScrollBars = ScrollBars.Vertical;
        txtLog.BackColor = System.Drawing.Color.FromArgb(245, 246, 248);
        txtLog.Font = new System.Drawing.Font("Consolas", 8.5f);

        Controls.AddRange(ctrls.ToArray());

        if (!admin)
            Log("⚠ 관리자 권한이 아니면 OMSI 메모리를 못 읽습니다. 이 창을 관리자 권한으로 다시 실행하세요.");
        Log(UserEdition
            ? "기사용 클라이언트입니다. 닉네임·노선·차량번호를 넣고 [시작]을 누르세요."
            : "관리자용 클라이언트입니다. 서버주소·닉네임 확인 후 [시작]을 누르세요.");

        FormClosing += (_, __) => { cts?.Cancel(); if (!UserEdition) StopServer(); SaveSettings(); };
    }

    void SaveSettings() => new Settings {
        Server = txtServer.Text.Trim(), Nick = txtNick.Text.Trim(),
        Line = txtLine.Text.Trim(), Map = txtMap.Text.Trim(),
        VehNo = txtVeh.Text.Trim(), Company = txtCompany.Text.Trim() }.Save();

    // ── client stream ──────────────────────────────────────────────────
    void Toggle()
    {
        if (cts == null) StartClient();
        else StopClient();
    }

    void StartClient()
    {
        if (string.IsNullOrWhiteSpace(txtNick.Text)) { txtNick.Focus(); Status("닉네임을 입력하세요", System.Drawing.Color.OrangeRed); return; }
        SaveSettings();
        SetInputs(false);
        btnStart.Text = "■  중지";
        sent = 0;
        cts = new CancellationTokenSource();
        _ = RunClient(cts.Token);
    }

    void StopClient()
    {
        cts?.Cancel(); cts = null;
        btnStart.Text = "▶  시작";
        SetInputs(true);
        Status("중지됨", System.Drawing.Color.DimGray);
        lblInfo.Text = "";
    }

    void SetInputs(bool on)
    { txtServer.Enabled = txtNick.Enabled = txtLine.Enabled = txtMap.Enabled =
        txtVeh.Enabled = txtCompany.Enabled = on; }

    async Task RunClient(CancellationToken ct)
    {
        string server = txtServer.Text.Trim().TrimEnd('/');
        string nick = txtNick.Text.Trim(), line = txtLine.Text.Trim(), map = txtMap.Text.Trim();
        string vehNo = txtVeh.Text.Trim(), company = txtCompany.Text.Trim();
        string id = $"{Environment.MachineName}-{Environment.UserName}".ToLowerInvariant();
        string url = server + "/api/update";

        var omsi = new OmsiHook.OmsiHook();
        Status("OMSI 연결 시도 중…", System.Drawing.Color.DarkOrange);
        while (!ct.IsCancellationRequested)
        {
            try { await omsi.AttachToOMSI(false); Log("OmsiHook 연결됨 (읽기 전용)."); break; }
            catch { Status("OMSI 대기 중 (OMSI를 실행하세요)…", System.Drawing.Color.DarkOrange); }
            try { await Task.Delay(2000, ct); } catch { return; }
        }

        const int HEARTBEAT_MS = 500;              // ~2 Hz baseline
        var lastPost = DateTime.MinValue;
        uint lastIdCode = uint.MaxValue;
        string lastState = "";
        void State(string s, System.Drawing.Color c) { if (s != lastState) { Status(s, c); lastState = s; } }

        while (!ct.IsCancellationRequested)
        {
            uint nextIdCode = 0; int nextIdx = -1; float nextDist = 0, prevDist = 0, atStation = 0;
            string nextName = ""; bool schedValid = false, havePos = false;
            try
            {
                var pv = omsi.Globals.PlayerVehicle;
                if (pv is not null)
                {
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

            if (!havePos) { State("맵+버스 대기 중 (버스에 탑승하세요)…", System.Drawing.Color.DarkOrange); }
            else
            {
                bool changed = nextIdCode != lastIdCode;
                if (changed || (DateTime.UtcNow - lastPost).TotalMilliseconds >= HEARTBEAT_MS)
                {
                    try
                    {
                        await http.PostAsJsonAsync(url, new { id, nick, line, map, vehNo, company,
                            nextIdx, nextIdCode, nextDist, prevDist, atStation, nextName, schedValid }, ct);
                        sent++; lastPost = DateTime.UtcNow; lastIdCode = nextIdCode;
                        State("● 전송 중", System.Drawing.Color.SeaGreen);
                        UI(() => lblInfo.Text = schedValid
                            ? $"다음 정류장: {nextName}  ·  보낸 수 {sent}"
                            : $"운행 스케줄 없음 (노선 배차 필요)  ·  보낸 수 {sent}");
                    }
                    catch (OperationCanceledException) { return; }
                    catch (Exception ex) { State("서버 연결 실패: " + Short(ex.Message), System.Drawing.Color.OrangeRed); }
                }
            }
            try { await Task.Delay(100, ct); } catch { return; }
        }
    }

    // ── local server ───────────────────────────────────────────────────
    string? serverDir; string? pythonExe;

    void ToggleServer()
    { if (serverProc == null) StartServer(); else StopServer(); }

    void StartServer()
    {
        if (serverDir == null || pythonExe == null) return;
        try
        {
            var psi = new ProcessStartInfo(pythonExe,
                "-m uvicorn app:app --host 127.0.0.1 --port 8000 --log-level warning")
            {
                WorkingDirectory = serverDir, UseShellExecute = false,
                RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
            };
            serverProc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            serverProc.OutputDataReceived += (_, e) => { if (e.Data != null) Log("[서버] " + e.Data); };
            serverProc.ErrorDataReceived += (_, e) => { if (e.Data != null) Log("[서버] " + e.Data); };
            serverProc.Exited += (_, __) => UI(() => { lblServer.Text = "꺼짐"; lblServer.ForeColor = System.Drawing.Color.Black; btnServer.Text = "로컬 서버 켜기"; serverProc = null; });
            serverProc.Start();
            serverProc.BeginOutputReadLine(); serverProc.BeginErrorReadLine();
            btnServer.Text = "로컬 서버 끄기";
            lblServer.Text = "실행 중 · http://127.0.0.1:8000";
            lblServer.ForeColor = System.Drawing.Color.SeaGreen;
            Log("로컬 서버 시작 (http://127.0.0.1:8000). 서버 주소 칸에 이 주소를 넣으면 로컬로 붙습니다.");
        }
        catch (Exception ex) { Log("로컬 서버 시작 실패: " + ex.Message); serverProc = null; }
    }

    void StopServer()
    {
        try { if (serverProc is { HasExited: false }) { serverProc.Kill(entireProcessTree: true); } } catch { }
        serverProc = null;
        UI(() => { lblServer.Text = "꺼짐"; lblServer.ForeColor = System.Drawing.Color.Black; btnServer.Text = "로컬 서버 켜기"; });
    }

    static (string?, string?) FindServer()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (int i = 0; i < 6 && dir != null; i++, dir = dir.Parent)
        {
            string sd = Path.Combine(dir.FullName, "server");
            string py = Path.Combine(sd, ".venv", "Scripts", "python.exe");
            if (File.Exists(Path.Combine(sd, "app.py")) && File.Exists(py)) return (sd, py);
        }
        return (null, null);
    }

    // ── helpers ────────────────────────────────────────────────────────
    void Status(string t, System.Drawing.Color c) => UI(() => { lblStatus.Text = t; lblStatus.ForeColor = c; });
    void Log(string s) => UI(() =>
    {
        txtLog.AppendText($"{DateTime.Now:HH:mm:ss}  {s}{Environment.NewLine}");
    });
    void UI(Action a) { if (IsHandleCreated && InvokeRequired) BeginInvoke(a); else a(); }
    static string Short(string s) => s.Length > 60 ? s[..60] + "…" : s;

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
}

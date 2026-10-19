using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

class PaperGraphLauncher : Form
{
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly Label status = new Label { AutoSize = false, Left = 24, Top = 60, Width = 530, Height = 95 };
    readonly Button graph = new Button { Text = "打开论文地图", Left = 24, Top = 160, Width = 155, Height = 40, Enabled = false };
    readonly Button papers = new Button { Text = "浏览论文", Left = 195, Top = 160, Width = 155, Height = 40, Enabled = false };
    readonly Button sources = new Button { Text = "数据源", Left = 365, Top = 160, Width = 155, Height = 40, Enabled = false };
    Process api, web;
    bool closing;
    readonly object logLock = new object();
    const string MapUrl = "http://127.0.0.1:3000/map?venue=icml&year=2025";

    [STAThread]
    static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using (var mutex = new Mutex(false, "PaperGraphLocalLauncher"))
        {
            if (!mutex.WaitOne(0, false)) { Open(MapUrl); return; }
            try
            {
                if (Array.IndexOf(args, "--self-test") >= 0)
                {
                    using (var form = new PaperGraphLauncher())
                    {
                        form.CheckFiles();
                        form.StartServices();
                        form.Log("Self-test PASS: API and frontend ready.");
                        form.StopOwned();
                    }
                    return;
                }
                Application.Run(new PaperGraphLauncher());
            }
            catch (Exception e) { MessageBox.Show(e.Message, "Paper Graph", MessageBoxButtons.OK, MessageBoxIcon.Error); Environment.ExitCode = 1; }
            finally { mutex.ReleaseMutex(); }
        }
    }

    public PaperGraphLauncher()
    {
        Text = "Paper Graph · 本地启动器";
        Width = 575; Height = 290; FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        Controls.Add(new Label { Text = "Paper Graph — Personal Academic Paper Explorer", Left = 24, Top = 20, Width = 530, Height = 30 });
        status.Text = "准备启动本地服务…";
        Controls.Add(status); Controls.Add(graph); Controls.Add(papers); Controls.Add(sources);
        Controls.Add(new Label { Text = "关闭此窗口会停止本次启动的服务；已在运行的服务会保留。", Left = 24, Top = 215, Width = 535, Height = 30 });
        graph.Click += delegate { Open(MapUrl); };
        papers.Click += delegate { Open("http://127.0.0.1:3000/?venue=icml&year=2025"); };
        sources.Click += delegate { Open("http://127.0.0.1:3000/sources"); };
        Shown += async delegate
        {
            try
            {
                await Task.Run(delegate { CheckFiles(); StartServices(); });
                if (closing) { StopOwned(); return; }
                status.Text = "服务已就绪。地图展示 ICML 2025 的 3,330 篇真实论文。\n拖动平移、Ctrl + 滚轮缩放；选择节点后可进入详情。\n地图数据使用项目已有 TF-IDF/SVD 适配器。";
                graph.Enabled = papers.Enabled = sources.Enabled = true;
                Open(MapUrl);
            }
            catch (Exception e) { Log(e.ToString()); StopOwned(); if (!closing) status.Text = "启动失败：" + e.Message + "\n详细日志：Data\\logs\\launcher.log"; }
        };
        FormClosing += delegate { closing = true; StopOwned(); };
    }

    void CheckFiles()
    {
        foreach (var file in new[] { "research-self\\.venv\\Scripts\\python.exe", "acceptodds\\node_modules\\next\\dist\\bin\\next", "acceptodds\\.next\\BUILD_ID", "Data\\database\\papers.sqlite" })
            if (!File.Exists(Path.Combine(root, file))) throw new Exception("缺少 " + file + "。请保留 EXE 在项目根目录，并按照 README 安装依赖/构建前端。");
    }

    string Read(string url)
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create(url);
            request.Proxy = null; request.Timeout = 2000; request.ReadWriteTimeout = 2000;
            using (var response = request.GetResponse())
            using (var reader = new StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
        }
        catch { return null; }
    }

    bool ApiReady()
    {
        var result = Read("http://127.0.0.1:8000/api/health");
        var expected = Path.Combine(root, "Data\\database\\papers.sqlite");
        return result != null && result.Contains("sqlite") && result.Replace("\\\\", "\\").IndexOf(expected, StringComparison.OrdinalIgnoreCase) >= 0;
    }
    bool WebReady() { var result = Read("http://127.0.0.1:3000"); return result != null && result.Contains("Paper Graph") && result.Contains("Data Sources"); }
    bool Occupied(int port)
    {
        try { using (var client = new TcpClient()) { var task = client.ConnectAsync("127.0.0.1", port); return task.Wait(600) && client.Connected; } }
        catch { return false; }
    }
    void StartServices()
    {
        try
        {
            if (!ApiReady())
            {
                if (Occupied(8000)) throw new Exception("端口 8000 被其他服务占用。");
                api = Start(Path.Combine(root, "research-self\\.venv\\Scripts\\python.exe"), "-m uvicorn paper_ingest.api:app --host 127.0.0.1 --port 8000", "research-self", "API");
            }
            WaitFor(ApiReady, "API");
            if (closing) return;
            if (!WebReady())
            {
                if (Occupied(3000)) throw new Exception("端口 3000 被其他服务占用。");
                web = Start("node.exe", "node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3000", "acceptodds", "WEB");
            }
            WaitFor(WebReady, "前端");
        }
        catch { StopOwned(); throw; }
    }
    void WaitFor(Func<bool> ready, string name)
    {
        for (int attempt = 0; attempt < 40; attempt++)
        {
            if (closing) throw new Exception("启动已取消。");
            if (ready()) return;
            Thread.Sleep(500);
        }
        throw new Exception(name + "启动超时，请检查 Data\\logs\\launcher.log。");
    }
    Process Start(string executable, string args, string directory, string name)
    {
        var info = new ProcessStartInfo(executable, args) { WorkingDirectory = Path.Combine(root, directory), UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        info.EnvironmentVariables["PAPER_DATA_DIR"] = Path.Combine(root, "Data");
        info.EnvironmentVariables["PAPER_DB_PATH"] = Path.Combine(root, "Data\\database\\papers.sqlite");
        info.EnvironmentVariables["PAPER_API_URL"] = "http://127.0.0.1:8000";
        info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        var process = new Process { StartInfo = info };
        process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) Log(name + " " + e.Data); };
        process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) Log(name + " " + e.Data); };
        process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine();
        Log("Started " + name + " PID " + process.Id); return process;
    }
    void Log(string message)
    {
        lock (logLock)
        {
            var directory = Path.Combine(root, "Data\\logs"); Directory.CreateDirectory(directory);
            File.AppendAllText(Path.Combine(directory, "launcher.log"), DateTime.Now.ToString("s") + " " + message + Environment.NewLine, Encoding.UTF8);
        }
    }
    void StopOwned()
    {
        foreach (var process in new[] { web, api })
        {
            if (process == null) continue;
            try
            {
                if (process.HasExited) continue;
                using (var kill = Process.Start(new ProcessStartInfo("taskkill.exe", "/PID " + process.Id + " /T /F") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true })) kill.WaitForExit(10000);
            }
            catch (Exception e) { Log("Shutdown: " + e.Message); }
        }
    }
    static void Open(string url) { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
}

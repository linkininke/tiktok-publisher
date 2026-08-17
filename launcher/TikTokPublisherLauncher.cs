// 双击 TikTokPublisher.exe → 启动修复版 (run_fixed.py)
using System;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Text;

class TikTokPublisherLauncher
{
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\', '/');
        try { Directory.SetCurrentDirectory(root); } catch { }

        string py = Path.Combine(root, "_tools", "py311", "python.exe");
        string script = Path.Combine(root, "run_fixed.py");
        string patch = Path.Combine(root, "patches", "fix_discard_draft.py");

        if (!File.Exists(py))
        {
            Fail("未找到嵌入式 Python：_tools\\py311\\python.exe");
            return 1;
        }
        if (!File.Exists(script))
        {
            Fail("未找到 run_fixed.py");
            return 1;
        }
        if (!File.Exists(patch))
        {
            Fail("缺少补丁 patches\\fix_discard_draft.py");
            return 1;
        }

        var psi = new ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = Quote(script);
        psi.WorkingDirectory = root;
        psi.UseShellExecute = false;
        psi.RedirectStandardOutput = false;
        psi.RedirectStandardError = false;
        // python.exe 是控制台程序，直接起会弹出一个黑窗口。
        // CreateNoWindow 只是不显示这个控制台，进程照样有 stdout，
        // 换成 pythonw.exe 反而会让 sys.stdout 变成 None，print 会直接报错。
        psi.CreateNoWindow = true;

        // 环境变量（与 启动_修复版.bat 一致）
        SetEnv(psi, "PYTHONUTF8", "1");
        SetEnv(psi, "PYTHONPATH",
            Path.Combine(root, "patches") + ";" +
            Path.Combine(root, "_tools", "runtime_site") + ";" +
            Path.Combine(root, "_internal") + ";" +
            root);
        SetEnv(psi, "TCL_LIBRARY", Path.Combine(root, "_internal", "_tcl_data"));
        SetEnv(psi, "TK_LIBRARY", Path.Combine(root, "_internal", "_tk_data"));

        // 本机 7892 代理可用才注入（蓝海加速器等常见混合端口）
        if (TcpOpen("127.0.0.1", 7892, 300))
        {
            SetEnv(psi, "HTTP_PROXY", "http://127.0.0.1:7892");
            SetEnv(psi, "HTTPS_PROXY", "http://127.0.0.1:7892");
        }

        try
        {
            using (Process p = Process.Start(psi))
            {
                if (p == null)
                {
                    Fail("无法启动 python 进程");
                    return 1;
                }
                p.WaitForExit();
                return p.ExitCode;
            }
        }
        catch (Exception ex)
        {
            Fail("启动失败: " + ex.Message + "\n请查看 logs\\publisher.log");
            return 1;
        }
    }

    static void SetEnv(ProcessStartInfo psi, string key, string value)
    {
        if (psi.EnvironmentVariables.ContainsKey(key))
            psi.EnvironmentVariables[key] = value;
        else
            psi.EnvironmentVariables.Add(key, value);
    }

    static bool TcpOpen(string host, int port, int timeoutMs)
    {
        try
        {
            using (var c = new TcpClient())
            {
                IAsyncResult ar = c.BeginConnect(host, port, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(timeoutMs))
                    return false;
                c.EndConnect(ar);
                return c.Connected;
            }
        }
        catch { return false; }
    }

    static string Quote(string path)
    {
        if (string.IsNullOrEmpty(path)) return "\"\"";
        if (path.StartsWith("\"") && path.EndsWith("\"")) return path;
        return "\"" + path.Replace("\"", "\\\"") + "\"";
    }

    static void Fail(string msg)
    {
        try
        {
            // 有 GUI 时弹窗；否则写控制台
            System.Windows.Forms.MessageBox.Show(
                msg,
                "TikTokPublisher",
                System.Windows.Forms.MessageBoxButtons.OK,
                System.Windows.Forms.MessageBoxIcon.Error);
        }
        catch
        {
            Console.Error.WriteLine(msg);
            try { Console.ReadKey(true); } catch { }
        }
    }
}

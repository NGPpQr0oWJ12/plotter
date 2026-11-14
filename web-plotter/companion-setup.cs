using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("Xiao Plotter Companion Setup")]

internal static class CompanionSetup
{
    private const string Caption = "Помощник Plotter";
    private const string OperatorUrl = "http://127.0.0.1:8767/";

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
            using (var installation = new Mutex(false, @"Local\XiaoPlotterCompanionSetup"))
            {
                if (!installation.WaitOne(0))
                {
                    MessageBox.Show("Установщик уже открыт.", Caption, MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
                try { Install(); }
                finally { installation.ReleaseMutex(); }
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, Caption, MessageBoxButtons.OK, MessageBoxIcon.Error);
            Environment.ExitCode = 1;
        }
    }

    private static void Install()
    {
        string directory = Path.Combine(Path.GetTempPath(), "Plotter-Setup-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        try
        {
            using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("plotter.payload"))
            using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
            {
                archive.ExtractToDirectory(directory);
            }
            string origin = File.ReadAllText(Path.Combine(directory, "origin.txt")).Trim();
            using (var progress = new Form())
            {
                progress.Text = Caption;
                progress.StartPosition = FormStartPosition.CenterScreen;
                progress.FormBorderStyle = FormBorderStyle.FixedDialog;
                progress.ControlBox = false;
                progress.Width = 445;
                progress.Height = 100;
                var label = new Label { Dock = DockStyle.Fill, TextAlign = System.Drawing.ContentAlignment.MiddleCenter };
                label.Text = "Установка помощника Plotter. Следуйте подсказкам.";
                progress.Controls.Add(label);
                progress.Show();
                if (RunInstaller(directory, origin) == 2)
                    return;
                progress.Hide();
            }
            MessageBox.Show("Помощник установлен и запущен. Он будет запускаться при входе в Windows.\n"
                + "Панель доступна по адресу " + OperatorUrl,
                Caption, MessageBoxButtons.OK, MessageBoxIcon.Information);
            try { Process.Start(new ProcessStartInfo(OperatorUrl) { UseShellExecute = true }); }
            catch (System.ComponentModel.Win32Exception)
            {
                MessageBox.Show("Откройте в браузере " + OperatorUrl, Caption, MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
        }
        finally
        {
            try { Directory.Delete(directory, true); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    private static int RunInstaller(string directory, string origin)
    {
        string script = Path.Combine(directory, "install-companion.ps1");
        string powershell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
        var info = new ProcessStartInfo(powershell)
        {
            Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -STA -File \""
                + script + "\" -Graphical -CentralUrl \"" + origin + "\"",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            StandardErrorEncoding = Encoding.UTF8,
            StandardOutputEncoding = Encoding.UTF8,
            WorkingDirectory = directory
        };
        info.EnvironmentVariables["PLOTTER_SETUP_DIRECTORY"] = AppDomain.CurrentDomain.BaseDirectory;
        using (var process = Process.Start(info))
        {
            var errors = new StringBuilder();
            process.ErrorDataReceived += (sender, args) => { if (args.Data != null) errors.AppendLine(args.Data); };
            process.OutputDataReceived += (sender, args) => { };
            process.BeginErrorReadLine();
            process.BeginOutputReadLine();
            while (!process.WaitForExit(100))
                Application.DoEvents();
            process.WaitForExit();
            if (process.ExitCode == 0 || process.ExitCode == 2)
                return process.ExitCode;
            throw new InvalidOperationException("Установка не завершилась.\n" + errors.ToString().Trim());
        }
    }
}

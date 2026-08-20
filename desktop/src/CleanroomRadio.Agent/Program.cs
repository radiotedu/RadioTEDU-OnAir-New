using System.Windows.Forms;

namespace CleanroomRadio.Desktop.Agent;

internal static class Program
{
    [STAThread]
    private static int Main()
    {
        Application.SetHighDpiMode(HighDpiMode.SystemAware);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        try
        {
            Application.Run(new TrayApplicationContext());
            return 0;
        }
        catch (Exception exception)
        {
            MessageBox.Show(
                $"RadioTEDU OnAir could not start.\n\n{exception.Message}",
                "RadioTEDU OnAir",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return 1;
        }
    }
}

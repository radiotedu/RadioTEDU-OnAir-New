namespace CleanroomRadio.Desktop.Shell;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        Application.SetHighDpiMode(HighDpiMode.SystemAware);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        ShellEnvironmentSettings settings;
        try
        {
            settings = ShellEnvironmentSettings.FromCurrentProcessEnvironment(args);
        }
        catch (Exception exception)
        {
            ShowStartupError(
                ShellProductIdentity.DisplayName,
                $"{ShellProductIdentity.DisplayName} could not start.\n\n{exception.Message}");
            return 1;
        }

        using var onAirInstance = OnAirSingleInstance.Acquire();
        if (!onAirInstance.IsPrimary)
        {
            return 0;
        }

        return ShellApplication.Run(
            initialize: () => { },
            createForm: () => new MainForm(settings),
            runForm: Application.Run,
            showError: ShowStartupError);
    }

    private static void ShowStartupError(string title, string message)
    {
        MessageBox.Show(
            message,
            title,
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);
    }
}

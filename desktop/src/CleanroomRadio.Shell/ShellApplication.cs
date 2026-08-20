using System.Windows.Forms;

namespace CleanroomRadio.Desktop.Shell;

public static class ShellApplication
{
    public static int Run(
        Action initialize,
        Func<Form> createForm,
        Action<Form> runForm,
        Action<string, string> showError)
    {
        ArgumentNullException.ThrowIfNull(initialize);
        ArgumentNullException.ThrowIfNull(createForm);
        ArgumentNullException.ThrowIfNull(runForm);
        ArgumentNullException.ThrowIfNull(showError);

        try
        {
            initialize();
            using var form = createForm();
            runForm(form);
            return 0;
        }
        catch (Exception exception)
        {
            showError(
                ShellProductIdentity.DisplayName,
                $"{ShellProductIdentity.DisplayName} could not start.\n\n{exception.Message}");
            return 1;
        }
    }
}

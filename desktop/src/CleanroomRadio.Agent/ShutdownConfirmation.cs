using System.Windows.Forms;

namespace CleanroomRadio.Desktop.Agent;

public sealed record ShutdownConfirmationPrompt(string Title, string Message);

public static class ShutdownConfirmation
{
    public static ShutdownConfirmationPrompt CreateStopBroadcastAndExitPrompt()
    {
        return new ShutdownConfirmationPrompt(
            "Stop Broadcast and Exit",
            "This will stop the broadcast and exit RadioTEDU OnAir. The window and background agent will close. Do you want to continue?");
    }
}

public interface IShutdownConfirmation
{
    bool ConfirmStopBroadcastAndExit(IWin32Window? owner = null);
}

public sealed class MessageBoxShutdownConfirmation : IShutdownConfirmation
{
    public bool ConfirmStopBroadcastAndExit(IWin32Window? owner = null)
    {
        var prompt = ShutdownConfirmation.CreateStopBroadcastAndExitPrompt();

        return MessageBox.Show(
            owner,
            prompt.Message,
            prompt.Title,
            MessageBoxButtons.YesNo,
            MessageBoxIcon.Warning,
            MessageBoxDefaultButton.Button2) == DialogResult.Yes;
    }
}

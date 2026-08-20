namespace CleanroomRadio.Desktop.Shell;

public static class ShellClosePolicy
{
    public static bool ShouldHideOnUserClose(CloseIntent requestedIntent, bool hideToTrayEnabled)
    {
        return hideToTrayEnabled && requestedIntent == CloseIntent.HideToTray;
    }
}

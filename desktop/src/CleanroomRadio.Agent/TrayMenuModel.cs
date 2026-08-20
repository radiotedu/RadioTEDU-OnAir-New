namespace CleanroomRadio.Desktop.Agent;

public enum TrayCommand
{
    OpenPanel,
    RestartBackend,
    StopBroadcastAndExit,
}

public sealed record TrayMenuItemModel(string Text, TrayCommand Command);

public sealed record TrayMenuModel(IReadOnlyList<TrayMenuItemModel> Items)
{
    public static TrayMenuModel BuildDefault()
    {
        return new TrayMenuModel(
            new[]
            {
                new TrayMenuItemModel("Open Panel", TrayCommand.OpenPanel),
                new TrayMenuItemModel("Restart Backend", TrayCommand.RestartBackend),
                new TrayMenuItemModel("Stop Broadcast and Exit", TrayCommand.StopBroadcastAndExit),
            });
    }
}

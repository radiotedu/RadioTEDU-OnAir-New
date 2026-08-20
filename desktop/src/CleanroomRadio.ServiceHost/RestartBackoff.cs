namespace CleanroomRadio.ServiceHost;

public sealed class RestartBackoff
{
    private readonly TimeSpan _initialDelay;
    private readonly TimeSpan _maximumDelay;
    private readonly TimeSpan _stableRunThreshold;
    private int _consecutiveFailures;

    public RestartBackoff(
        TimeSpan? initialDelay = null,
        TimeSpan? maximumDelay = null,
        TimeSpan? stableRunThreshold = null)
    {
        _initialDelay = initialDelay ?? TimeSpan.FromSeconds(1);
        _maximumDelay = maximumDelay ?? TimeSpan.FromMinutes(1);
        _stableRunThreshold = stableRunThreshold ?? TimeSpan.FromMinutes(1);
    }

    public TimeSpan RegisterExit(TimeSpan runDuration)
    {
        _consecutiveFailures = runDuration >= _stableRunThreshold ? 0 : Math.Min(_consecutiveFailures + 1, 16);
        var multiplier = 1L << Math.Max(0, Math.Min(_consecutiveFailures - 1, 15));
        var milliseconds = Math.Min(_initialDelay.TotalMilliseconds * multiplier, _maximumDelay.TotalMilliseconds);
        return TimeSpan.FromMilliseconds(milliseconds);
    }
}

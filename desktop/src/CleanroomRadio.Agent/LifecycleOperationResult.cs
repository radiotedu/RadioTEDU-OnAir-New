namespace CleanroomRadio.Desktop.Agent;

public sealed record LifecycleOperationResult(bool Succeeded, string? ErrorMessage)
{
    public static LifecycleOperationResult Success()
    {
        return new LifecycleOperationResult(true, null);
    }

    public static LifecycleOperationResult Failure(string errorMessage)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(errorMessage);
        return new LifecycleOperationResult(false, errorMessage);
    }
}

public sealed record ShutdownOperationResult(bool Succeeded, bool ShouldExit, string? ErrorMessage)
{
    public static ShutdownOperationResult Success()
    {
        return new ShutdownOperationResult(true, true, null);
    }

    public static ShutdownOperationResult Cancelled()
    {
        return new ShutdownOperationResult(false, false, null);
    }

    public static ShutdownOperationResult Failure(string errorMessage)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(errorMessage);
        return new ShutdownOperationResult(false, false, errorMessage);
    }
}

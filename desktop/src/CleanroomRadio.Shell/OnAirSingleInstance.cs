using System.Threading;

namespace CleanroomRadio.Desktop.Shell;

/// <summary>
/// Guarantees that the workstation has only one visible OnAir control window.
/// </summary>
public sealed class OnAirSingleInstance : IDisposable
{
    public const string MutexName = @"Global\RadioTEDU.OnAir.Operator";

    private readonly Mutex _mutex;
    private bool _disposed;

    private OnAirSingleInstance(Mutex mutex, bool isPrimary)
    {
        _mutex = mutex;
        IsPrimary = isPrimary;
    }

    public bool IsPrimary { get; }

    public static OnAirSingleInstance Acquire()
    {
        var mutex = new Mutex(initiallyOwned: true, MutexName, out var createdNew);
        return new OnAirSingleInstance(mutex, createdNew);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        if (IsPrimary)
        {
            _mutex.ReleaseMutex();
        }

        _mutex.Dispose();
    }
}

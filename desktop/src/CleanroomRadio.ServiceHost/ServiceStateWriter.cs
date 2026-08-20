using System.Text.Json;

namespace CleanroomRadio.ServiceHost;

public sealed record ChildProcessState(
    string Id,
    string Status,
    int? ProcessId,
    int RestartCount,
    DateTimeOffset? StartedUtc,
    DateTimeOffset? NextRestartUtc,
    int? LastExitCode);

public sealed record ServiceHostState(
    string ServiceName,
    string Status,
    DateTimeOffset UpdatedUtc,
    IReadOnlyCollection<ChildProcessState> Children);

public sealed class ServiceStateWriter
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };
    private readonly SemaphoreSlim _writeGate = new(1, 1);
    private readonly string _path;

    public ServiceStateWriter(ServiceHostSettings settings)
    {
        Directory.CreateDirectory(settings.StateDirectory);
        _path = System.IO.Path.Combine(settings.StateDirectory, $"supervisor-{SafeFileName(settings.ServiceName)}.json");
    }

    public string Path => _path;

    public async Task WriteAsync(ServiceHostState state, CancellationToken cancellationToken)
    {
        var temporaryPath = $"{_path}.{Guid.NewGuid():N}.tmp";
        var payload = JsonSerializer.Serialize(state, JsonOptions);
        await _writeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await File.WriteAllTextAsync(temporaryPath, payload, cancellationToken).ConfigureAwait(false);
            File.Move(temporaryPath, _path, overwrite: true);
        }
        finally
        {
            _writeGate.Release();
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }
        }
    }

    private static string SafeFileName(string value) => string.Concat(value.Select(character =>
        char.IsLetterOrDigit(character) || character is '.' or '-' or '_' ? character : '_'));
}

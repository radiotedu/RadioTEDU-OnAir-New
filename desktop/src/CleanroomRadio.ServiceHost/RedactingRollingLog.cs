using System.Text;

namespace CleanroomRadio.ServiceHost;

public sealed class RedactingRollingLog
{
    private const long MaximumBytes = 2 * 1024 * 1024;
    private const int RetainedArchives = 5;
    private readonly object _gate = new();
    private readonly string _path;

    public RedactingRollingLog(ServiceHostSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        Directory.CreateDirectory(settings.LogDirectory);
        _path = Path.Combine(settings.LogDirectory, $"supervisor-{SafeFileName(settings.ServiceName)}.log");
    }

    public void Write(string level, string message)
    {
        var line = $"{DateTimeOffset.UtcNow:O} [{level.ToUpperInvariant()}] {SecretRedactor.Redact(message)}{Environment.NewLine}";
        lock (_gate)
        {
            RotateIfNeeded(Encoding.UTF8.GetByteCount(line));
            File.AppendAllText(_path, line, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
    }

    private void RotateIfNeeded(int incomingBytes)
    {
        if (!File.Exists(_path) || new FileInfo(_path).Length + incomingBytes <= MaximumBytes)
        {
            return;
        }

        var oldest = $"{_path}.{RetainedArchives}";
        if (File.Exists(oldest))
        {
            File.Delete(oldest);
        }

        for (var index = RetainedArchives - 1; index >= 1; index--)
        {
            var source = $"{_path}.{index}";
            if (File.Exists(source))
            {
                File.Move(source, $"{_path}.{index + 1}", overwrite: true);
            }
        }

        File.Move(_path, $"{_path}.1", overwrite: true);
    }

    private static string SafeFileName(string value) => string.Concat(value.Select(character =>
        char.IsLetterOrDigit(character) || character is '.' or '-' or '_' ? character : '_'));
}

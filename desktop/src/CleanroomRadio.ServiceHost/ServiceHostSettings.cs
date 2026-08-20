using System.Text.RegularExpressions;

namespace CleanroomRadio.ServiceHost;

public sealed record ManagedProcessDefinition(
    string Id,
    string ExecutablePath,
    string Arguments,
    string WorkingDirectory,
    bool RestartOnExit);

public sealed record ServiceHostSettings(
    string ServiceName,
    string ConfigPath,
    string StateDirectory,
    string LogDirectory)
{
    private static readonly Regex InlineCredentialFlag = new(
        "(?i)(?:^|\\s)--?(?:token|password|passwd|pwd|secret|api[-_]?key)(?:\\s|=|$)",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);

    private static readonly Regex UrlWithUserInfo = new(
        "(?i)\\b[a-z][a-z0-9+.-]*://[^/\\s@]+@",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);

    public static ServiceHostSettings FromCommandLine(IReadOnlyList<string> args)
    {
        var serviceName = ReadOption(args, "--service-name");
        var configPath = ReadOption(args, "--config");
        var programData = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);
        var root = ServiceHostProductIdentity.GetDataRoot(programData);

        return new ServiceHostSettings(
            ValidateServiceName(serviceName),
            Path.GetFullPath(configPath),
            Path.Combine(root, "State", "Supervisor"),
            Path.Combine(root, "Logs", "Supervisor"));
    }

    public static IReadOnlyList<ManagedProcessDefinition> ReadDefinitions(string configPath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(configPath);
        var definitions = new List<ManagedProcessDefinition>();
        var lineNumber = 0;

        foreach (var rawLine in File.ReadLines(configPath))
        {
            lineNumber++;
            var line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith('#') || line.StartsWith(';') || line.StartsWith("//", StringComparison.Ordinal))
            {
                continue;
            }

            var fields = line.Split('|', StringSplitOptions.TrimEntries);
            if (fields.Length != 5)
            {
                throw new InvalidDataException($"Config line {lineNumber} must contain exactly five pipe-delimited fields.");
            }

            if (!bool.TryParse(fields[4], out var restartOnExit))
            {
                throw new InvalidDataException($"Config line {lineNumber} has an invalid restart-on-exit flag.");
            }

            var id = ValidateIdentifier(fields[0], lineNumber);
            ValidateArguments(fields[2], lineNumber);
            var executablePath = Path.GetFullPath(fields[1]);
            var workingDirectory = string.IsNullOrWhiteSpace(fields[3])
                ? Path.GetDirectoryName(executablePath) ?? throw new InvalidDataException($"Config line {lineNumber} has no working directory.")
                : Path.GetFullPath(fields[3]);

            if (!File.Exists(executablePath))
            {
                throw new FileNotFoundException($"Configured executable was not found for process '{id}'.", executablePath);
            }

            if (!Directory.Exists(workingDirectory))
            {
                throw new DirectoryNotFoundException($"Configured working directory was not found for process '{id}'.");
            }

            definitions.Add(new ManagedProcessDefinition(id, executablePath, fields[2], workingDirectory, restartOnExit));
        }

        if (definitions.Count == 0)
        {
            throw new InvalidDataException("The service configuration contains no managed process definitions.");
        }

        if (definitions.Select(definition => definition.Id).Distinct(StringComparer.OrdinalIgnoreCase).Count() != definitions.Count)
        {
            throw new InvalidDataException("Managed process identifiers must be unique.");
        }

        return definitions;
    }

    private static string ReadOption(IReadOnlyList<string> args, string option)
    {
        for (var index = 0; index < args.Count - 1; index++)
        {
            if (string.Equals(args[index], option, StringComparison.OrdinalIgnoreCase))
            {
                return args[index + 1];
            }
        }

        throw new ArgumentException($"Required option '{option}' is missing.");
    }

    private static string ValidateServiceName(string value)
    {
        if (!Regex.IsMatch(value, "^[A-Za-z0-9_.-]{1,128}$", RegexOptions.CultureInvariant))
        {
            throw new ArgumentException("Service name contains unsupported characters.");
        }

        return value;
    }

    private static string ValidateIdentifier(string value, int lineNumber)
    {
        if (!Regex.IsMatch(value, "^[A-Za-z0-9_.-]{1,80}$", RegexOptions.CultureInvariant))
        {
            throw new InvalidDataException($"Config line {lineNumber} has an invalid process identifier.");
        }

        return value;
    }

    private static void ValidateArguments(string value, int lineNumber)
    {
        if (InlineCredentialFlag.IsMatch(value) || UrlWithUserInfo.IsMatch(value))
        {
            throw new InvalidDataException($"Config line {lineNumber} contains a forbidden inline credential.");
        }
    }
}

using System.Text.Json;

namespace CleanroomRadio.Desktop.Shell;

public sealed record NativePickerRequest(
    string RequestId,
    string Kind,
    string InitialPath,
    string Description);

public sealed record NativePickerResponse(
    string RequestId,
    bool Selected,
    string Path,
    string Error = "");

public static class NativePickerBridge
{
    public const string RequestType = ShellProductIdentity.PickerRequestType;
    public const string ResponseType = ShellProductIdentity.PickerResponseType;

    public static bool IsTrustedSource(Uri panelUri, string? source)
    {
        ArgumentNullException.ThrowIfNull(panelUri);
        return Uri.TryCreate(source, UriKind.Absolute, out var sourceUri)
            && Uri.Compare(
                panelUri,
                sourceUri,
                UriComponents.SchemeAndServer,
                UriFormat.SafeUnescaped,
                StringComparison.OrdinalIgnoreCase) == 0;
    }

    public static bool TryParseRequest(string? json, out NativePickerRequest? request)
    {
        request = null;
        if (string.IsNullOrWhiteSpace(json) || json.Length > 4096)
        {
            return false;
        }

        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || ReadString(root, "type") != RequestType)
            {
                return false;
            }

            var requestId = ReadString(root, "requestId");
            var kind = ReadString(root, "kind").ToLowerInvariant();
            if (!IsSafeRequestId(requestId) || kind is not ("folder" or "file"))
            {
                return false;
            }

            var initialPath = Limit(ReadString(root, "initialPath"), 2048);
            var description = Limit(ReadString(root, "description").Trim(), 200);
            if (description.Length == 0)
            {
                description = kind == "folder"
                    ? $"Select a {ShellProductIdentity.DisplayName} folder"
                    : $"Select a {ShellProductIdentity.DisplayName} file";
            }

            request = new NativePickerRequest(requestId, kind, initialPath, description);
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    public static string CreateResponseJson(NativePickerResponse response)
    {
        ArgumentNullException.ThrowIfNull(response);
        return JsonSerializer.Serialize(new
        {
            type = ResponseType,
            requestId = response.RequestId,
            selected = response.Selected,
            path = response.Path,
            error = response.Error,
        });
    }

    private static string ReadString(JsonElement root, string propertyName)
    {
        return root.TryGetProperty(propertyName, out var value)
            && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? string.Empty
                : string.Empty;
    }

    private static string Limit(string value, int maximumLength)
    {
        return value.Length <= maximumLength ? value : value[..maximumLength];
    }

    private static bool IsSafeRequestId(string value)
    {
        return value.Length is > 0 and <= 100
            && value.All(character => char.IsAsciiLetterOrDigit(character)
                || character is '-' or '_' or '.' or ':');
    }
}

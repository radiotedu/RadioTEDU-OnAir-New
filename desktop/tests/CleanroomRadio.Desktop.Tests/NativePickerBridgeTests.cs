using System.Text.Json;
using CleanroomRadio.Desktop.Shell;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class NativePickerBridgeTests
{
    [Fact]
    public void TryParseRequest_AcceptsBoundedFolderRequest()
    {
        var json = """
            {
              "type": "radiotedu-picker-request",
              "requestId": "picker-123-1",
              "kind": "folder",
              "initialPath": "D:\\Radio\\Music",
              "description": "Select station music"
            }
            """;

        var parsed = NativePickerBridge.TryParseRequest(json, out var request);

        Assert.True(parsed);
        Assert.NotNull(request);
        Assert.Equal("picker-123-1", request.RequestId);
        Assert.Equal("folder", request.Kind);
        Assert.Equal("D:\\Radio\\Music", request.InitialPath);
        Assert.Equal("Select station music", request.Description);
    }

    [Theory]
    [InlineData("{}")]
    [InlineData("{\"type\":\"untrusted\",\"requestId\":\"one\",\"kind\":\"folder\"}")]
    [InlineData("{\"type\":\"radiotedu-picker-request\",\"requestId\":\"bad id\",\"kind\":\"folder\"}")]
    [InlineData("{\"type\":\"radiotedu-picker-request\",\"requestId\":\"one\",\"kind\":\"command\"}")]
    public void TryParseRequest_RejectsMalformedOrUnsafeRequests(string json)
    {
        Assert.False(NativePickerBridge.TryParseRequest(json, out var request));
        Assert.Null(request);
    }

    [Fact]
    public void IsTrustedSource_RequiresTheConfiguredPanelOrigin()
    {
        var panel = new Uri("http://127.0.0.1:8100/app");

        Assert.True(NativePickerBridge.IsTrustedSource(
            panel,
            "http://127.0.0.1:8100/app#media"));
        Assert.False(NativePickerBridge.IsTrustedSource(
            panel,
            "https://example.org/app"));
        Assert.False(NativePickerBridge.IsTrustedSource(panel, "not-a-url"));
    }

    [Fact]
    public void CreateResponseJson_UsesTheStableWebViewContract()
    {
        var json = NativePickerBridge.CreateResponseJson(
            new NativePickerResponse("picker-1", true, "D:\\Radio\\Music"));
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;

        Assert.Equal("radiotedu-picker-response", root.GetProperty("type").GetString());
        Assert.Equal("picker-1", root.GetProperty("requestId").GetString());
        Assert.True(root.GetProperty("selected").GetBoolean());
        Assert.Equal("D:\\Radio\\Music", root.GetProperty("path").GetString());
    }
}

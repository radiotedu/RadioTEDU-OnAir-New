using System.IO;
using System.Reflection;
using System.Windows.Forms;
using CleanroomRadio.Desktop.Shell;
using Microsoft.Web.WebView2.WinForms;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class ShellBootstrapperTests
{
    [Fact]
    public void Run_ShowsStartupErrorAndReturnsOne_WhenInitializationFails()
    {
        string? shownTitle = null;
        string? shownMessage = null;

        var exitCode = ShellApplication.Run(
            initialize: () => throw new InvalidOperationException("Configuration failed."),
            createForm: () => throw new InvalidOperationException("Should not run."),
            runForm: _ => throw new InvalidOperationException("Should not run."),
            showError: (title, message) =>
            {
                shownTitle = title;
                shownMessage = message;
            });

        Assert.Equal(1, exitCode);
        Assert.Equal("RadioTEDU OnAir", shownTitle);
        Assert.Contains("could not start", shownMessage);
        Assert.Contains("Configuration failed.", shownMessage);
    }

    [Fact]
    public void Run_ShowsStartupErrorAndReturnsOne_WhenFormConstructionFails()
    {
        string? shownTitle = null;
        string? shownMessage = null;

        var exitCode = ShellApplication.Run(
            initialize: () => { },
            createForm: () => throw new FormatException("RADIOTEDU_ONAIR_PORT must be an integer."),
            runForm: _ => throw new InvalidOperationException("Should not run."),
            showError: (title, message) =>
            {
                shownTitle = title;
                shownMessage = message;
            });

        Assert.Equal(1, exitCode);
        Assert.Equal("RadioTEDU OnAir", shownTitle);
        Assert.Contains("could not start", shownMessage);
        Assert.Contains("RADIOTEDU_ONAIR_PORT", shownMessage);
    }

    [Fact]
    public void Run_ReturnsZeroAndDoesNotShowError_WhenStartupSucceeds()
    {
        var errorShown = false;

        var exitCode = ShellApplication.Run(
            initialize: () => { },
            createForm: () => new Form(),
            runForm: form =>
            {
                Assert.IsType<Form>(form);
            },
            showError: (_, _) => errorShown = true);

        Assert.Equal(0, exitCode);
        Assert.False(errorShown);
    }

    [Fact]
    public void MainForm_ConfiguresWebViewUserDataFolderBeforeLoad()
    {
        var original = Environment.GetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER");

        try
        {
            Environment.SetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER", null);

            using var form = new MainForm(new Uri("http://127.0.0.1:8100/app"));
            var field = typeof(MainForm).GetField("_webView", BindingFlags.Instance | BindingFlags.NonPublic);

            Assert.NotNull(field);

            var webView = Assert.IsType<WebView2>(field!.GetValue(form));
            var expectedPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "RadioTEDU OnAir",
                "WebView2");

            Assert.NotNull(webView.CreationProperties);
            Assert.Equal(expectedPath, webView.CreationProperties!.UserDataFolder);
            Assert.Equal(expectedPath, Environment.GetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER"));
        }
        finally
        {
            Environment.SetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER", original);
        }
    }
}

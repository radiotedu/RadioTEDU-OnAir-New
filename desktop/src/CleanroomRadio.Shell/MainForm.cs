using System.IO;
using System.Net.Http;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace CleanroomRadio.Desktop.Shell;

public sealed class MainForm : Form
{
    private readonly WebView2 _webView;
    private readonly Uri _panelUri;
    private readonly ShellNavigationPolicy _navigationPolicy;
    private readonly string _webViewUserDataFolder;
    private CloseIntent _closeIntent = CloseIntent.ExitApplication;

    public MainForm()
        : this(ShellEnvironmentSettings.FromCurrentProcessEnvironment())
    {
    }

    public MainForm(Uri panelUri)
        : this(new ShellEnvironmentSettings(panelUri, ShellLaunchMode.Operator))
    {
    }

    public MainForm(ShellEnvironmentSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        _panelUri = settings.PanelUri ?? throw new ArgumentNullException(nameof(settings));
        _navigationPolicy = new ShellNavigationPolicy(_panelUri, fixedNavigation: false);
        _webViewUserDataFolder = GetWebViewUserDataFolder();
        Directory.CreateDirectory(_webViewUserDataFolder);
        Environment.SetEnvironmentVariable(
            "WEBVIEW2_USER_DATA_FOLDER",
            _webViewUserDataFolder,
            EnvironmentVariableTarget.Process);

        Text = ShellProductIdentity.DisplayName;
        Width = 1280;
        Height = 800;
        StartPosition = FormStartPosition.CenterScreen;
        _webView = new WebView2
        {
            Dock = DockStyle.Fill,
            CreationProperties = new CoreWebView2CreationProperties
            {
                UserDataFolder = _webViewUserDataFolder,
            },
        };

        Controls.Add(_webView);
        Load += HandleLoad;
        FormClosing += HandleFormClosing;
    }

    public bool HideToTrayEnabled { get; set; }

    public void RequestClose(CloseIntent intent)
    {
        _closeIntent = intent;
        Close();
    }

    private async void HandleLoad(object? sender, EventArgs e)
    {
        try
        {
            await WaitForBackendAsync();
            await InitializeWebViewAsync();
        }
        catch (Exception exception)
        {
            HandleWebViewInitializationFailure(exception);
        }
    }

    private void HandleFormClosing(object? sender, FormClosingEventArgs e)
    {
        if (e.CloseReason != CloseReason.UserClosing)
        {
            return;
        }

        if (!ShellClosePolicy.ShouldHideOnUserClose(_closeIntent, HideToTrayEnabled))
        {
            return;
        }

        e.Cancel = true;
        Hide();
    }

    private static string GetWebViewUserDataFolder()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(
            localAppData,
            ShellProductIdentity.WebViewDataDirectory,
            ShellProductIdentity.WebViewDataLeaf);
    }

    private async Task WaitForBackendAsync()
    {
        var handler = new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            UseProxy = false,
        };
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(2) };
        var authority = _panelUri.GetLeftPart(UriPartial.Authority);
        var healthUri = new Uri($"{authority}/api/health/live");

        for (var attempt = 0; attempt < 120; attempt++)
        {
            try
            {
                using var response = await client.GetAsync(healthUri).ConfigureAwait(true);
                if (response.IsSuccessStatusCode)
                {
                    await using var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(true);
                    using var payload = await JsonDocument.ParseAsync(stream).ConfigureAwait(true);
                    if (payload.RootElement.TryGetProperty("product", out var product)
                        && string.Equals(
                            product.GetString(),
                            ShellProductIdentity.DisplayName,
                            StringComparison.Ordinal))
                    {
                        return;
                    }
                }
            }
            catch (HttpRequestException)
            {
            }
            catch (TaskCanceledException)
            {
            }

            await Task.Delay(TimeSpan.FromMilliseconds(500)).ConfigureAwait(true);
        }

        throw new InvalidOperationException(
            $"The local {ShellProductIdentity.DisplayName} supervisor did not become available " +
            "within 60 seconds. Open Windows Services and verify that " +
            $"{ShellProductIdentity.SupervisorServiceName} is running.");
    }

    private async Task InitializeWebViewAsync()
    {
        var env = await CoreWebView2Environment.CreateAsync(
            userDataFolder: _webViewUserDataFolder);

        await _webView.EnsureCoreWebView2Async(env);
        _webView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
        _webView.CoreWebView2.Settings.AreDevToolsEnabled = false;
        _webView.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = false;
        _webView.CoreWebView2.Settings.IsStatusBarEnabled = false;
        _webView.CoreWebView2.NavigationStarting += HandleNavigationStarting;
        _webView.CoreWebView2.NewWindowRequested += (_, eventArgs) => eventArgs.Handled = true;
        _webView.CoreWebView2.WebMessageReceived += HandleWebMessageReceived;
        _webView.Source = _panelUri;
    }

    private void HandleWebMessageReceived(
        object? sender,
        CoreWebView2WebMessageReceivedEventArgs eventArgs)
    {
        if (_webView.CoreWebView2 is null
            || !NativePickerBridge.IsTrustedSource(_panelUri, eventArgs.Source)
            || !NativePickerBridge.TryParseRequest(eventArgs.WebMessageAsJson, out var request)
            || request is null)
        {
            return;
        }

        NativePickerResponse response;
        try
        {
            response = ShowNativePicker(request);
        }
        catch (Exception ex)
        {
            System.Diagnostics.Trace.TraceError(
                $"{ShellProductIdentity.DisplayName} native picker failed: {{0}}",
                ex.Message);
            response = new NativePickerResponse(
                request.RequestId,
                false,
                string.Empty,
                "The desktop folder window could not be opened. Enter the absolute path instead.");
        }

        _webView.CoreWebView2.PostWebMessageAsJson(
            NativePickerBridge.CreateResponseJson(response));
    }

    private NativePickerResponse ShowNativePicker(NativePickerRequest request)
    {
        if (request.Kind == "folder")
        {
            using var dialog = new FolderBrowserDialog
            {
                Description = request.Description,
                ShowNewFolderButton = true,
                UseDescriptionForTitle = true,
                SelectedPath = Directory.Exists(request.InitialPath)
                    ? request.InitialPath
                    : string.Empty,
            };
            var selected = dialog.ShowDialog(this) == DialogResult.OK
                && !string.IsNullOrWhiteSpace(dialog.SelectedPath);
            return new NativePickerResponse(
                request.RequestId,
                selected,
                selected ? dialog.SelectedPath : string.Empty);
        }

        using var fileDialog = new OpenFileDialog
        {
            Title = request.Description,
            Filter = "Audio files|*.mp3;*.wav;*.flac;*.m4a;*.aac;*.ogg;*.opus|All files (*.*)|*.*",
            CheckFileExists = true,
            Multiselect = false,
        };
        if (File.Exists(request.InitialPath))
        {
            fileDialog.InitialDirectory = Path.GetDirectoryName(request.InitialPath) ?? string.Empty;
            fileDialog.FileName = Path.GetFileName(request.InitialPath);
        }
        else if (Directory.Exists(request.InitialPath))
        {
            fileDialog.InitialDirectory = request.InitialPath;
        }

        var fileSelected = fileDialog.ShowDialog(this) == DialogResult.OK
            && !string.IsNullOrWhiteSpace(fileDialog.FileName);
        return new NativePickerResponse(
            request.RequestId,
            fileSelected,
            fileSelected ? fileDialog.FileName : string.Empty);
    }

    private void HandleNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs eventArgs)
    {
        if (Uri.TryCreate(eventArgs.Uri, UriKind.Absolute, out var target) && _navigationPolicy.Allows(target))
        {
            return;
        }

        eventArgs.Cancel = true;
    }

    private void HandleWebViewInitializationFailure(Exception exception)
    {
        MessageBox.Show(
            this,
            $"{ShellProductIdentity.DisplayName} could not start the embedded browser control.\n\n" +
            exception.Message,
            Text,
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);

        _closeIntent = CloseIntent.ExitApplication;
        Close();
    }
}

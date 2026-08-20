using System.Net;
using System.Net.Http;

namespace CleanroomRadio.Desktop.Common;

public static class HealthProbe
{
    private static readonly TimeSpan DefaultAttemptTimeout = TimeSpan.FromSeconds(1);

    public static async Task<bool> WaitForHealthyAsync(
        Uri baseAddress,
        HttpClient? httpClient = null,
        int retries = 30,
        TimeSpan? delay = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(baseAddress);
        if (retries < 1)
        {
            throw new ArgumentOutOfRangeException(nameof(retries));
        }

        var interval = delay ?? TimeSpan.FromMilliseconds(500);
        var ownsClient = httpClient is null;
        httpClient ??= new HttpClient
        {
            Timeout = DefaultAttemptTimeout,
        };

        try
        {
            var healthUri = BuildHealthUri(baseAddress);

            for (var attempt = 0; attempt < retries; attempt++)
            {
                try
                {
                    using var response = await httpClient.GetAsync(healthUri, cancellationToken).ConfigureAwait(false);
                    if (response.StatusCode == HttpStatusCode.OK)
                    {
                        return true;
                    }
                }
                catch (HttpRequestException)
                {
                }
                catch (TaskCanceledException) when (!cancellationToken.IsCancellationRequested)
                {
                }

                if (attempt < retries - 1 && interval > TimeSpan.Zero)
                {
                    await Task.Delay(interval, cancellationToken).ConfigureAwait(false);
                }
            }

            return false;
        }
        finally
        {
            if (ownsClient)
            {
                httpClient.Dispose();
            }
        }
    }

    private static Uri BuildHealthUri(Uri baseAddress)
    {
        var builder = new UriBuilder(baseAddress);
        if (!builder.Path.EndsWith("/", StringComparison.Ordinal))
        {
            builder.Path += "/";
        }

        return new Uri(builder.Uri, "api/health/ready");
    }
}

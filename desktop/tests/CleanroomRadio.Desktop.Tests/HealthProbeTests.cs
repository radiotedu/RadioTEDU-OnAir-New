using System.Net;
using System.Net.Http;
using System.Diagnostics;
using System.Net.Sockets;
using CleanroomRadio.Desktop.Common;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class HealthProbeTests
{
    [Fact]
    public async Task WaitForHealthyAsync_RetriesUntilReadinessProbeReturnsOk()
    {
        var attempts = 0;
        using var client = new HttpClient(new SequencedHealthHandler(() =>
        {
            attempts++;
            return attempts < 3
                ? HttpStatusCode.ServiceUnavailable
                : HttpStatusCode.OK;
        }));

        var ready = await HealthProbe.WaitForHealthyAsync(
            new Uri("http://127.0.0.1:8100/panel/"),
            client,
            retries: 5,
            delay: TimeSpan.Zero);

        Assert.True(ready);
        Assert.Equal(3, attempts);
    }

    [Fact]
    public async Task WaitForHealthyAsync_DefaultClient_BoundsEachAttemptWhenProbeDoesNotRespond()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var endpoint = (IPEndPoint)listener.LocalEndpoint;
        using var cts = new CancellationTokenSource();
        var serverTask = Task.Run(async () =>
        {
            try
            {
                using var socket = await listener.AcceptTcpClientAsync(cts.Token);
                using var stream = socket.GetStream();
                var buffer = new byte[1024];
                _ = await stream.ReadAsync(buffer, cts.Token);
                await Task.Delay(Timeout.InfiniteTimeSpan, cts.Token);
            }
            catch (OperationCanceledException)
            {
            }
        });

        var started = Stopwatch.StartNew();

        var ready = await HealthProbe.WaitForHealthyAsync(
            new Uri($"http://127.0.0.1:{endpoint.Port}/"),
            retries: 1,
            delay: TimeSpan.Zero);

        started.Stop();
        cts.Cancel();
        listener.Stop();
        await serverTask;

        Assert.False(ready);
        Assert.True(started.Elapsed < TimeSpan.FromSeconds(3));
    }

    private sealed class SequencedHealthHandler : HttpMessageHandler
    {
        private readonly Func<HttpStatusCode> _nextStatus;

        public SequencedHealthHandler(Func<HttpStatusCode> nextStatus)
        {
            _nextStatus = nextStatus;
        }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            Assert.Equal(HttpMethod.Get, request.Method);
            Assert.Equal(new Uri("http://127.0.0.1:8100/panel/api/health/ready"), request.RequestUri);

            var response = new HttpResponseMessage(_nextStatus());
            return Task.FromResult(response);
        }
    }
}

using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace CleanroomRadio.ServiceHost;

internal static class Program
{
    public static async Task<int> Main(string[] args)
    {
        try
        {
            using var singleInstance = new Mutex(
                initiallyOwned: true,
                ServiceHostProductIdentity.SupervisorMutexName,
                out var createdNew);
            if (!createdNew)
            {
                Console.Error.WriteLine(
                    $"The {ServiceHostProductIdentity.DisplayName} supervisor is already running.");
                return 2;
            }

            var settings = ServiceHostSettings.FromCommandLine(args);
            var builder = Host.CreateApplicationBuilder(args);
            builder.Services.AddWindowsService(options => options.ServiceName = settings.ServiceName);
            builder.Services.AddSingleton(settings);
            builder.Services.AddSingleton<RedactingRollingLog>();
            builder.Services.AddHostedService<ServiceSupervisor>();
            builder.Logging.ClearProviders();

            using var host = builder.Build();
            await host.RunAsync().ConfigureAwait(false);
            singleInstance.ReleaseMutex();
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(SecretRedactor.Redact(exception.Message));
            return 1;
        }
    }
}

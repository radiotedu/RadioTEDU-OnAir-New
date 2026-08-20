namespace CleanroomRadio.ServiceHost;

internal static class ServiceHostProductIdentity
{
    public const string DisplayName = "RadioTEDU OnAir";
    public const string DataVendorDirectory = "RadioTEDU";
    public const string? DataProductDirectory = "OnAir";
    public const string SupervisorMutexName = @"Global\RadioTEDU.OnAir.Supervisor";

    public static string GetDataRoot(string programData)
    {
        return Path.Combine(programData, DataVendorDirectory, DataProductDirectory!);
    }
}

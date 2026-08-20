namespace CleanroomRadio.Desktop.Shell;

public sealed class ShellNavigationPolicy
{
    private readonly Uri _fixedUri;

    public ShellNavigationPolicy(Uri fixedUri, bool fixedNavigation)
    {
        _fixedUri = fixedUri ?? throw new ArgumentNullException(nameof(fixedUri));
        FixedNavigation = fixedNavigation;
    }

    public bool FixedNavigation { get; }

    public bool Allows(Uri target)
    {
        ArgumentNullException.ThrowIfNull(target);
        if (!string.IsNullOrEmpty(target.UserInfo)
            || !string.Equals(target.Scheme, _fixedUri.Scheme, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(target.Host, _fixedUri.Host, StringComparison.OrdinalIgnoreCase)
            || target.Port != _fixedUri.Port)
        {
            return false;
        }

        return !FixedNavigation || Uri.Compare(
            _fixedUri,
            target,
            UriComponents.PathAndQuery,
            UriFormat.SafeUnescaped,
            StringComparison.OrdinalIgnoreCase) == 0;
    }
}

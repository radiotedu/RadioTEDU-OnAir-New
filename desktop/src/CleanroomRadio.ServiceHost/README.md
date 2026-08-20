# RadioTEDU OnAir Supervisor

The product-specific supervisor executable is the Windows SCM supervisor for the local backend.

The host accepts exactly:

```text
--service-name RadioTEDU.OnAir.Supervisor --config "C:\ProgramData\RadioTEDU\OnAir\Services\RadioTEDU.OnAir.Supervisor.services"
```

Each non-comment configuration line is compatible with the legacy five-field shape:

```text
id|executable-path|arguments|working-directory|restart-on-exit
```

Every valid row starts once. `restart-on-exit=true` restarts an exited child with a 1s-to-60s bounded exponential backoff; `false` leaves that child reported as `Exited`. The host kills entire child process trees on SCM stop and writes only redacted logs. It exposes a secret-free state snapshot at:

```text
C:\ProgramData\RadioTEDU\OnAir\State\Supervisor\supervisor-<service-name>.json
```

Each product installer registers and updates its own service, applies restart recovery, and starts it only after protected data ACLs are in place.

Arguments are fail-closed: inline `token`, `password`, `secret`, or `api-key` flags (including a following value), and URLs containing userinfo are rejected without repeating the argument. Use secret-free arguments and a separately protected configuration-file reference. Providing secrets to the child remains an explicit deployment prerequisite outside this host configuration.

The installer disables ACL inheritance on shared product data and grants access only to LocalSystem, built-in Administrators, and the product-specific supervisor virtual account.

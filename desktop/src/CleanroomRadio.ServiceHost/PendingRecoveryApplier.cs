using System.Security.Cryptography;
using System.Text.Json;

namespace CleanroomRadio.ServiceHost;

public sealed record PendingRecoveryPlan(
    int Schema,
    string PlanId,
    string SourceDatabase,
    string SourceDatabaseSha256,
    string TargetDatabase,
    string BackupDatabase,
    string? SourceCredentialVault,
    string? SourceCredentialVaultSha256,
    string? TargetCredentialVault,
    string? BackupCredentialVault,
    bool DeleteCredentialTarget,
    string? OriginPlanId = null);

public sealed record RecoveryApplyResult(bool Applied, string PlanId, string EvidencePath, string Error);

public static class PendingRecoveryApplier
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = false,
    };

    public static RecoveryApplyResult TryApply(ServiceHostSettings settings, RedactingRollingLog log)
    {
        var stateRoot = Directory.GetParent(settings.StateDirectory)?.FullName
            ?? throw new InvalidOperationException("Supervisor state path has no parent.");
        var commonRoot = Directory.GetParent(stateRoot)?.FullName
            ?? throw new InvalidOperationException("Supervisor state path is outside the product root.");
        var recoveryRoot = Path.Combine(stateRoot, "Recovery");
        var pendingPath = Path.Combine(recoveryRoot, "pending.json");
        if (!File.Exists(pendingPath))
        {
            return new RecoveryApplyResult(false, string.Empty, string.Empty, string.Empty);
        }

        try
        {
            var result = ApplyPlan(pendingPath, commonRoot);
            log.Write("information", $"Pending recovery plan '{result.PlanId}' was applied before backend startup.");
            return result;
        }
        catch (Exception exception)
        {
            Directory.CreateDirectory(Path.Combine(recoveryRoot, "Failed"));
            var failedPath = Path.Combine(
                recoveryRoot,
                "Failed",
                $"failed-{DateTimeOffset.UtcNow:yyyyMMddTHHmmssfffZ}.json");
            var safeFailure = JsonSerializer.Serialize(new
            {
                schema = 1,
                failedAt = DateTimeOffset.UtcNow,
                error = exception.GetType().Name,
            }, JsonOptions);
            File.WriteAllText(failedPath, safeFailure);
            File.Delete(pendingPath);
            log.Write("error", $"Pending recovery was rejected before backend startup: {exception.Message}");
            return new RecoveryApplyResult(false, string.Empty, failedPath, exception.GetType().Name);
        }
    }

    public static RecoveryApplyResult ApplyPlan(string pendingPath, string commonRoot)
    {
        var root = Path.GetFullPath(commonRoot);
        var planPath = Path.GetFullPath(pendingPath);
        var expectedPlanPath = Path.Combine(root, "State", "Recovery", "pending.json");
        if (!PathEquals(planPath, expectedPlanPath))
        {
            throw new InvalidDataException("Recovery plan path is invalid.");
        }

        var plan = JsonSerializer.Deserialize<PendingRecoveryPlan>(
            File.ReadAllText(planPath), JsonOptions)
            ?? throw new InvalidDataException("Recovery plan is invalid.");
        ValidatePlan(plan, root);
        VerifyDigest(plan.SourceDatabase, plan.SourceDatabaseSha256);
        if (!string.IsNullOrWhiteSpace(plan.SourceCredentialVault))
        {
            VerifyDigest(plan.SourceCredentialVault!, plan.SourceCredentialVaultSha256!);
        }

        Directory.CreateDirectory(Path.GetDirectoryName(plan.TargetDatabase)!);
        Directory.CreateDirectory(Path.GetDirectoryName(plan.BackupDatabase)!);
        var databaseTemporary = plan.TargetDatabase + $".restore-{plan.PlanId}.tmp";
        var credentialTemporary = string.IsNullOrWhiteSpace(plan.TargetCredentialVault)
            ? string.Empty
            : plan.TargetCredentialVault + $".restore-{plan.PlanId}.tmp";
        var targetDatabaseExisted = File.Exists(plan.TargetDatabase);
        var targetCredentialExisted = !string.IsNullOrWhiteSpace(plan.TargetCredentialVault)
            && File.Exists(plan.TargetCredentialVault);

        try
        {
            File.Copy(plan.SourceDatabase, databaseTemporary, overwrite: false);
            if (targetDatabaseExisted)
            {
                File.Copy(plan.TargetDatabase, plan.BackupDatabase, overwrite: false);
            }

            if (!string.IsNullOrWhiteSpace(plan.SourceCredentialVault))
            {
                Directory.CreateDirectory(Path.GetDirectoryName(plan.TargetCredentialVault!)!);
                Directory.CreateDirectory(Path.GetDirectoryName(plan.BackupCredentialVault!)!);
                File.Copy(plan.SourceCredentialVault!, credentialTemporary, overwrite: false);
                if (targetCredentialExisted)
                {
                    File.Copy(plan.TargetCredentialVault!, plan.BackupCredentialVault!, overwrite: false);
                }
            }
            else if (plan.DeleteCredentialTarget && targetCredentialExisted)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(plan.BackupCredentialVault!)!);
                File.Copy(plan.TargetCredentialVault!, plan.BackupCredentialVault!, overwrite: false);
            }

            File.Move(databaseTemporary, plan.TargetDatabase, overwrite: true);
            DeleteSqliteSidecar(plan.TargetDatabase, "-wal");
            DeleteSqliteSidecar(plan.TargetDatabase, "-shm");
            if (!string.IsNullOrWhiteSpace(plan.SourceCredentialVault))
            {
                File.Move(credentialTemporary, plan.TargetCredentialVault!, overwrite: true);
            }
            else if (plan.DeleteCredentialTarget && !string.IsNullOrWhiteSpace(plan.TargetCredentialVault))
            {
                File.Delete(plan.TargetCredentialVault!);
            }
        }
        catch
        {
            RestoreTarget(plan.BackupDatabase, plan.TargetDatabase, targetDatabaseExisted);
            if (!string.IsNullOrWhiteSpace(plan.TargetCredentialVault))
            {
                RestoreTarget(
                    plan.BackupCredentialVault ?? string.Empty,
                    plan.TargetCredentialVault!,
                    targetCredentialExisted);
            }
            throw;
        }
        finally
        {
            File.Delete(databaseTemporary);
            if (!string.IsNullOrWhiteSpace(credentialTemporary))
            {
                File.Delete(credentialTemporary);
            }
        }

        var completedRoot = Path.Combine(root, "State", "Recovery", "Completed");
        Directory.CreateDirectory(completedRoot);
        var evidencePath = Path.Combine(completedRoot, $"{plan.PlanId}.json");
        var evidence = JsonSerializer.Serialize(new
        {
            schema = 1,
            planId = plan.PlanId,
            appliedAt = DateTimeOffset.UtcNow,
            targetDatabase = plan.TargetDatabase,
            backupDatabase = targetDatabaseExisted ? plan.BackupDatabase : string.Empty,
            targetCredentialVault = plan.TargetCredentialVault ?? string.Empty,
            backupCredentialVault = targetCredentialExisted ? plan.BackupCredentialVault ?? string.Empty : string.Empty,
            credentialTargetPreviouslyExisted = targetCredentialExisted,
            originPlanId = plan.OriginPlanId ?? string.Empty,
        }, JsonOptions);
        var evidenceTemporary = evidencePath + ".tmp";
        File.WriteAllText(evidenceTemporary, evidence);
        File.Move(evidenceTemporary, evidencePath, overwrite: true);
        File.Delete(planPath);
        return new RecoveryApplyResult(true, plan.PlanId, evidencePath, string.Empty);
    }

    private static void ValidatePlan(PendingRecoveryPlan plan, string root)
    {
        if (plan.Schema != 1 || !Guid.TryParse(plan.PlanId, out _))
        {
            throw new InvalidDataException("Recovery plan identity is invalid.");
        }

        var allowedSources = new[]
        {
            Path.Combine(root, "Recovery", "Staging"),
            Path.Combine(root, "Backups"),
        };
        RequireWithinAny(plan.SourceDatabase, allowedSources, "source database");
        RequireWithin(plan.TargetDatabase, root, "target database");
        RequireWithin(plan.BackupDatabase, Path.Combine(root, "Backups"), "database backup");
        if (!string.IsNullOrWhiteSpace(plan.SourceCredentialVault))
        {
            RequireWithinAny(plan.SourceCredentialVault!, allowedSources, "source credential vault");
        }
        if (!string.IsNullOrWhiteSpace(plan.TargetCredentialVault))
        {
            RequireWithin(plan.TargetCredentialVault!, root, "target credential vault");
        }
        if (!string.IsNullOrWhiteSpace(plan.BackupCredentialVault))
        {
            RequireWithin(plan.BackupCredentialVault!, Path.Combine(root, "Backups"), "credential backup");
        }
        if (!File.Exists(plan.SourceDatabase))
        {
            throw new FileNotFoundException("Recovery source database is missing.");
        }
    }

    private static void VerifyDigest(string path, string expected)
    {
        if (string.IsNullOrWhiteSpace(expected) || expected.Length != 64)
        {
            throw new InvalidDataException("Recovery source digest is invalid.");
        }
        using var stream = File.OpenRead(path);
        var actual = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
        if (!string.Equals(actual, expected, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Recovery source digest changed.");
        }
    }

    private static void RequireWithin(string path, string parent, string label)
    {
        var candidate = Path.GetFullPath(path);
        var root = Path.GetFullPath(parent);
        var relative = Path.GetRelativePath(root, candidate);
        if (relative == "." || relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal) || Path.IsPathRooted(relative))
        {
            throw new InvalidDataException($"Recovery {label} path is outside its allowed root.");
        }
    }

    private static void RequireWithinAny(string path, IEnumerable<string> parents, string label)
    {
        foreach (var parent in parents)
        {
            try
            {
                RequireWithin(path, parent, label);
                return;
            }
            catch (InvalidDataException)
            {
                // Try the next explicitly allowed product root.
            }
        }
        throw new InvalidDataException($"Recovery {label} path is outside its allowed roots.");
    }

    private static bool PathEquals(string left, string right) =>
        string.Equals(Path.GetFullPath(left), Path.GetFullPath(right), StringComparison.OrdinalIgnoreCase);

    private static void DeleteSqliteSidecar(string database, string suffix)
    {
        var path = database + suffix;
        if (File.Exists(path))
        {
            File.Delete(path);
        }
    }

    private static void RestoreTarget(string backup, string target, bool previouslyExisted)
    {
        if (previouslyExisted && !string.IsNullOrWhiteSpace(backup) && File.Exists(backup))
        {
            File.Copy(backup, target, overwrite: true);
        }
        else if (!previouslyExisted && File.Exists(target))
        {
            File.Delete(target);
        }
    }
}

using System.Security.Cryptography;
using System.Text.Json;
using CleanroomRadio.ServiceHost;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public sealed class PendingRecoveryApplierTests
{
    [Fact]
    public void AppliesDatabaseAndCredentialVaultWithRollbackBackups()
    {
        var root = NewRoot();
        try
        {
            var planId = Guid.NewGuid().ToString();
            var sourceDatabase = Write(root, Path.Combine("Recovery", "Staging", "stage", "databases", "rtai.sqlite3"), "new-database");
            var targetDatabase = Write(root, "rtai-onair.db", "old-database");
            var sourceVault = Write(root, Path.Combine("Recovery", "Staging", "stage", "protected-local", "vault.json"), "new-vault");
            var targetVault = Write(root, Path.Combine("secrets", "station-credentials.json"), "old-vault");
            var backupDatabase = Path.Combine(root, "Backups", $"{planId}-database.bak");
            var backupVault = Path.Combine(root, "Backups", $"{planId}-vault.bak");
            var planPath = Path.Combine(root, "State", "Recovery", "pending.json");
            Directory.CreateDirectory(Path.GetDirectoryName(planPath)!);
            File.WriteAllText(planPath, JsonSerializer.Serialize(new
            {
                schema = 1,
                planId,
                sourceDatabase,
                sourceDatabaseSha256 = Sha256(sourceDatabase),
                targetDatabase,
                backupDatabase,
                sourceCredentialVault = sourceVault,
                sourceCredentialVaultSha256 = Sha256(sourceVault),
                targetCredentialVault = targetVault,
                backupCredentialVault = backupVault,
                deleteCredentialTarget = false,
            }));

            var result = PendingRecoveryApplier.ApplyPlan(planPath, root);

            Assert.True(result.Applied);
            Assert.Equal("new-database", File.ReadAllText(targetDatabase));
            Assert.Equal("old-database", File.ReadAllText(backupDatabase));
            Assert.Equal("new-vault", File.ReadAllText(targetVault));
            Assert.Equal("old-vault", File.ReadAllText(backupVault));
            Assert.False(File.Exists(planPath));
            Assert.True(File.Exists(result.EvidencePath));
        }
        finally
        {
            Directory.Delete(root, recursive: true);
        }
    }

    [Fact]
    public void RejectsSourceOutsideProductRecoveryRoots()
    {
        var root = NewRoot();
        var outside = Path.Combine(Path.GetTempPath(), $"rtai-outside-{Guid.NewGuid():N}.db");
        try
        {
            File.WriteAllText(outside, "untrusted");
            var planId = Guid.NewGuid().ToString();
            var targetDatabase = Path.Combine(root, "rtai-onair.db");
            Directory.CreateDirectory(Path.GetDirectoryName(targetDatabase)!);
            var planPath = Path.Combine(root, "State", "Recovery", "pending.json");
            Directory.CreateDirectory(Path.GetDirectoryName(planPath)!);
            File.WriteAllText(planPath, JsonSerializer.Serialize(new
            {
                schema = 1,
                planId,
                sourceDatabase = outside,
                sourceDatabaseSha256 = Sha256(outside),
                targetDatabase,
                backupDatabase = Path.Combine(root, "Backups", "backup.db"),
                sourceCredentialVault = (string?)null,
                sourceCredentialVaultSha256 = (string?)null,
                targetCredentialVault = (string?)null,
                backupCredentialVault = (string?)null,
                deleteCredentialTarget = false,
            }));

            Assert.Throws<InvalidDataException>(() =>
                PendingRecoveryApplier.ApplyPlan(planPath, root));
        }
        finally
        {
            File.Delete(outside);
            Directory.Delete(root, recursive: true);
        }
    }

    [Fact]
    public void AppliesRecoveryRollbackAndRecordsItsOrigin()
    {
        var root = NewRoot();
        try
        {
            var planId = Guid.NewGuid().ToString();
            var originPlanId = Guid.NewGuid().ToString();
            var sourceDatabase = Write(root, Path.Combine("Backups", "before-recovery.db"), "before-recovery");
            var targetDatabase = Write(root, "rtai-onair.db", "restored-database");
            var targetVault = Write(root, Path.Combine("secrets", "station-credentials.json"), "restored-vault");
            var backupDatabase = Path.Combine(root, "Backups", $"{planId}-database.bak");
            var backupVault = Path.Combine(root, "Backups", $"{planId}-vault.bak");
            var planPath = Path.Combine(root, "State", "Recovery", "pending.json");
            Directory.CreateDirectory(Path.GetDirectoryName(planPath)!);
            File.WriteAllText(planPath, JsonSerializer.Serialize(new
            {
                schema = 1,
                planId,
                originPlanId,
                sourceDatabase,
                sourceDatabaseSha256 = Sha256(sourceDatabase),
                targetDatabase,
                backupDatabase,
                sourceCredentialVault = (string?)null,
                sourceCredentialVaultSha256 = (string?)null,
                targetCredentialVault = targetVault,
                backupCredentialVault = backupVault,
                deleteCredentialTarget = true,
            }));

            var result = PendingRecoveryApplier.ApplyPlan(planPath, root);
            using var evidence = JsonDocument.Parse(File.ReadAllText(result.EvidencePath));

            Assert.Equal("before-recovery", File.ReadAllText(targetDatabase));
            Assert.Equal("restored-database", File.ReadAllText(backupDatabase));
            Assert.False(File.Exists(targetVault));
            Assert.Equal("restored-vault", File.ReadAllText(backupVault));
            Assert.Equal(originPlanId, evidence.RootElement.GetProperty("originPlanId").GetString());
        }
        finally
        {
            Directory.Delete(root, recursive: true);
        }
    }

    private static string NewRoot()
    {
        var root = Path.Combine(Path.GetTempPath(), $"rtai-recovery-test-{Guid.NewGuid():N}");
        Directory.CreateDirectory(root);
        return root;
    }

    private static string Write(string root, string relative, string value)
    {
        var path = Path.Combine(root, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, value);
        return path;
    }

    private static string Sha256(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }
}

using Xunit;

namespace CodeStoragePoc.PlanContracts.Tests;

public sealed class CliResolverTests
{
    [Fact]
    public void ResolvesConfiguredPathContainingSpacesWithoutLaunchingTheCli()
    {
        var directory = Path.Combine(Path.GetTempPath(), "hekate cli " + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var name = "hekate-test-" + Guid.NewGuid().ToString("N");
        var executable = Path.Combine(directory, name + ".cmd");
        File.WriteAllText(executable, "@echo off");
        var original = Environment.GetEnvironmentVariable("PATH");
        try
        {
            Environment.SetEnvironmentVariable("PATH", directory);
            Assert.Equal(executable, CliResolver.Resolve(name));
        }
        finally
        {
            Environment.SetEnvironmentVariable("PATH", original);
            Directory.Delete(directory, true);
        }
    }
}

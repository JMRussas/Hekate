using Xunit;

namespace CodeStoragePoc.PlanContracts.Tests;

public class PlanContractGateTests
{
    private static Dictionary<string, string?> Safe() => new()
    {
        ["HEKATE_PLAN_CONTRACT"] = "1",
        ["HEKATE_API_URLS"] = "http://127.0.0.1:5103",
        ["CODESTORAGE_CONNSTR"] = "Host=127.0.0.1;Port=5434;Database=code_storage;Username=postgres;Password=postgres",
        ["HEKATE_DISABLE_DISPATCHER"] = "1",
    };

    private static PlanContractGateResult Eval(Dictionary<string, string?> env) =>
        PlanContractGate.Evaluate(k => env.TryGetValue(k, out var v) ? v : null);

    [Fact]
    public void Flag_absent_or_not_one_is_disabled_without_checking_anything_else()
    {
        Assert.Equal(PlanContractMode.Disabled, Eval(new()).Mode);
        Assert.Equal(PlanContractMode.Disabled, Eval(new() { ["HEKATE_PLAN_CONTRACT"] = "true" }).Mode);
    }

    [Fact]
    public void Safe_local_profile_is_enabled()
    {
        Assert.Equal(PlanContractMode.Enabled, Eval(Safe()).Mode);
        var localhost = Safe(); localhost["HEKATE_API_URLS"] = "http://localhost:5103"; localhost["CODESTORAGE_CONNSTR"] = "Host=localhost;Database=x;Username=u;Password=p";
        Assert.Equal(PlanContractMode.Enabled, Eval(localhost).Mode);
    }

    [Theory]
    [InlineData("HEKATE_API_URLS", null, "must be set")]
    [InlineData("HEKATE_API_URLS", "http://0.0.0.0:5103", "not loopback")]
    [InlineData("HEKATE_API_URLS", "http://*:5103", "not")]
    [InlineData("HEKATE_API_URLS", "http://+:5103", "not")]
    [InlineData("HEKATE_API_URLS", "http://127.0.0.1:5103;http://0.0.0.0:5104", "exactly one URL")]
    [InlineData("HEKATE_API_URLS", "http://192.168.1.164:5103", "not loopback")]
    [InlineData("CODESTORAGE_CONNSTR", null, "set explicitly")]
    [InlineData("CODESTORAGE_CONNSTR", "Host=192.168.1.164;Database=x", "not loopback")]
    [InlineData("CODESTORAGE_CONNSTR", "Host=127.0.0.1,192.168.1.164;Database=x", "exactly one host")]
    [InlineData("HEKATE_API_URLS", "http://127.0.0.1.nip.io:5103", "not loopback")]
    [InlineData("HEKATE_API_URLS", "http://localhost.:5103", "not loopback")]
    [InlineData("HEKATE_API_URLS", "http://[::ffff:127.0.0.1]:5103", "not loopback")]
    [InlineData("HEKATE_API_URLS", "http://127.0.0.2:5103", "not loopback")]
    [InlineData("HEKATE_API_URLS", "127.0.0.1:5103", "not")]
    [InlineData("CODESTORAGE_CONNSTR", "Database=x;Username=u;Password=p", "Host explicitly")]
    [InlineData("CODESTORAGE_CONNSTR", "Host=127.0.0.1;Username=u;Password=p", "Database explicitly")]
    [InlineData("CODESTORAGE_CONNSTR", "Server=192.168.1.164;Database=x", "not loopback")]
    [InlineData("HEKATE_DISABLE_DISPATCHER", null, "HEKATE_DISABLE_DISPATCHER")]
    [InlineData("HEKATE_DISABLE_DISPATCHER", "0", "HEKATE_DISABLE_DISPATCHER")]
    public void Explicit_flag_with_unsafe_configuration_fails_startup(string key, string? value, string expected)
    {
        var env = Safe();
        env[key] = value;
        var ex = Assert.Throws<PlanContractConfigurationException>(() => Eval(env));
        Assert.Contains(expected, ex.Message);
        Assert.Contains("Refusing to start", ex.Message);
    }
}

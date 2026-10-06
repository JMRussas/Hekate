// CodeStoragePoc - Plan node contracts v1: opt-in safety gate
//
// The plan-contract store (schema, fence triggers, endpoints) is enabled only for an
// explicitly local profile. With HEKATE_PLAN_CONTRACT unset the feature is simply off.
// With HEKATE_PLAN_CONTRACT=1 every condition must hold, otherwise startup fails BEFORE
// any managed DDL — an explicitly requested but unsafe configuration never half-enables.
//
// Depends on: Npgsql (connection-string parsing)
// Used by:    Api/Program, PlanContracts.Tests

using Npgsql;

namespace CodeStoragePoc.PlanContracts;

public enum PlanContractMode { Disabled, Enabled }

public sealed record PlanContractGateResult(PlanContractMode Mode, string Reason);

public sealed class PlanContractConfigurationException(string message) : Exception(message);

public static class PlanContractGate
{
    public const string FlagVariable = "HEKATE_PLAN_CONTRACT";

    private static readonly HashSet<string> LoopbackHosts = new(StringComparer.OrdinalIgnoreCase)
    {
        "127.0.0.1", "localhost", "::1", "[::1]",
    };

    /// <summary>
    /// Disabled when the flag is not "1". Enabled only when the Api URL, the database host and
    /// the dispatcher setting are all strictly local. Throws PlanContractConfigurationException
    /// when the flag is "1" but any condition fails.
    /// </summary>
    public static PlanContractGateResult Evaluate(Func<string, string?> env)
    {
        var flag = env(FlagVariable);
        if (flag != "1")
            return new(PlanContractMode.Disabled, flag is null ? $"{FlagVariable} not set" : $"{FlagVariable}='{flag}' (only '1' enables)");

        var problems = new List<string>();

        var urls = env("HEKATE_API_URLS");
        if (string.IsNullOrWhiteSpace(urls)) problems.Add("HEKATE_API_URLS must be set to a single loopback URL (the default binds 0.0.0.0)");
        else if (urls.Contains(';') || urls.Contains(','))
            problems.Add("HEKATE_API_URLS must be exactly one URL");
        else if (!Uri.TryCreate(urls.Trim(), UriKind.Absolute, out var uri) || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            problems.Add($"HEKATE_API_URLS '{urls}' is not an absolute http(s) URL");
        else if (!LoopbackHosts.Contains(uri.Host))
            problems.Add($"HEKATE_API_URLS host '{uri.Host}' is not loopback");

        var connStr = env("CODESTORAGE_CONNSTR");
        if (string.IsNullOrWhiteSpace(connStr)) problems.Add("CODESTORAGE_CONNSTR must be set explicitly (no default credentials)");
        else
        {
            NpgsqlConnectionStringBuilder? csb = null;
            try { csb = new NpgsqlConnectionStringBuilder(connStr); }
            catch (Exception) { problems.Add("CODESTORAGE_CONNSTR could not be parsed"); }
            if (csb is not null)
            {
                var host = csb.Host;
                if (string.IsNullOrWhiteSpace(host)) problems.Add("CODESTORAGE_CONNSTR must name a Host explicitly");
                else if (host.Contains(',')) problems.Add("CODESTORAGE_CONNSTR must name exactly one host");
                else if (!LoopbackHosts.Contains(host.Trim())) problems.Add($"CODESTORAGE_CONNSTR host '{host}' is not loopback");
                if (string.IsNullOrWhiteSpace(csb.Database)) problems.Add("CODESTORAGE_CONNSTR must name a Database explicitly");
            }
        }

        if (env("HEKATE_DISABLE_DISPATCHER") != "1")
            problems.Add("HEKATE_DISABLE_DISPATCHER must be '1' (no agent spawning in the plan-contract profile)");

        if (problems.Count > 0)
            throw new PlanContractConfigurationException(
                $"{FlagVariable}=1 but the configuration is not a safe local profile: {string.Join("; ", problems)}. Refusing to start.");
        return new(PlanContractMode.Enabled, "local profile verified");
    }
}

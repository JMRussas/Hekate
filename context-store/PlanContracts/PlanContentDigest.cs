// CodeStoragePoc - Plan node contracts v1: canonical content digest
//
// Stable SHA-256 over a node's contract content (value + content attributes) used in
// operation fingerprints, so a reused operation key with different content is detected.
// Null and empty are distinct, and absent and present keys are distinct.
//
// Depends on: (none)
// Used by:    PlanStore, PlanContracts.Tests

using System.Security.Cryptography;
using System.Text;

namespace CodeStoragePoc.PlanContracts;

public static class PlanContentDigest
{
    /// <summary>Attribute keys that are contract content on managed nodes (writes bump ContentRevision).</summary>
    public static readonly System.Collections.Immutable.ImmutableHashSet<string> ContentAttributeKeys =
        System.Collections.Immutable.ImmutableHashSet.Create(StringComparer.Ordinal,
            "description", "acceptance_criteria", "verification_criteria", "success_criteria",
            "scope", "affected_files", "requirement_ids");

    /// <summary>Digest of (fields..., value, sorted attributes). Fields cover e.g. a child's name.</summary>
    public static string Compute(string? value, IReadOnlyDictionary<string, string?>? attributes, params string?[] fields)
    {
        var sb = new StringBuilder();
        foreach (var f in fields) Append(sb, f);
        Append(sb, value);
        var attrs = attributes ?? new Dictionary<string, string?>();
        sb.Append("attrs:").Append(attrs.Count).Append(';');
        foreach (var kv in attrs.OrderBy(k => k.Key, StringComparer.Ordinal))
        {
            Append(sb, kv.Key);
            Append(sb, kv.Value);
        }
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(sb.ToString())));
    }

    private static void Append(StringBuilder sb, string? s)
    {
        if (s is null) { sb.Append("~;"); return; }
        sb.Append(s.Length).Append(':').Append(s).Append(';');
    }
}

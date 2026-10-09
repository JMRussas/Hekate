// CodeStoragePoc - CLI Resolver
//
// Resolves CLI executable names (claude, gemini, codex) to their actual paths on Windows.
// .NET Process.Start with UseShellExecute=false does NOT search PATH for .cmd files,
// only .exe. npm-installed CLIs are .cmd wrappers, so we need to resolve them ourselves.
//
// Used by: ChatService, ExtractionService, AgentDispatcher

using System.Diagnostics;

namespace CodeStoragePoc;

public static class CliResolver
{
    private static readonly Dictionary<string, string?> _cache = new(StringComparer.OrdinalIgnoreCase);
    private static readonly object _lock = new();
    private static readonly string[] _extensions = [".cmd", ".exe", ".bat", ".ps1"];

    /// <summary>
    /// Resolve a bare executable name to a full path that Process.Start can find.
    /// Searches PATH for .cmd, .exe, .bat, .ps1 variants.
    /// Returns the original name if nothing found (let Process.Start fail with a clear error).
    /// </summary>
    public static string Resolve(string executable)
    {
        // Already a rooted path — use as-is
        if (Path.IsPathRooted(executable))
            return executable;

        lock (_lock)
        {
            if (_cache.TryGetValue(executable, out var cached))
                return cached ?? executable;
        }

        var resolved = SearchPath(executable);

        lock (_lock)
        {
            _cache[executable] = resolved;
        }

        return resolved ?? executable;
    }

    // Well-known directories where npm/pip install CLI tools on Windows
    private static readonly string[] _fallbackDirs =
    [
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "npm"),
        @"C:\Program Files\nodejs",
    ];

    private static string? SearchPath(string executable)
    {
        var pathVar = Environment.GetEnvironmentVariable("PATH");
        Console.WriteLine($"[CLI] Resolving '{executable}', PATH length: {pathVar?.Length ?? 0}");

        var dirs = new List<string>();

        if (!string.IsNullOrEmpty(pathVar))
            dirs.AddRange(pathVar.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries));

        // Add fallback directories (NSSM LocalSystem may not have user PATH)
        foreach (var fb in _fallbackDirs)
        {
            if (!dirs.Contains(fb, StringComparer.OrdinalIgnoreCase) && Directory.Exists(fb))
                dirs.Add(fb);
        }

        foreach (var dir in dirs)
        {
            // Check with each extension first (npm CLIs are .cmd)
            foreach (var ext in _extensions)
            {
                var candidate = Path.Combine(dir, executable + ext);
                if (File.Exists(candidate))
                {
                    Console.WriteLine($"[CLI] Resolved '{executable}' → {candidate}");
                    return candidate;
                }
            }

            // Check bare name (Linux/Git Bash executables)
            var bare = Path.Combine(dir, executable);
            if (File.Exists(bare))
            {
                Console.WriteLine($"[CLI] Resolved '{executable}' → {bare}");
                return bare;
            }
        }

        Console.WriteLine($"[CLI] FAILED to resolve '{executable}' in {dirs.Count} directories");
        return null;
    }
}

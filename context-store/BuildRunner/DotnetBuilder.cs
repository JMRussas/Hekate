// CodeStoragePoc - Build Runner
//
// Invokes `dotnet build` against a generated project directory.
// Captures stdout/stderr and reports success/failure.
//
// Depends on: (none - uses System.Diagnostics.Process)
// Used by:    Program

using System.Diagnostics;

namespace CodeStoragePoc.BuildRunner;

public static class DotnetBuilder
{
    /// <summary>
    /// Run dotnet build in the given directory. Returns (success, output).
    /// </summary>
    public static async Task<(bool Success, string Output)> Build(string projectDir)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "dotnet",
            Arguments = "build",
            WorkingDirectory = projectDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        using var proc = Process.Start(psi)!;
        // Read stdout and stderr concurrently to avoid deadlock when one buffer fills
        var stdoutTask = proc.StandardOutput.ReadToEndAsync();
        var stderrTask = proc.StandardError.ReadToEndAsync();
        await Task.WhenAll(stdoutTask, stderrTask);
        await proc.WaitForExitAsync();

        var stdout = stdoutTask.Result;
        var stderr = stderrTask.Result;

        var success = proc.ExitCode == 0;
        var output = success ? stdout : $"{stdout}\n{stderr}";

        Console.WriteLine($"[BUILD]    dotnet build: {(success ? "SUCCESS" : "FAILED")}");
        if (!success)
            Console.WriteLine($"[BUILD]    {stderr.Trim()}");

        return (success, output.Trim());
    }
}

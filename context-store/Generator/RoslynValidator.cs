// CodeStoragePoc - Roslyn Validator
//
// Validates generated C# source code using the Roslyn compiler.
// Parses the code and checks for syntax diagnostics.
// Rejects code with errors before it gets written to disk.
//
// Note: This does parse-level validation (syntax). Full semantic validation
// (type checking, reference resolution) would require creating a Compilation
// with assembly references, which is beyond the POC scope.
//
// Depends on: Microsoft.CodeAnalysis.CSharp
// Used by:    Program

using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace CodeStoragePoc.Generator;

public class RoslynValidator
{
    /// <summary>
    /// Parse the C# source and return diagnostics.
    /// </summary>
    public IReadOnlyList<Diagnostic> Validate(string sourceCode)
    {
        var tree = CSharpSyntaxTree.ParseText(sourceCode);
        return tree.GetDiagnostics().ToList();
    }

    /// <summary>
    /// Validate and throw if there are any error-level diagnostics.
    /// </summary>
    public void ValidateOrThrow(string sourceCode)
    {
        var diagnostics = Validate(sourceCode);
        var errors = diagnostics.Where(d => d.Severity == DiagnosticSeverity.Error).ToList();

        Console.WriteLine($"[VALIDATE] Roslyn: {errors.Count} errors, {diagnostics.Count} total diagnostics");

        if (errors.Count > 0)
        {
            foreach (var error in errors)
                Console.WriteLine($"[VALIDATE]   ERROR: {error.GetMessage()} at {error.Location.GetLineSpan()}");

            throw new InvalidOperationException(
                $"Generated code has {errors.Count} Roslyn errors. See output above.");
        }
    }
}

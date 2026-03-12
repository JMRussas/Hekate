// CodeStoragePoc - Edge Type Constants
//
// Centralized string constants for AGE graph edge labels.
// Prevents typos and provides a single registry of valid edge types.
// Must stay in sync with AgeLayer.ValidEdgeTypes whitelist.
//
// Depends on: (none)
// Used by:    AgeLayer, ExtractionService, ContextAssembler, PlanToCodeGenerator

namespace CodeStoragePoc.GraphLayer;

/// <summary>
/// String constants for AGE edge labels used in the code_graph.
/// Organized by domain. Add new edge types here AND in AgeLayer.ValidEdgeTypes.
/// </summary>
public static class EdgeTypes
{
    // Code domain
    public const string Calls = "CALLS";
    public const string References = "REFERENCES";
    public const string DependsOn = "DEPENDS_ON";
    public const string Modifies = "MODIFIES";

    // Ideation & planning domain
    public const string Extracted = "EXTRACTED";
    public const string SpawnedFrom = "SPAWNED_FROM";
    public const string ImplementedBy = "IMPLEMENTED_BY";
    public const string Produces = "PRODUCES";
    public const string Constrains = "CONSTRAINS";
    public const string Blocks = "BLOCKS";
    public const string RelatesTo = "RELATES_TO";
    public const string Contradicts = "CONTRADICTS";

    // Research & roadmap domain
    public const string Informs = "INFORMS";

    // Agent action domain
    public const string Produced = "PRODUCED";
    public const string Triggered = "TRIGGERED";
    public const string ForkedFrom = "FORKED_FROM";
    public const string Observed = "OBSERVED";
    public const string Informed = "INFORMED";
}

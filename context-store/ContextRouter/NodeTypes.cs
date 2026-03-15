// CodeStoragePoc - Node Type Constants
//
// Centralized string constants for all known node types.
// Prevents typos and provides a single registry of valid types.
// SQL queries use these values as string literals — keep in sync.
//
// Depends on: (none)
// Used by:    ContextAssembler, ChatService, SeedData/*, Program

namespace CodeStoragePoc.ContextRouter;

/// <summary>
/// String constants for node types stored in the nodes.node_type TEXT column.
/// Organized by domain. Add new types here — not as scattered string literals.
/// </summary>
public static class NodeTypes
{
    // Code domain
    public const string CompilationUnit = "compilation_unit";
    public const string UsingDirective = "using_directive";
    public const string Namespace = "namespace";
    public const string Struct = "struct";
    public const string Class = "class";
    public const string Field = "field";
    public const string Constructor = "constructor";
    public const string Method = "method";
    public const string Parameter = "parameter";
    public const string Block = "block";
    public const string Statement = "statement";

    // Planning domain
    public const string Plan = "plan";
    public const string PlanPhase = "plan_phase";
    public const string PlanStep = "plan_step";
    public const string Task = "task";
    public const string Risk = "risk";
    public const string TestSpec = "test_spec";
    public const string Revision = "revision";
    public const string Retrospective = "retrospective";
    public const string Blocker = "blocker";
    public const string Milestone = "milestone";

    // Ideation domain
    public const string Conversation = "conversation";
    public const string Turn = "turn";
    public const string Topic = "topic";
    public const string Idea = "idea";
    public const string Question = "question";
    public const string Decision = "decision";
    public const string ActionItem = "action_item";
    public const string Thread = "thread";

    // Interpreter domain
    public const string Interpretation = "interpretation";

    // Tool-use domain
    public const string ToolCall = "tool_call";
    public const string ToolResult = "tool_result";
    public const string Skill = "skill";

    // Research & roadmap domain
    public const string Roadmap = "roadmap";
    public const string Research = "research";
    public const string Finding = "finding";
    public const string OpenQuestion = "open_question";
    public const string Reference = "reference";

    // Permission domain
    public const string PendingAction = "pending_action";

    // Agent action domain (planned)
    public const string MutateNode = "mutate_node";
    public const string Execute = "execute";

    // Sentinel domain
    public const string SentinelObservation = "sentinel_observation";
}

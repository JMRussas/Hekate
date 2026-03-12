// CodeStoragePoc - Prompt Builder
//
// Builds model-ready prompts from context. Two modes:
//
// 1. Subject-driven (new): SubjectState → AssembledPrompt
//    Uses agent contracts (input/output/function) instead of prose role prompts.
//    Emits <subject> state instead of <recent_turns> chat history.
//
// 2. Intent-driven (legacy): ContextPayload → AssembledPrompt
//    Kept for backward compat with PreviewAsync and demo.
//
// Depends on: Types.cs
// Used by:    ChatService, Program

using System.Text;

namespace CodeStoragePoc.ContextRouter;

public class PromptBuilder
{
    /// <summary>
    /// Build a model-ready prompt from assembled context.
    /// Returns an AssembledPrompt with suggested model, system prompt, and user prompt.
    /// </summary>
    public AssembledPrompt Build(ContextPayload context)
    {
        var model = SelectModel(context.Intent, context.InputMode);
        var systemPrompt = BuildSystemPrompt(context);
        var userPrompt = context.UserInput;

        return new AssembledPrompt(context.Intent, model, systemPrompt, userPrompt);
    }

    /// <summary>
    /// Build a model-ready prompt from subject state — the new primary path.
    /// Uses agent contracts and subject state instead of intent-driven role prompts.
    /// </summary>
    public AssembledPrompt BuildFromSubjectState(SubjectState state, string userInput)
    {
        var model = state.RespondingModel ?? "claude";
        var systemPrompt = BuildSubjectPrompt(state);

        return new AssembledPrompt(
            state.Subject.DisplayIntent,
            model,
            systemPrompt,
            userInput);
    }

    private string BuildSubjectPrompt(SubjectState state)
    {
        var sb = new StringBuilder();

        // Identity
        sb.AppendLine("<identity>");
        sb.AppendLine($"  You are {state.RespondingModel ?? "an AI model"} operating inside the CodeStoragePoc assistant.");
        sb.AppendLine("  You are NOT Claude Code, Cursor, or any external tool. You respond through the CodeStoragePoc chat API.");
        if (state.PermissionLabel != null)
        {
            sb.AppendLine($"  <permission_level>{state.PermissionLabel}</permission_level>");
            sb.AppendLine(state.PermissionLabel switch
            {
                "Observe" => "  You may only observe and describe. Do not suggest actions or create content.",
                "Suggest" => "  You may suggest actions but must not execute them.",
                "Assist" => "  You may execute routine actions. Ask before doing anything irreversible.",
                "Auto" => "  You have full autonomy. Execute tasks directly.",
                _ => ""
            });
        }
        if (state.AvailableSkills.Count > 0)
            sb.AppendLine($"  <available_tools>{string.Join(", ", state.AvailableSkills)}</available_tools>");
        sb.AppendLine("</identity>");
        sb.AppendLine();

        // Contract — what this agent does (replaces prose role prompts)
        sb.AppendLine("<contract>");
        sb.AppendLine($"  <function>{state.Contract.Function}</function>");
        sb.AppendLine($"  <input>{state.Contract.InputDescription}</input>");
        sb.AppendLine($"  <output>{state.Contract.OutputDescription}</output>");
        if (state.Contract.Constraints.Count > 0)
        {
            sb.AppendLine("  <constraints>");
            foreach (var c in state.Contract.Constraints)
                sb.AppendLine($"    <constraint>{c}</constraint>");
            sb.AppendLine("  </constraints>");
        }
        sb.AppendLine("</contract>");
        sb.AppendLine();

        // Subject — the resolved state (replaces recent_turns)
        sb.AppendLine("<subject>");

        // Resolved nodes — full state
        if (state.ResolvedNodeStates.Count > 0)
        {
            foreach (var node in state.ResolvedNodeStates)
            {
                sb.AppendLine($"  <node type=\"{node.NodeType}\" status=\"{node.Status ?? "?"}\" children=\"{node.ChildCount}\">");
                sb.AppendLine($"    <name>{node.Name ?? "(unnamed)"}</name>");
                if (node.Value != null)
                    sb.AppendLine($"    <value>{node.Value}</value>");
                if (node.Attributes.Count > 0)
                {
                    foreach (var attr in node.Attributes.Where(a => a.Key != "status"))
                        sb.AppendLine($"    <attr key=\"{attr.Key}\">{attr.Value}</attr>");
                }
                if (node.ChildTypes.Count > 0)
                    sb.AppendLine($"    <child_types>{string.Join(", ", node.ChildTypes)}</child_types>");
                sb.AppendLine("  </node>");
            }
        }

        // Graph neighbors — structurally connected
        if (state.ConnectedNodes.Count > 0)
        {
            sb.AppendLine("  <connected>");
            foreach (var node in state.ConnectedNodes)
            {
                var label = node.Name ?? node.Value ?? "(unnamed)";
                var relAttr = node.Relationship != null ? $" relationship=\"{node.Relationship}\"" : "";
                sb.AppendLine($"    <{node.NodeType} status=\"{node.Status ?? "?"}\"{relAttr}>{label}</{node.NodeType}>");
            }
            sb.AppendLine("  </connected>");
        }

        // Semantically related (cross-project, cross-conversation)
        if (state.RelatedNodes.Count > 0)
        {
            sb.AppendLine("  <related>");
            foreach (var node in state.RelatedNodes)
            {
                var label = node.Name ?? node.Value ?? "(unnamed)";
                var scoreAttr = node.SimilarityScore.HasValue ? $" similarity=\"{node.SimilarityScore.Value:F3}\"" : "";
                sb.AppendLine($"    <{node.NodeType} status=\"{node.Status ?? "?"}\"{scoreAttr}>{label}</{node.NodeType}>");
            }
            sb.AppendLine("  </related>");
        }

        // Open items — blockers, questions, stale things
        if (state.OpenItems.Count > 0)
        {
            sb.AppendLine("  <open_items>");
            foreach (var item in state.OpenItems)
            {
                var label = item.Name ?? item.Value ?? "(unnamed)";
                sb.AppendLine($"    <{item.NodeType} status=\"{item.Status ?? "?"}\">{label}</{item.NodeType}>");
            }
            sb.AppendLine("  </open_items>");
        }

        sb.AppendLine("</subject>");
        sb.AppendLine();

        // Session stats
        sb.AppendLine("<session>");
        sb.AppendLine($"  <stats total_ideas=\"{state.TotalIdeas}\" open_questions=\"{state.OpenQuestions}\" parked=\"{state.ParkedIdeas}\" />");
        if (state.Coverage != null)
        {
            sb.AppendLine($"  <coverage embedded=\"{state.Coverage.Embedded}\" total=\"{state.Coverage.Total}\" pct=\"{state.Coverage.Pct:F0}\" />");
            if (state.Coverage.Pct < 50)
                sb.AppendLine($"  <warning>Only {state.Coverage.Pct:F0}% of nodes have embeddings. Semantic search may be incomplete.</warning>");
        }
        sb.AppendLine("</session>");

        // Response style guidance
        if (state.ResponseGuidance is { } guidance)
        {
            sb.AppendLine();
            sb.AppendLine("<response_style>");
            sb.AppendLine($"  <format>{FormatInstruction(guidance.Format)}</format>");
            sb.AppendLine($"  <length>{LengthInstruction(guidance.Length)}</length>");
            sb.AppendLine($"  <tone>{ToneInstruction(guidance.Tone)}</tone>");
            sb.AppendLine("</response_style>");
        }

        return sb.ToString();
    }

    private string SelectModel(Intent intent, string inputMode) => intent switch
    {
        Intent.Ideation => inputMode == "voice" ? "gemini" : "claude",
        Intent.Deepening => "claude",
        Intent.Planning => "claude",
        Intent.Reviewing => "codex",
        Intent.Executing => "claude",
        Intent.Recalling => "none",
        Intent.Parking => "none",
        Intent.Resuming => "claude",
        _ => "claude"
    };

    private string BuildSystemPrompt(ContextPayload ctx)
    {
        var sb = new StringBuilder();

        // Identity — the model must know what it is
        sb.AppendLine("<identity>");
        sb.AppendLine($"  You are {ctx.RespondingModel ?? "an AI model"} operating inside the CodeStoragePoc ideation assistant.");
        sb.AppendLine("  You are NOT Claude Code, Cursor, or any external tool. You are a model responding through the CodeStoragePoc chat API.");
        sb.AppendLine("  The user is interacting with you through a web UI at localhost:5179.");
        if (ctx.PermissionLabel != null)
        {
            sb.AppendLine($"  <permission_level>{ctx.PermissionLabel}</permission_level>");
            sb.AppendLine(ctx.PermissionLabel switch
            {
                "Observe" => "  You may only observe and describe. Do not suggest actions or create content.",
                "Suggest" => "  You may suggest actions but must not execute them. Present options and wait for approval.",
                "Assist" => "  You may execute routine actions. Ask before doing anything irreversible or high-impact.",
                "Auto" => "  You have full autonomy. Execute tasks directly without asking for permission. Use available tools proactively.",
                _ => ""
            });
        }
        if (ctx.AvailableSkills.Count > 0)
            sb.AppendLine($"  <available_tools>{string.Join(", ", ctx.AvailableSkills)}</available_tools>");
        sb.AppendLine("</identity>");
        sb.AppendLine();

        sb.AppendLine("<context>");

        // Session stats
        sb.AppendLine("  <session_stats>");
        sb.AppendLine($"    <total_ideas>{ctx.TotalIdeas}</total_ideas>");
        sb.AppendLine($"    <open_questions>{ctx.OpenQuestions}</open_questions>");
        sb.AppendLine($"    <parked_ideas>{ctx.ParkedIdeas}</parked_ideas>");
        sb.AppendLine($"    <stale_threads>{ctx.StaleThreadCount}</stale_threads>");
        if (ctx.Coverage != null)
            sb.AppendLine($"    <search_coverage embedded=\"{ctx.Coverage.Embedded}\" total=\"{ctx.Coverage.Total}\" pct=\"{ctx.Coverage.Pct:F0}\" />");
        sb.AppendLine("  </session_stats>");

        // Coverage warning — model should know if semantic search is degraded
        if (ctx.Coverage != null && ctx.Coverage.Pct < 50)
        {
            sb.AppendLine("  <warning>Only {0}% of nodes have embeddings. Semantic search results may be incomplete. " +
                "Run backfill_embeddings.py to improve coverage.</warning>"
                .Replace("{0}", $"{ctx.Coverage.Pct:F0}"));
        }

        // Relevant ideas (may include similarity scores from semantic search or graph relationship)
        if (ctx.RelevantIdeas.Count > 0)
        {
            sb.AppendLine("  <relevant_ideas>");
            foreach (var idea in ctx.RelevantIdeas)
            {
                var label = idea.Name ?? idea.Value ?? "(unnamed)";
                var status = idea.Status ?? "?";
                var scoreAttr = idea.SimilarityScore.HasValue
                    ? $" similarity=\"{idea.SimilarityScore.Value:F3}\""
                    : "";
                var relAttr = idea.Relationship != null
                    ? $" relationship=\"{idea.Relationship}\""
                    : "";
                sb.AppendLine($"    <{idea.NodeType} status=\"{status}\"{scoreAttr}{relAttr}>{label}</{idea.NodeType}>");
            }
            sb.AppendLine("  </relevant_ideas>");
        }

        // Connected nodes (discovered via graph traversal from resolved entities)
        if (ctx.ConnectedNodes.Count > 0)
        {
            sb.AppendLine("  <connected_nodes>");
            sb.AppendLine("    <!-- Structurally connected via graph edges -->");
            foreach (var node in ctx.ConnectedNodes)
            {
                var label = node.Name ?? node.Value ?? "(unnamed)";
                var status = node.Status ?? "?";
                var relAttr = node.Relationship != null
                    ? $" relationship=\"{node.Relationship}\""
                    : "";
                sb.AppendLine($"    <{node.NodeType} status=\"{status}\"{relAttr}>{label}</{node.NodeType}>");
            }
            sb.AppendLine("  </connected_nodes>");
        }

        // Open threads (ideas that need attention)
        if (ctx.OpenThreads.Count > 0)
        {
            sb.AppendLine("  <open_threads>");
            foreach (var thread in ctx.OpenThreads)
            {
                var label = thread.Name ?? thread.Value ?? "(unnamed)";
                sb.AppendLine($"    <idea status=\"{thread.Status}\">{label}</idea>");
            }
            sb.AppendLine("  </open_threads>");
        }

        // Recent conversation turns (not full history — just enough for flow)
        if (ctx.RecentTurns.Count > 0)
        {
            sb.AppendLine("  <recent_turns>");
            foreach (var turn in ctx.RecentTurns)
            {
                var speaker = turn.Status ?? "?"; // status field holds speaker for turns
                var text = turn.Value ?? "";
                // Truncate very long turns but keep enough for plans/detailed responses
                if (text.Length > 1000)
                    text = text[..1000] + "...";
                sb.AppendLine($"    <turn speaker=\"{speaker}\">{text}</turn>");
            }
            sb.AppendLine("  </recent_turns>");
        }

        // Cross-project ideas (from other sessions/projects, may include similarity scores)
        if (ctx.CrossProjectIdeas.Count > 0)
        {
            sb.AppendLine("  <cross_session_context>");
            sb.AppendLine("    <!-- Related ideas from other sessions -->");
            foreach (var idea in ctx.CrossProjectIdeas)
            {
                var label = idea.Name ?? idea.Value ?? "(unnamed)";
                var status = idea.Status ?? "?";
                var scoreAttr = idea.SimilarityScore.HasValue
                    ? $" similarity=\"{idea.SimilarityScore.Value:F3}\""
                    : "";
                sb.AppendLine($"    <{idea.NodeType} status=\"{status}\"{scoreAttr}>{label}</{idea.NodeType}>");
            }
            sb.AppendLine("  </cross_session_context>");
        }

        sb.AppendLine("</context>");
        sb.AppendLine();

        // Role section based on intent
        sb.AppendLine("<role>");
        sb.AppendLine(GetRoleDescription(ctx.Intent));
        sb.AppendLine("</role>");

        // Response style guidance from the interpreter (optional)
        if (ctx.ResponseGuidance is { } guidance)
        {
            sb.AppendLine();
            sb.AppendLine("<response_style>");
            sb.AppendLine($"  <format>{FormatInstruction(guidance.Format)}</format>");
            sb.AppendLine($"  <length>{LengthInstruction(guidance.Length)}</length>");
            sb.AppendLine($"  <tone>{ToneInstruction(guidance.Tone)}</tone>");
            sb.AppendLine("  Follow these formatting preferences unless the content clearly requires something else.");
            sb.AppendLine("</response_style>");
        }

        return sb.ToString();
    }

    /// <summary>
    /// Build a lightweight agent handoff from an AgentContext.
    /// Contains node references (not full content) + role + stats.
    /// The agent navigates the tree itself for detail.
    /// </summary>
    public AssembledPrompt BuildFromAgentContext(AgentContext ctx)
    {
        var sb = new StringBuilder();

        sb.AppendLine("<agent_context>");
        sb.AppendLine($"  <source>{ctx.SourceAgent}</source>");
        sb.AppendLine($"  <target>{ctx.TargetAgent}</target>");
        sb.AppendLine($"  <intent>{ctx.Intent}</intent>");
        sb.AppendLine($"  <input_mode>{ctx.InputMode}</input_mode>");

        sb.AppendLine("  <stats>");
        sb.AppendLine($"    <total_ideas>{ctx.TotalIdeas}</total_ideas>");
        sb.AppendLine($"    <open_questions>{ctx.OpenQuestions}</open_questions>");
        sb.AppendLine($"    <parked_ideas>{ctx.ParkedIdeas}</parked_ideas>");
        sb.AppendLine("  </stats>");

        // Node references — the agent's starting points
        if (ctx.FocusNodes.Count > 0)
        {
            sb.AppendLine("  <focus_nodes>");
            foreach (var node in ctx.FocusNodes)
            {
                sb.AppendLine($"    <node id=\"{node.NodeId}\" type=\"{node.NodeType}\" " +
                    $"status=\"{node.Status ?? "?"}\">");
                sb.AppendLine($"      <name>{node.Name ?? "(unnamed)"}</name>");
                if (node.Summary != null)
                    sb.AppendLine($"      <summary>{node.Summary}</summary>");
                sb.AppendLine($"    </node>");
            }
            sb.AppendLine("  </focus_nodes>");
        }

        if (ctx.OpenThreads.Count > 0)
        {
            sb.AppendLine("  <open_threads>");
            foreach (var thread in ctx.OpenThreads)
                sb.AppendLine($"    <node id=\"{thread.NodeId}\" " +
                    $"status=\"{thread.Status}\">{thread.Name ?? "(unnamed)"}</node>");
            sb.AppendLine("  </open_threads>");
        }

        sb.AppendLine("  <instructions>");
        sb.AppendLine("    Use GetNodeWithAttributes(id) to read any focus node in detail.");
        sb.AppendLine("    Use GetChildren(id) to see sub-items.");
        sb.AppendLine("    Use GetParent(id) to see broader context.");
        sb.AppendLine("    Only pull what you need — don't fetch the full tree.");
        sb.AppendLine("  </instructions>");

        sb.AppendLine("</agent_context>");
        sb.AppendLine();
        sb.AppendLine("<role>");
        sb.AppendLine(ctx.RolePrompt);
        sb.AppendLine("</role>");

        return new AssembledPrompt(
            ctx.Intent,
            ctx.TargetAgent,
            sb.ToString(),
            ctx.UserInput);
    }

    public static string GetRoleDescriptionStatic(Intent intent) =>
        GetRoleDescription(intent);

    private static string GetRoleDescription(Intent intent) => intent switch
    {
        Intent.Ideation =>
            "You are a technical sounding board. Engage with the idea, ask clarifying questions, " +
            "and push back on weak reasoning. The user tends to start things and not finish — " +
            "if this opens a new thread, flag it and ask if they want to park something else first.",

        Intent.Deepening =>
            "The user wants to go deeper on a specific idea. Focus your response on that idea. " +
            "Provide concrete details, tradeoffs, and implementation considerations. " +
            "Don't introduce new tangents.",

        Intent.Planning =>
            "The user wants to turn an idea into an actionable plan. Break it down into " +
            "concrete steps with clear deliverables. Identify risks and dependencies. " +
            "Use the plan format: numbered steps, each with a clear outcome.",

        Intent.Reviewing =>
            "You are providing a second opinion. Be constructively critical. " +
            "Point out assumptions, edge cases, and things the user might be missing. " +
            "It's more helpful to find problems than to agree.",

        Intent.Executing =>
            "The user wants to write code or execute a plan step. Focus on implementation. " +
            "Use the code context provided to understand what exists. " +
            "Write clean, minimal code that does exactly what's needed.",

        Intent.Recalling =>
            "The user is trying to recall a previous idea or discussion. " +
            "Summarize what was discussed, what was decided, and the current status. " +
            "Be concise — they want a refresher, not a replay.",

        Intent.Parking =>
            "Acknowledge the user wants to shelve this idea. Confirm what's being parked " +
            "and ensure nothing is lost. The idea will be tracked and resurfaced later.",

        Intent.Resuming =>
            "The user wants to revisit a previously parked or stale idea. " +
            "Brief them on where they left off, what was decided, and what's still open. " +
            "Then ask what angle they want to explore.",

        _ => "You are a helpful assistant."
    };

    private static string FormatInstruction(string format) => format switch
    {
        "bullets" => "Use bullet points. No prose paragraphs.",
        "code" => "Respond with code. Use comments for explanation, not prose around the code.",
        "structured" => "Use a structured format: headings, numbered steps, or tables as appropriate.",
        "prose" => "Respond in natural prose paragraphs.",
        _ => "Respond in natural prose paragraphs."
    };

    private static string LengthInstruction(string length) => length switch
    {
        "brief" => "Keep it short — a few bullet points or 2-3 sentences max.",
        "detailed" => "Be thorough. Cover edge cases, tradeoffs, and implementation details.",
        "moderate" => "Moderate length — enough detail to be useful without over-explaining.",
        _ => "Moderate length — enough detail to be useful without over-explaining."
    };

    private static string ToneInstruction(string tone) => tone switch
    {
        "casual" => "Casual, conversational. Match the user's energy.",
        "technical" => "Precise technical language. Reference specific technologies and patterns.",
        "explanatory" => "Teaching mode. Define terms, explain reasoning, build understanding.",
        _ => "Casual, conversational. Match the user's energy."
    };
}

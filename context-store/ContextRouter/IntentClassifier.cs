// CodeStoragePoc - Intent Classifier
//
// Pattern-based intent classification from user input text.
// Fast, deterministic, no API cost. Falls back to Ideation for ambiguous input.
//
// This is the first stage of the context router pipeline:
//   Input text → IntentClassifier → Intent enum
//   → ContextAssembler uses intent to decide which DB queries to run
//   → PromptBuilder formats the result for a specific model
//
// Transport-agnostic: works identically on voice transcripts and typed text.
//
// Depends on: Types.cs
// Used by:    ContextAssembler, Program

namespace CodeStoragePoc.ContextRouter;

public class IntentClassifier
{
    // Pattern groups ordered by specificity (most specific first)
    private static readonly (Intent Intent, string[] Patterns)[] Rules =
    {
        (Intent.Parking, new[]
        {
            "park that", "park this", "shelve", "put that aside",
            "come back to that later", "not now", "table that",
            "save for later", "back burner"
        }),
        (Intent.Resuming, new[]
        {
            "revisit", "come back to", "pick up where",
            "what about that idea", "remember when we",
            "let's go back to", "reopen", "un-park"
        }),
        (Intent.Recalling, new[]
        {
            "what did i say about", "what did we decide",
            "when did we talk about", "remind me",
            "what was that idea", "did i mention",
            "what's the status of", "where did we leave"
        }),
        (Intent.Planning, new[]
        {
            "let's plan", "make a plan", "break it down",
            "what are the steps", "how would we implement",
            "create a plan", "plan out", "turn this into steps",
            "what would it take"
        }),
        (Intent.Executing, new[]
        {
            "write the code", "implement", "build it",
            "let's code", "start coding", "create the",
            "write a function", "add the method"
        }),
        (Intent.Reviewing, new[]
        {
            "what do you think of", "review this",
            "second opinion", "does this make sense",
            "is this a good idea", "evaluate",
            "critique", "push back on"
        }),
        (Intent.Deepening, new[]
        {
            "tell me more about", "elaborate on",
            "dig deeper", "what about the details",
            "how exactly would", "specifically how",
            "drill into", "unpack that"
        })
        // Ideation is the default — no patterns needed
    };

    /// <summary>
    /// Classify user input into an intent.
    /// Returns Ideation as the default for unmatched input.
    /// </summary>
    public IntentResult Classify(string userInput)
    {
        var lower = userInput.ToLowerInvariant();

        foreach (var (intent, patterns) in Rules)
        {
            foreach (var pattern in patterns)
            {
                if (lower.Contains(pattern))
                {
                    return new IntentResult(intent, pattern, Confidence.High);
                }
            }
        }

        // Default: ideation (safest — broadest context)
        return new IntentResult(Intent.Ideation, null, Confidence.Default);
    }
}

public enum Confidence { High, Medium, Default }

public record IntentResult(Intent Intent, string? MatchedPattern, Confidence Confidence);

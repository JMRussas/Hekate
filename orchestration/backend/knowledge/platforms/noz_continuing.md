# NoZ Continuing Development — Platform Knowledge

Rules and conventions for agents continuing work on existing NoZ projects (DnD, OrcKing, etc.). This supplements the base `noz.md` platform knowledge with project-continuation-specific guidance.

## Mandatory Pre-Work

Before writing any code on an existing NoZ project:

1. **Read `CLAUDE.md`** at the project root — contains build commands, project structure, and component overview
2. **Read `.claude/` docs** — especially `architecture.md`, `anti-patterns.md`, and any `decision-log.md`
3. **Read existing source files** you will modify — understand current patterns before changing them
4. **Check `PLAN.md`** if it exists — understand current sprint state and what has been built
5. **Check `.claude/decision-log.md`** — architecture decisions are already settled, do not re-decide them

## Integration Rules

### Use the Game.cs Hub

Every NoZ project has a central `Game.cs` that orchestrates all systems. When adding new functionality:

- Register new systems through `Game.cs` initialization
- Access shared state through the hub, not via static singletons
- Do not create parallel entry points or alternative initialization flows

```csharp
// CORRECT — integrate through Game.cs
public class Game
{
    private readonly CombatSystem _combat;
    private readonly QuestSystem _quests;  // New system added here

    public Game()
    {
        _combat = new CombatSystem();
        _quests = new QuestSystem(_combat);  // Wired through hub
    }
}

// WRONG — parallel static entry point
public static class QuestManager
{
    private static QuestSystem _instance;
    public static void Initialize() { ... }  // Bypasses Game.cs
}
```

### Match Existing Conventions

- **Naming**: follow whatever the project already uses — check 2-3 existing files to confirm the pattern
- **File organization**: place new files in the same directory structure as similar existing files
- **Error handling style**: match how the project handles errors (exceptions, result types, error codes)
- **Configuration approach**: use the same config mechanism the project already uses

### Do Not Restructure

- Do not move files to "better" locations
- Do not rename existing directories
- Do not reorganize namespace hierarchies
- Do not refactor code that is not part of your current task

## Known Anti-Patterns

These are documented issues from existing NoZ projects. Check for them in any code you write or modify.

### AP-03: Duplicate Button IDs

**Problem**: Two or more `UI.Button` calls use the same ID string in the same frame. One button silently stops receiving input.

**Detection**: Search for `UI.Button("` with the same string literal appearing multiple times, especially inside loops or repeated UI blocks.

**Fix**: Every button ID must be unique per frame. Use item IDs, indices, or context prefixes:
```csharp
// In loops: include unique identifier
if (UI.Button($"action_{enemy.Id}", "Attack")) { ... }

// In repeated panels: prefix with panel context
if (UI.Button($"{panelId}_confirm", "OK")) { ... }
```

### AP-04: Graphics.Draw Outside UI.Scene

**Problem**: `Graphics.Draw` called outside a `UI.Scene` callback targets the wrong render target, causing rendering corruption.

**Detection**: Any `Graphics.Draw`, `Graphics.DrawSprite`, or similar draw call that is not inside the lambda passed to `UI.Scene()`.

**Fix**: Move all draw calls inside the UI.Scene callback. If you need to draw from a system, pass the draw context through:
```csharp
UI.Scene("game", () =>
{
    _renderSystem.Draw();  // Draw calls happen inside the callback
});
```

### AP-05: LINQ in Update Hot Path

**Problem**: LINQ methods allocate delegates and iterators every call, causing GC pressure at 90+ FPS.

**Detection**: Any `.Where(`, `.Select(`, `.ToList()`, `.Any(`, `.First(`, `.OrderBy(` etc. inside `Update()`, render methods, or any code path that runs per-frame.

**Fix**: Replace with manual loops and pre-allocated buffers:
```csharp
// Pre-allocate once
private readonly List<Entity> _activeBuffer = new();

// In Update — zero allocation
_activeBuffer.Clear();
for (int i = 0; i < _entities.Count; i++)
{
    if (_entities[i].IsActive)
        _activeBuffer.Add(_entities[i]);
}
```

## MCP Tools Available

When working on NoZ projects, these MCP tools may be available for validation:

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `build` | Compile the project | After any code change |
| `capture_screen` | Screenshot the running game | After visual changes to verify rendering |
| `validate_layout` | Check UI element counts, layout issues | After UI changes |
| `check_accessibility` | Verify UI element tree structure | After adding new UI panels |
| `get_design` | Fetch design specs from Pencil | When implementing UI from designs |

Use `build` after every code change to catch compilation errors early. Use `capture_screen` after visual changes to verify the result matches intent.

## Sprint Continuation Rules

When continuing work on an in-progress sprint:

1. **Read the existing `PLAN.md`** — understand what has been completed and what remains
2. **Continue numbering** — if the plan has tasks 1-5 completed, your new work starts at task 6
3. **Build on existing systems** — do not rewrite or replace systems built in earlier tasks
4. **Maintain the plan format** — update `PLAN.md` with the same structure (task numbering, status markers, descriptions)
5. **Check test state** — run existing tests before making changes to establish a baseline
6. **Update docs at close-out** — after completing work, update `CLAUDE.md`, `.claude/` docs, and file dependency headers for any files you created or significantly modified

### Sprint Handoff Checklist

Before marking a task complete:

- [ ] Code compiles without errors (`dotnet build`)
- [ ] No new instances of AP-03, AP-04, AP-05 anti-patterns introduced
- [ ] New files have dependency headers
- [ ] Game.cs updated if new systems were added
- [ ] `PLAN.md` updated with completion status
- [ ] `.claude/` docs updated if architecture changed

## Project-Specific Conventions Lookup

Each project may have additional conventions. Always check:

| File | Contains |
|------|----------|
| `CLAUDE.md` | Build commands, structure overview, component list |
| `.claude/architecture.md` | System design, data flow, component relationships |
| `.claude/anti-patterns.md` | Project-specific anti-patterns beyond the standard set |
| `.claude/decision-log.md` | Settled architecture decisions — do not re-decide |
| `.claude/prompt-engineering.md` | LLM prompting conventions if the project uses AI |
| `PLAN.md` | Current sprint plan and task status |

If a `.claude/` doc contradicts this platform knowledge doc, the project-specific doc takes precedence — it reflects deliberate project decisions.

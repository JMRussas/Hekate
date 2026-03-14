# NoZ Engine — Platform Knowledge

## Engine Overview

NoZ is a C#/.NET 10 game engine built on SDL3 with WebGPU rendering. It uses an **immediate-mode UI** system — UI is rebuilt every frame from code, not retained as persistent objects. There is no scene graph editor; all game state and layout is code-driven.

## Project Structure

```
game/                    # Game-specific code (scenes, systems, components)
platform/desktop/        # Desktop entry point (Program.cs, .csproj)
assets/                  # Raw assets (sprites, fonts, audio, data files)
library/                 # Compiled asset cache (auto-generated, do not edit)
tools/                   # CLI, MCP server, UI capture, design check scripts
CLAUDE.md                # Project entry point
.claude/                 # Deep-dive docs, anti-patterns, decision log
```

## Build & Run

```bash
# Build
dotnet build platform/desktop/Desktop.csproj

# Run
dotnet run --project platform/desktop/Desktop.csproj

# Build + run (common during dev)
dotnet run --project platform/desktop/Desktop.csproj --configuration Debug
```

The engine submodule lives outside the game project. Do not modify engine code unless explicitly asked.

## Architecture Patterns

### GameVtable Bridge

Games implement a `GameVtable` struct that the engine calls into. This is the bridge between engine and game code. The game registers callbacks for lifecycle events (Init, Update, Draw, Shutdown). All game initialization flows through this — do not create alternative entry points.

### Game.cs Hub

Each game has a central `Game.cs` that wires up systems, manages global state, and acts as the orchestration hub. All new systems integrate through `Game.cs`. Never create parallel entry points or static singletons that bypass this hub.

### Immediate-Mode UI Lifecycle

UI is declared every frame inside a `UI.Scene` callback. The pattern:

```csharp
UI.Scene("main", () =>
{
    UI.Panel("header", () =>
    {
        UI.Label("title", "Game Title");
        if (UI.Button("play_btn", "Play"))
        {
            StartGame();
        }
    });
});
```

Key rules:
- UI elements exist only during the frame they are declared
- State is tracked by ID strings, not by object references
- Layout is computed top-down within the callback

### Asset Compilation Pipeline

Raw assets in `assets/` are compiled to optimized formats in `library/` at build time. The pipeline handles:
- Sprite sheets → texture atlases (texture_2d_array)
- Font files → SDF glyph atlases
- Audio → compressed format
- Data files → binary serialized

Never reference raw asset paths at runtime. Always use compiled asset handles.

## Critical Rules

### UI.Scene Must Be Last/Only Child

`UI.Scene` must be the last (or only) child element in its container. Adding elements after `UI.Scene` in the same container causes undefined layout behavior. The scene callback consumes remaining space.

```csharp
// CORRECT
UI.Panel("root", () =>
{
    UI.Label("info", "Header text");
    UI.Scene("game_scene", () => { /* ... */ });
});

// WRONG — element after UI.Scene
UI.Panel("root", () =>
{
    UI.Scene("game_scene", () => { /* ... */ });
    UI.Label("info", "Footer text");  // BUG: will not render correctly
});
```

### Button IDs Must Be Globally Unique Per Frame

Every `UI.Button` call in a single frame must have a unique ID string. Duplicate IDs cause **silent input loss** — one of the buttons will not respond to clicks, with no error or warning.

```csharp
// WRONG — duplicate ID in a loop
foreach (var item in items)
{
    if (UI.Button("buy_btn", $"Buy {item.Name}"))  // Same ID every iteration!
        Buy(item);
}

// CORRECT — unique ID per instance
foreach (var item in items)
{
    if (UI.Button($"buy_btn_{item.Id}", $"Buy {item.Name}"))
        Buy(item);
}
```

### No LINQ in Update/Render Hot Paths

LINQ methods (`.Where()`, `.Select()`, `.ToList()`, `.Any()`, `.FirstOrDefault()`, etc.) allocate delegate objects and iterators every call. In Update or render code running 90+ FPS, this causes GC pressure and frame hitches.

```csharp
// WRONG — allocates every frame
var activeUnits = units.Where(u => u.IsActive).ToList();

// CORRECT — pre-allocated list, manual iteration
activeBuffer.Clear();
for (int i = 0; i < units.Count; i++)
{
    if (units[i].IsActive)
        activeBuffer.Add(units[i]);
}
```

### Graphics.Draw Only Inside UI.Scene Callback

All `Graphics.Draw` calls must occur inside a `UI.Scene` callback. Drawing outside this context causes rendering corruption — draw calls may target the wrong render target or produce visual artifacts with no error.

### Max 8192 UI Elements Per Frame

The UI system supports a maximum of 8192 elements per frame. Exceeding this limit causes silent truncation — elements beyond the limit simply don't appear. For complex UIs (inventories, lists), use virtualized scrolling to stay under budget.

### Frame Time Budget: <11ms

Target frame time is under 11ms (90 FPS). Profile with the engine's built-in frame timer. Common budget allocation:
- Game logic/Update: ~4ms
- UI layout + rebuild: ~3ms
- Rendering: ~3ms
- Headroom: ~1ms

### Sprite Shader Texture Binding

Sprite rendering uses `texture_2d_array` bindings. When creating custom shaders or modifying sprite rendering:
- Textures must be bound as `texture_2d_array`, not `texture_2d`
- The array index selects the atlas page
- Mismatched binding types cause shader compilation failures or black sprites

### Label Alignment

`UI.Label` defaults to **left-aligned** text regardless of parent container alignment. To center label text, you must explicitly set the text alignment property. A centered parent container centers the label *element* but not the text *within* it.

## Performance Budgets

| Resource | Budget | Detection |
|----------|--------|-----------|
| Frame time | <11ms | Built-in frame timer overlay |
| UI elements | <8192/frame | Silent truncation if exceeded |
| Draw calls | <500/frame | Batch sprites via atlases |
| GC allocations in hot path | 0 bytes/frame | .NET GC profiler, watch for LINQ/string concat |
| Texture memory | <2GB VRAM | Atlas packing, mip levels |

## Common Anti-Patterns

| ID | Anti-Pattern | Symptom | Detection Hint |
|----|-------------|---------|----------------|
| AP-03 | Duplicate button IDs | Buttons don't respond to clicks | Search for `UI.Button` calls with same string literal in loops or repeated blocks |
| AP-04 | Graphics.Draw outside UI.Scene | Visual artifacts, wrong render target | Any `Graphics.Draw` call not inside a `UI.Scene` lambda |
| AP-05 | LINQ in Update | Periodic frame hitches, rising GC count | `.Where(`, `.Select(`, `.ToList()` etc. inside `Update()` or render methods |
| AP-06 | String concatenation in hot path | GC pressure | `$"..."` or `+` string ops inside Update/render |
| AP-07 | New object allocation in Update | GC pressure | `new List<>()`, `new Dictionary<>()` etc. per frame |
| AP-08 | Element after UI.Scene | Broken layout | Any UI element declared after `UI.Scene()` in the same container |

## NoZ-Specific Conventions

- **Naming**: `PascalCase` for types and methods, `camelCase` for locals, `_camelCase` for private fields
- **File per type**: One public class/struct per file, filename matches type name
- **No service locator**: All dependencies via constructor injection
- **Systems over inheritance**: Prefer composition and system-based architecture over deep class hierarchies

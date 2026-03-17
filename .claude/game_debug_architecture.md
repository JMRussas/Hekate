# Game Debug Architecture

Agent-driven game debugging, testing, and automation via a unified debug protocol. Games expose structured state over HTTP; a central broker manages sessions; agents interact through MCP tools.

## Problem

Hekate orchestrates code generation and static verification (LLM reads output). It cannot run games, observe runtime behavior, or diagnose bugs dynamically. The verify step is "does the code look right?" not "does it work?"

Agents need to:
- Launch game instances (headless or windowed)
- Inspect runtime state (game variables, UI tree, performance)
- Inject input (keyboard, mouse, gamepad)
- Step through frames deterministically
- Take screenshots for visual verification
- Record and replay input sessions
- Set conditional breakpoints
- Do all of this autonomously, across multiple game instances simultaneously

## Architecture Overview

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Agent A     │  │  Agent B     │  │  Agent C     │
│  (executor)  │  │  (executor)  │  │  (Claude Code)│
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       │       MCP tools: game_debug_*    │
       ▼                 ▼                 ▼
┌─────────────────────────────────────────────────┐
│            Game Debug MCP Server                 │
│                                                  │
│  Broker: session registry + process management   │
│  ┌─────────────────────────────────────────┐    │
│  │ abc123 → localhost:9501 (TowerDefense)  │    │
│  │ def456 → localhost:9502 (PuzzleGame)    │    │
│  │ ghi789 → localhost:9503 (TowerDefense)  │    │
│  └─────────────────────────────────────────┘    │
└────────┬──────────────┬──────────────┬──────────┘
         │              │              │
    HTTP requests   HTTP requests  HTTP requests
         │              │              │
         ▼              ▼              ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │ Game :9501│  │ Game :9502│  │ Game :9503│
   │ Tower Def │  │ Puzzle   │  │ Tower Def │
   │ (headless)│  │ (windowed)│  │ (headless)│
   └──────────┘  └──────────┘  └──────────┘
```

Three layers: debug protocol (engine), broker (orchestration), MCP tools (agent interface).

## Layer 1: Debug Protocol (NoZ Engine, C#)

Each game process embeds a lightweight HTTP server exposing structured debug endpoints. The engine already has `NullPlatform`, `NullGraphicsDriver`, `NullAudioDriver`, and `CommandLineApplication` for headless execution. The debug server adds runtime introspection on top.

### Engine Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `DebugServer` | `engine/src/debug/DebugServer.cs` | HTTP listener, endpoint routing, broker registration |
| `DebugState` | `engine/src/debug/DebugState.cs` | State path registry (get/set game variables) |
| `ElementTree.Debug.cs` | `engine/src/ui/ElementTree.Debug.cs` | UI tree JSON serialization |
| Input injection | `platform/cli/NullPlatform.cs` | Synthetic PlatformEvent firing via OnEvent |
| Frame stepping | `engine/src/Application.cs` | Step mode: advance N frames on demand |

### Debug Server Startup

```csharp
// Game's Program.cs or Application init
if (args.Contains("--debug") || args.Contains("--headless"))
{
    int port = ParsePort(args) ?? 0;  // 0 = auto-assign
    DebugServer.Start(port);
}
```

Two launch modes:
- `--headless` — NullPlatform + NullGraphicsDriver + debug server. Max speed, no window. For CI and automated testing.
- `--debug-port=N` — Real SDL3 rendering + debug server. Human sees the game, agents inspect/control it. For interactive debugging.

### HTTP Debug Endpoints

All endpoints return JSON. The game process owns these routes.

| Endpoint | Method | Request | Response | Notes |
|----------|--------|---------|----------|-------|
| `/debug/ping` | GET | — | `{"alive":true,"frame":1235,"game":"TowerDefense","uptime_s":42.5}` | Heartbeat, used by broker |
| `/debug/state?path=X` | GET | — | `{"path":"player.hp","value":100,"type":"int"}` | Read registered state |
| `/debug/state` | POST | `{"path":"player.hp","value":50}` | `{"ok":true}` | Write registered state |
| `/debug/state/all` | GET | — | `{"player.hp":100,"player.position":"(3,5)","wave.current":3,...}` | Dump all registered paths |
| `/debug/ui` | GET | — | UI element tree JSON (see below) | Full tree with IDs, types, bounds, text |
| `/debug/ui/find?id=buy_btn` | GET | — | Matching elements (supports glob: `buy_*`) | Search UI tree |
| `/debug/input` | POST | `[{"type":"key","code":"Space","down":true}]` | `{"ok":true,"frame":1234}` | Inject input events |
| `/debug/input/click` | POST | `{"x":640,"y":360,"button":"Left"}` | `{"ok":true,"frame":1234}` | Convenience: mouse click at position |
| `/debug/input/type` | POST | `{"text":"hello"}` | `{"ok":true}` | Type text into focused input |
| `/debug/frame/step?count=1` | POST | — | `{"frame":1236,"dt":0.016}` | Advance N frames (game must be paused) |
| `/debug/frame/pause` | POST | — | `{"paused":true,"frame":1235}` | Freeze game loop |
| `/debug/frame/resume` | POST | — | `{"paused":false}` | Resume game loop |
| `/debug/frame/info` | GET | — | `{"frame":1235,"paused":false,"dt":0.011,"time_scale":1.0}` | Current frame state |
| `/debug/screenshot` | GET | — | PNG bytes (Content-Type: image/png) | Capture framebuffer |
| `/debug/perf` | GET | — | `{"fps":90,"frame_ms":11.1,"draw_calls":180,"ui_elements":2400,"gc_allocs_bytes":0,"batches":45}` | Performance counters |
| `/debug/record/start` | POST | — | `{"recording_id":"rec_001"}` | Start input recording |
| `/debug/record/stop` | POST | — | `{"recording_id":"rec_001","frames":450,"events":128}` | Stop recording, save to file |
| `/debug/replay` | POST | `{"recording_id":"rec_001","speed":1.0}` | Streams frame state as NDJSON | Replay recorded session |
| `/debug/breakpoint` | POST | `{"condition":"player.hp <= 0"}` | `{"id":"bp_1"}` | Pause when condition is true |
| `/debug/breakpoint/delete` | POST | `{"id":"bp_1"}` | `{"ok":true}` | Remove breakpoint |
| `/debug/breakpoints` | GET | — | `[{"id":"bp_1","condition":"player.hp <= 0","hit_count":0}]` | List breakpoints |
| `/debug/log?tail=50&filter=Error` | GET | — | `[{"level":"Error","msg":"...","frame":1200},...]` | Recent log entries |
| `/debug/shutdown` | POST | — | `{"ok":true}` | Graceful shutdown |

### State Registration

Games register debuggable state via paths. This is explicit — no reflection magic by default, opt-in per field.

```csharp
// In Game.Init() or system initialization
DebugState.Register("player.hp",
    getter: () => player.HP,
    setter: v => player.HP = Convert.ToInt32(v));

DebugState.Register("player.position",
    getter: () => $"({player.Position.X},{player.Position.Y})");
    // no setter = read-only

DebugState.Register("wave.current",
    getter: () => waveManager.CurrentWave);

DebugState.Register("scene",
    getter: () => Game.CurrentScene.Name,
    setter: v => Game.LoadScene(v.ToString()));
```

Optional attribute-based registration for convenience:

```csharp
[Debuggable]
public class Player
{
    [DebugPath("player.hp")]
    public int HP;

    [DebugPath("player.position")]
    public Vector2 Position { get; }

    [DebugPath("player.state")]
    public string State;
}
```

### UI Tree Serialization

The engine's `ElementTree` already tracks all UI elements with IDs, types, and layout. Serialize to JSON for agent inspection:

```json
{
  "frame": 1235,
  "element_count": 2400,
  "root": {
    "id": "main_scene",
    "type": "Scene",
    "bounds": {"x":0, "y":0, "w":1280, "h":720},
    "children": [
      {
        "id": "header_panel",
        "type": "Panel",
        "bounds": {"x":0, "y":0, "w":1280, "h":60},
        "children": [
          {
            "id": "hp_label",
            "type": "Label",
            "text": "HP: 100",
            "bounds": {"x":10, "y":15, "w":120, "h":30}
          },
          {
            "id": "pause_btn",
            "type": "Button",
            "bounds": {"x":1200, "y":10, "w":60, "h":40},
            "widget_state": {"hovered": false, "pressed": false, "focused": false}
          }
        ]
      }
    ]
  }
}
```

This enables agents to:
- Find buttons by ID and verify they exist
- Detect duplicate IDs (AP-03)
- Check UI element counts against the 8192 budget
- Locate click targets by bounds (for `inject_input/click`)
- Verify label text without screenshots

### Input Injection

`NullPlatform` already has an `OnEvent` action. Input injection fires synthetic events:

```csharp
// Addition to NullPlatform
public void InjectKey(InputCode code, bool down)
{
    OnEvent?.Invoke(new PlatformEvent
    {
        Type = down ? PlatformEventType.KeyDown : PlatformEventType.KeyUp,
        KeyCode = code
    });
}

public void InjectMouse(float x, float y, InputCode button = InputCode.None, bool down = false)
{
    OnEvent?.Invoke(new PlatformEvent
    {
        Type = PlatformEventType.MouseMove,
        MouseX = x, MouseY = y
    });
    if (button != InputCode.None)
    {
        OnEvent?.Invoke(new PlatformEvent
        {
            Type = down ? PlatformEventType.MouseDown : PlatformEventType.MouseUp,
            KeyCode = button, MouseX = x, MouseY = y
        });
    }
}
```

For windowed mode (real SDL3), input injection goes through the same `OnEvent` path, synthesizing events alongside real input.

### Frame Stepping

Add a stepping mode to `Application`:

```csharp
// In Application.cs
private static bool _paused;
private static int _stepsRemaining;

public static void Pause() { _paused = true; _stepsRemaining = 0; }
public static void Resume() { _paused = false; }
public static void Step(int count = 1) { _stepsRemaining = count; }

// In the main loop, before frame processing:
if (_paused && _stepsRemaining <= 0)
{
    Thread.Sleep(1);  // yield CPU while paused
    DebugServer.PollRequests();  // still handle debug HTTP
    continue;  // skip frame
}
if (_stepsRemaining > 0) _stepsRemaining--;
```

Key constraint: while paused, the debug server must still handle HTTP requests. The game loop polls the debug server explicitly during pause.

### Input Recording / Replay

```csharp
public class InputRecording
{
    public string Id { get; init; }
    public List<RecordedEvent> Events { get; } = new();

    public record RecordedEvent(int Frame, PlatformEvent Event);
}
```

Recording captures every `PlatformEvent` with its frame number. Replay injects them at the corresponding frames. Deterministic if the game logic is deterministic (no wall-clock time dependencies).

### Conditional Breakpoints

Breakpoints are evaluated once per frame against registered state:

```csharp
public class Breakpoint
{
    public string Id { get; init; }
    public string Condition { get; init; }  // e.g., "player.hp <= 0"
    public int HitCount { get; set; }
}
```

Condition evaluation uses simple expression parsing against `DebugState` paths: comparisons (`<`, `>`, `<=`, `>=`, `==`, `!=`) and boolean logic (`&&`, `||`). Not a full expression engine — just enough for practical debugging.

When a breakpoint hits, the game pauses and the debug server returns the breakpoint ID in the next `/debug/frame/info` response.

## Layer 2: Game Debug Broker (Python, Hekate Service)

Central service that manages game process lifecycles and routes debug commands. Runs as part of the orchestration backend.

### Broker Responsibilities

1. **Launch** game processes with the right flags and ports
2. **Track** active sessions (session_id → port mapping)
3. **Health-check** sessions via `/debug/ping` heartbeat
4. **Route** debug commands from MCP tools to the right game instance
5. **Cleanup** dead sessions (process crash, timeout)
6. **Auto-register** games that start with `--debug-port` and call the broker

### Session Model

```python
@dataclass
class GameSession:
    id: str                     # short uuid (abc123)
    port: int                   # debug server port
    process: asyncio.subprocess.Process
    project_path: str           # game project directory
    game_name: str              # from /debug/ping response
    mode: str                   # "headless" | "windowed"
    created_at: datetime
    last_ping: datetime
    owner_agent: str | None     # which agent/task launched it
```

### Broker API

```python
class GameDebugBroker:
    async def launch(self, project_path: str, mode: str = "headless",
                     args: list[str] | None = None) -> str:
        """Launch game process, return session_id."""

    async def connect(self, port: int) -> str:
        """Register an externally-launched game, return session_id."""

    async def send(self, session_id: str, method: str, path: str,
                   body: dict | None = None) -> dict:
        """Route HTTP request to game instance."""

    async def screenshot(self, session_id: str) -> bytes:
        """Get screenshot as PNG bytes."""

    async def kill(self, session_id: str) -> None:
        """Terminate game process and deregister."""

    async def list_sessions(self) -> list[dict]:
        """Return all active sessions with status."""

    async def health_check(self) -> None:
        """Ping all sessions, remove dead ones. Called periodically."""
```

### Process Lifecycle

```
launch(project, mode)
    │
    ├─ find free port
    ├─ dotnet run --project {project}/platform/desktop -- --debug-port={port} [--headless]
    ├─ poll /debug/ping until ready (timeout 30s)
    ├─ register session
    └─ return session_id
        │
        ├─ agents use session via MCP tools
        ├─ broker pings every 5s
        ├─ crash detected → session marked dead, event emitted
        │
kill(session_id)  OR  timeout (no commands for 10 min)
    │
    ├─ POST /debug/shutdown (graceful)
    ├─ wait 5s
    ├─ process.terminate() (if still alive)
    └─ deregister session
```

## Layer 3: MCP Tools (Agent Interface)

The Game Debug MCP server exposes all debug capabilities as tools. This is what agents actually call.

### Tool Definitions

**Session Management:**

| Tool | Parameters | Returns |
|------|-----------|---------|
| `launch_game` | `project_path`, `mode?` ("headless"\|"windowed"), `args?` | session_id |
| `list_game_sessions` | — | List of active sessions with status |
| `kill_game` | `session_id` | Confirmation |

**State Inspection:**

| Tool | Parameters | Returns |
|------|-----------|---------|
| `get_game_state` | `session_id`, `path` | Value at path with type |
| `set_game_state` | `session_id`, `path`, `value` | Confirmation |
| `get_all_state` | `session_id` | All registered state paths and values |
| `get_ui_tree` | `session_id`, `filter?` | UI element tree JSON |
| `find_ui_element` | `session_id`, `id_pattern` | Matching elements |
| `get_perf` | `session_id` | Performance counters |
| `get_game_log` | `session_id`, `tail?`, `filter?` | Recent log entries |

**Control:**

| Tool | Parameters | Returns |
|------|-----------|---------|
| `pause_game` | `session_id` | Confirmation + frame number |
| `resume_game` | `session_id` | Confirmation |
| `step_frames` | `session_id`, `count?` | New frame number |
| `inject_input` | `session_id`, `events` | Confirmation + frame |
| `click_ui` | `session_id`, `element_id` | Confirmation (resolves bounds from UI tree, clicks center) |
| `type_text` | `session_id`, `text` | Confirmation |
| `screenshot` | `session_id` | Image (base64 or file path) |

**Recording & Replay:**

| Tool | Parameters | Returns |
|------|-----------|---------|
| `start_recording` | `session_id` | recording_id |
| `stop_recording` | `session_id` | recording_id + stats |
| `replay_recording` | `session_id`, `recording_id`, `speed?` | Frame-by-frame results |

**Breakpoints:**

| Tool | Parameters | Returns |
|------|-----------|---------|
| `set_breakpoint` | `session_id`, `condition` | breakpoint_id |
| `remove_breakpoint` | `session_id`, `breakpoint_id` | Confirmation |
| `list_breakpoints` | `session_id` | Active breakpoints with hit counts |

### Convenience Tool: `click_ui`

Agents shouldn't need to manually look up element bounds and calculate click coordinates. `click_ui` does it:

1. Fetch UI tree
2. Find element by ID
3. Calculate center of bounds
4. Inject mouse move + mouse down + mouse up at that position
5. Step one frame (to process the click)
6. Return result

This is the difference between an agent writing 5 tool calls vs. 1.

### MCP Server Registration

**For Claude Code CLI executors** — register in project `.mcp.json`:

```json
{
  "mcpServers": {
    "game-debug": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "orchestration.backend.services.game_debug_mcp"],
      "env": {
        "BROKER_URL": "http://localhost:5200"
      }
    }
  }
}
```

**Allowed tools in executor:**

```python
# claude_code_executor.py
allowed_tools = "Edit,Write,Read,Glob,Grep,Bash(...),mcp__hekate__*,mcp__game_debug__*"
```

**For API executors** — register `GameDebugTool` in the tool registry, wrapping the broker.

## Hekate Orchestration Integration

### New Task Types

| Type | Purpose | Executor Tools |
|------|---------|---------------|
| `game_test` | Automated testing — launch game, run assertions, report pass/fail | game_debug MCP tools |
| `game_debug` | Investigate a failure — connect to game, inspect state, diagnose | game_debug MCP tools |

### Task Flow

```
Wave N: game_code tasks (write game logic)
    ↓
Wave N+1: game_build_verify (dotnet build + syntax check)
    ↓
Wave N+2: game_test tasks
    │
    ├─ Executor launches game: session_id = launch_game(project, "headless")
    ├─ Agent executes test steps from task description
    │   ├─ click_ui("play_btn")
    │   ├─ step_frames(60)  — wait 1 second of game time
    │   ├─ get_game_state("wave.current") → assert == 1
    │   ├─ screenshot() → visual check
    │   └─ get_perf() → assert frame_ms < 11
    ├─ Report: PASS or FAIL with evidence
    └─ kill_game(session_id)
    │
    ↓ on failure
Sentinel detects test failure
    ↓
game_debug task auto-created
    │
    ├─ Agent launches game with same config
    ├─ Reproduces the failure
    ├─ Inspects state: get_ui_tree, get_game_state, get_game_log
    ├─ Diagnoses root cause
    ├─ Outputs: diagnosis + suggested fix
    └─ kill_game(session_id)
    │
    ↓
Fix task created with diagnosis as context
    ↓
Re-test
```

### Planner Integration

The planner generates test tasks alongside code tasks. Example plan decomposition:

```
Task: "Implement combat system"
  → game_code: "Write combat system with attack, defend, HP tracking"
  → game_test: "Test combat: click attack button, verify enemy HP decreases,
                 verify player HP decreases on enemy turn, verify game over
                 when HP reaches 0"
```

Test task descriptions are natural language — the agent interprets them and maps to debug tool calls. No test script authoring required.

### Sentinel Integration

The sentinel monitors test outcomes as health signals:

- **Test failure rate** — if >50% of game_test tasks fail in a wave, flag the project
- **Performance regression** — if `get_perf` shows frame_ms increasing across test runs
- **Crash detection** — broker reports game crashes, sentinel correlates with recent code changes
- **Flaky tests** — same test passes/fails inconsistently, sentinel flags as unreliable

## Multi-Client Scenarios

| Scenario | Configuration |
|----------|---------------|
| Parallel testing — 3 agents test 3 games | Each `launch_game()` returns different session_id. Fully independent. |
| Same game, different tests — agent A tests combat, agent B tests shop | Two `launch_game()` for same project. Separate processes, separate state. |
| Two agents, one game — one drives, one observes | Share session_id. Reads (get_state, screenshot) are safe. Writes (input, set_state) need agent coordination. |
| Live debugging — human plays, agent watches | `launch_game(mode="windowed")`. Human gets SDL3 window, agent reads state via debug tools. |
| CI pipeline — headless batch | `launch_game(mode="headless")`. No GPU needed. Run all tests, collect results, exit. |
| Remote debugging — game on device, agent on dev machine | Game's debug port exposed on network. `connect(port)` instead of `launch_game()`. |

## Non-NoZ Games

The debug protocol is HTTP — any game engine that hosts an HTTP server with the same endpoints works. The broker and MCP tools don't care about the engine.

| Engine | Integration Path |
|--------|-----------------|
| **NoZ** | Native — engine hosts `DebugServer` directly |
| **Unity** | C# HttpListener in editor or player build, wrapping UnityEngine APIs |
| **Web games** | Debug endpoints alongside game server, or Playwright MCP for browser-based |
| **CLI/text games** | Wrapper process that manages stdin/stdout + debug HTTP server |
| **Any executable** | Generic wrapper: launch process, inject input via OS APIs (SendInput/xdotool), capture screenshots via screen capture |

The generic wrapper (last row) is the lowest-effort path for any game that doesn't expose a debug API. It's less structured (screenshots instead of state trees) but universal.

## Implementation Order

| Phase | Components | Enables |
|-------|-----------|---------|
| **1** | `DebugServer` in engine + `/debug/ping` + `/debug/state` + `/debug/shutdown` | Basic state inspection |
| **2** | Input injection in `NullPlatform` + frame stepping in `Application` + `/debug/frame/*` + `/debug/input` | Deterministic control |
| **3** | UI tree serialization + `/debug/ui` | Structured UI inspection |
| **4** | Broker in orchestration + `launch_game` / `kill_game` / `list_sessions` MCP tools | Multi-session management |
| **5** | Full MCP tool set + `click_ui` convenience tool | Agent-ready interface |
| **6** | `game_test` / `game_debug` task types + planner integration | Automated test generation |
| **7** | Screenshot capture + `/debug/screenshot` | Visual verification |
| **8** | Recording/replay + breakpoints | Advanced debugging |
| **9** | Sentinel integration (test failure detection, performance regression) | Autonomous debug loop |
| **10** | Generic wrapper for non-NoZ games | Universal game automation |

## Design Decisions (Resolved)

Resolved through dual review (Claude + Gemini). Both reviewers independently converged on the same conclusions for most questions.

### D1: Threading Model (CRITICAL — resolve before writing any code)

**Decision:** Game-thread polling with request queuing.

The debug HTTP listener runs on a background thread, but all state reads/writes are deferred to the game thread via a concurrent queue. The queue is drained at a fixed point in the frame loop (after input processing, before Update). During pause, `DebugServer.PollRequests()` drains the queue explicitly. This avoids thread safety issues with game state access while keeping the HTTP server responsive.

This is the standard pattern used by Unity and Unreal debug servers. Do not attempt lock-free concurrent reads of game state — game objects are not designed for it.

### D2: State Path Expressions

**Decision:** Simple comparisons only. Support `==`, `!=`, `<`, `>`, `<=`, `>=`. Parse condition into an opcode at set-time, not per-frame string parsing. No `&&`/`||` — use multiple breakpoints if needed. Strictly whitelist syntax to avoid injection risk from agent-generated expressions.

Cap at 16 breakpoints per session. Evaluation cost is negligible (dictionary lookup + one comparison per breakpoint per frame).

### D3: Screenshot Transport

**Decision:** Save to temp directory, return file path. Base64-encoded 1280x720 PNGs are 500KB+ and poison LLM context. Agent uses the file path with the Read tool (which handles images natively in Claude Code).

For headless mode: screenshots require either (a) a software renderer (wgpu-native CPU backend) or (b) are unavailable with NullGraphicsDriver. Return a clear error when attempted headless without software rendering. Move screenshot support to Phase 3, not Phase 7 — visual verification is too high-value to defer.

### D4: Concurrent Writes

**Decision:** Single-writer advisory locking at the broker level. Broker tracks `owner_agent` per session. Read operations (get_state, get_ui_tree, screenshot, get_perf) are open to any agent. Write operations (inject_input, set_state, step_frames) require the control lock. Returns `409 Conflict` if another agent holds the lock. Lock is released on `kill_game` or idle timeout.

The "two agents, one game" scenario with shared writing is an antipattern — remove it from supported configurations. If a second agent needs to drive the same game, it should launch a separate session.

### D5: Determinism

**Decision:** In headless/step mode, use a fixed `DeltaTime` of `1/60s` per frame, ignoring wall-clock time. This is mandatory for replay determinism. Verify that NoZ's `Time.DeltaTime` is engine-controlled (not `System.DateTime`). In windowed mode, use real elapsed time as normal.

Floating-point comparisons in breakpoints and assertions should use epsilon-based matching, not exact equality. The `wait_for_state` tool (see below) should support both exact and approximate matching.

### D6: Debug Server in Release Builds

**Decision:** Always present in code, never started unless runtime flags are passed. Do NOT use `#if DEBUG` — release-only bugs and performance profiling require debugging optimized builds. The code cost of an un-started HTTP server is zero. Use a build flag `ENABLE_DEBUG_SERVER` for actual shipping/player-facing builds where it must be stripped.

### D7: Debug Server Binding

**Decision:** Bind to `127.0.0.1` only, never `0.0.0.0`. Debug endpoints have no authentication by default. For CI environments with shared runners, the broker generates a random token per session, passes it as `--debug-token=X`, and the game's DebugServer requires it as a header. Cheap to implement, prevents accidental cross-talk.

### D8: Port Allocation

**Decision:** Use port 0 (OS auto-assign) exclusively. The game process writes its assigned port to stdout as a structured line (e.g., `DEBUG_PORT=9501`). The broker parses this during launch. Eliminates TOCTOU race conditions with concurrent launches.

## Required Additions (from review)

### `wait_for_state` Tool (CRITICAL — Phase 5)

Both reviewers independently flagged this as the single most important missing piece. Without it, every test is a polling loop: `step_frames(1)` → `get_game_state("x")` → check → repeat. This burns 5-10x the necessary tokens.

```
wait_for_state(session_id, path, condition, timeout_frames=300)
```

The engine runs frames internally (as fast as possible in headless) until the condition is met or timeout, then returns the final value and frame count. This collapses what would be 50+ tool calls into 1.

### `run_test_sequence` Tool (Phase 5)

Batch tool that accepts a sequence of actions and assertions, executes them in order, and returns all results:

```json
{
  "session_id": "abc123",
  "steps": [
    {"action": "click_ui", "id": "play_btn"},
    {"action": "wait_for_state", "path": "scene", "condition": "== game", "timeout": 120},
    {"action": "step_frames", "count": 60},
    {"action": "get_game_state", "path": "wave.current"},
    {"action": "screenshot"},
    {"action": "get_perf"}
  ]
}
```

Returns array of results. Reduces round-trip latency from N tool calls to 1.

### State Discovery: `/debug/schema` Endpoint

Agents need to know what paths exist before querying them. Without this, agents guess at path names (`player.hp` vs `Player.Health`) and waste tokens.

```
GET /debug/schema
→ {
    "paths": [
      {"path": "player.hp", "type": "int", "writable": true},
      {"path": "player.position", "type": "Vector2", "writable": false},
      {"path": "scene", "type": "string", "writable": true}
    ]
  }
```

The `get_all_state` endpoint partially covers this, but doesn't include type info or writability. Add `/debug/schema` as a dedicated discovery tool.

### Exception Reporting: `/debug/exceptions` Endpoint

Games throw exceptions. The current protocol has `/debug/log` but no structured exception reporting. When an unhandled exception occurs, agents need to know immediately:

```
GET /debug/exceptions
→ [
    {"frame": 1200, "type": "NullReferenceException", "message": "...", "stack": "...", "caught": false}
  ]
```

Also include exception state in `/debug/frame/info` response when an exception occurred on that frame.

### UI Tree Pagination

For complex scenes approaching the 8192 element budget, a full `/debug/ui` dump may be too large for LLM context. Add parameters:

```
GET /debug/ui?depth=2              — limit tree depth
GET /debug/ui?root_id=shop_panel   — subtree only
GET /debug/ui?summary=true         — IDs and types only, no bounds/text
```

### Game State Reset: `/debug/reset`

Test isolation requires resetting to a known state between test runs. Currently the only option is `kill_game` + `launch_game` (~30s startup cost). Add:

```
POST /debug/reset
→ reloads initial scene, clears state to defaults
```

For richer isolation, support save/load state snapshots:

```
POST /debug/snapshot/save    → {"snapshot_id": "snap_001"}
POST /debug/snapshot/load    → {"snapshot_id": "snap_001"}  — restore state
```

### Structured Test Results

Define a standard format for test task output so the sentinel can parse results programmatically:

```json
{
  "test_name": "combat_basic",
  "result": "FAIL",
  "duration_frames": 450,
  "assertions": [
    {"condition": "enemy.hp < 100", "expected": true, "actual": false, "frame": 380},
    {"condition": "player.hp > 0", "expected": true, "actual": true, "frame": 380}
  ],
  "screenshots": ["/tmp/debug/abc123_frame380.png"],
  "perf": {"avg_frame_ms": 8.2, "max_frame_ms": 14.1, "gc_allocs": 0}
}
```

This enables sentinel features: failure rate tracking, flaky test detection, performance regression trending.

### Session Timeout Configuration

Make idle timeout configurable per-session:

```
launch_game(project_path, mode="headless", max_idle_s=60, max_lifetime_s=300)
```

Defaults: 10 min idle for interactive debugging, 60s idle for CI/automated testing. The broker enforces `max_lifetime_s` as a hard ceiling to prevent process leaks.

### Asset Loading Guard in `click_ui`

When an agent sends `click_ui` immediately after `launch_game`, the UI may not be inflated yet. The `click_ui` tool must wait for the UI tree to contain the target element (with bounded retries), not fail instantly. Same for `wait_for_state` when paths aren't registered yet during initialization.

### Sentinel Bus Topics

Add to `bus.py` `VALID_TOPICS`:

```python
"game_session.started"
"game_session.crashed"
"game_session.timeout"
"game_test.completed"
"game_perf.regression"
```

The sentinel can then react: auto-create `game_debug` tasks on crashes, flag performance regressions, track test flakiness.

### DI Container Wiring

`GameDebugBroker` must be registered in `container.py` as a Singleton with dependencies on `Database`, `ProgressManager`, and `SentinelBus`. The MCP tool module wraps the broker singleton.

## Revised Implementation Phases

| Phase | Components | Enables |
|-------|-----------|---------|
| **1** | `DebugServer` (HTTP listener, threading model, broker registration) + `/debug/ping` + `/debug/state` + `/debug/schema` + `/debug/shutdown` | Basic state inspection + discovery |
| **2** | Input injection in `NullPlatform` + frame stepping in `Application` + fixed dt in headless + `/debug/frame/*` + `/debug/input` + `/debug/exceptions` | Deterministic control |
| **3** | UI tree serialization (with depth/root/summary params) + `/debug/ui` + `/debug/ui/find` + screenshot (software renderer investigation) | Structured UI + visual inspection |
| **4** | Broker in orchestration (DI wiring, session management, port auto-assign, auth tokens, configurable timeouts) + `launch_game` / `kill_game` / `list_sessions` MCP tools | Multi-session management |
| **5** | Full MCP tool set + `click_ui` (with asset loading guard) + `wait_for_state` + `run_test_sequence` + single-writer locking | Agent-ready interface |
| **6** | `game_test` / `game_debug` task types + planner integration + structured test result format | Automated test generation |
| **7** | Sentinel bus topics + test failure/flaky detection + performance regression trending | Autonomous debug loop |
| **8** | Recording/replay + breakpoints + `/debug/reset` + state snapshots | Advanced debugging |
| **9** | Generic wrapper for non-NoZ games | Universal game automation |

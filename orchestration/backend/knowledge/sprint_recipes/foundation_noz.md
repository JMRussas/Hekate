# Foundation Sprint — NoZ Engine

## Sprint Goal

Stand up a playable NoZ game project: window opens, a test scene renders, a player entity responds to input, and the camera follows. This sprint produces the skeleton that every subsequent sprint builds on.

## Platform: NoZ (C# / .NET 8)

## Files to Create

| File | Purpose |
|------|---------|
| `game/Game.cs` | Root game class — registers systems, loads initial scene |
| `game/GameVtable.cs` | Virtual method table wiring NoZ engine callbacks |
| `platform/desktop/Program.cs` | Desktop entry point — creates window, starts game loop |
| `platform/desktop/desktop.csproj` | .NET project file referencing NoZ engine and game assembly |
| `game/game.csproj` | Game assembly project file |
| `game/Scenes/TestScene.cs` | Minimal scene with a ground plane and player spawn point |
| `game/Player/PlayerNode.cs` | Player entity node with position, sprite placeholder |
| `game/Player/PlayerInputSystem.cs` | Input handler mapping keyboard/gamepad to player actions |
| `game/Camera/CameraFollowSystem.cs` | Camera system that tracks the player node |

## Task Breakdown

### Task 1: Project Scaffold
- **Type:** code
- **Description:** Create solution structure with `platform/desktop/` and `game/` projects. Set up `desktop.csproj` referencing NoZ engine as a submodule and `game.csproj` as a project reference. Add `.gitignore` for bin/obj.
- **Depends on:** none
- **Affected files:** `platform/desktop/desktop.csproj`, `game/game.csproj`, `.gitignore`

### Task 2: Entry Point & Game Shell
- **Type:** code
- **Description:** Implement `Program.cs` with NoZ window creation (title, resolution from config). Implement `Game.cs` inheriting from NoZ's base game class. Wire `GameVtable.cs` so the engine calls into the game assembly. Game should start and show an empty window with a solid background color.
- **Depends on:** Task 1
- **Affected files:** `platform/desktop/Program.cs`, `game/Game.cs`, `game/GameVtable.cs`

### Task 3: Test Scene
- **Type:** code
- **Description:** Create `TestScene.cs` that builds a simple node hierarchy: a root node, a colored rectangle as ground, and a spawn marker. Register the scene in `Game.cs` as the initial scene. Verify the scene renders when the game launches.
- **Depends on:** Task 2
- **Affected files:** `game/Scenes/TestScene.cs`, `game/Game.cs`

### Task 4: Player Entity
- **Type:** code
- **Description:** Create `PlayerNode.cs` as a NoZ Node subclass with position, a colored rectangle placeholder sprite (no art assets yet), and a `Velocity` property. Spawn the player at the scene's spawn point. Player should appear on screen.
- **Depends on:** Task 3
- **Affected files:** `game/Player/PlayerNode.cs`, `game/Scenes/TestScene.cs`

### Task 5: Input System
- **Type:** code
- **Description:** Create `PlayerInputSystem.cs` that reads keyboard input (WASD/arrows) and gamepad stick via NoZ's input API. Map input to player velocity. Apply velocity to player position each frame with delta-time scaling. Player should move smoothly in four directions.
- **Depends on:** Task 4
- **Affected files:** `game/Player/PlayerInputSystem.cs`, `game/Player/PlayerNode.cs`

### Task 6: Camera Follow
- **Type:** code
- **Description:** Create `CameraFollowSystem.cs` that lerps the camera position toward the player node each frame. Configure follow speed and dead-zone via constants (extract to config later). Camera should smoothly track the player as they move around the test scene.
- **Depends on:** Task 5
- **Affected files:** `game/Camera/CameraFollowSystem.cs`, `game/Game.cs`

### Task 7: Build Verification & Cleanup
- **Type:** integration
- **Description:** Ensure the full solution builds with zero warnings. Run the game and verify: window opens, test scene renders, player moves with input, camera follows. Fix any remaining wiring issues. Add a one-line description to the project README.
- **Depends on:** Task 6
- **Affected files:** `README.md`

## Exit Criteria

1. `dotnet build platform/desktop/desktop.csproj` completes with 0 errors and 0 warnings.
2. Running the game opens a window showing the test scene.
3. WASD/arrow keys move the player entity.
4. Camera smoothly follows the player.
5. No placeholder `TODO` or `throw NotImplementedException()` in shipped code.

## Build Verification Command

```bash
dotnet build platform/desktop/desktop.csproj --configuration Debug
```

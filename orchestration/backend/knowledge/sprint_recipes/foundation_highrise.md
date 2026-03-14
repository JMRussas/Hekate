# Foundation Sprint — Highrise (Unity + Lua)

## Sprint Goal

Stand up a playable Highrise world: server initializes the game session, a player spawns with a controllable avatar, the camera is positioned correctly, and a test environment provides ground and boundaries. This sprint produces the skeleton that every subsequent sprint builds on.

## Platform: Highrise (Unity / Lua scripting)

## Files to Create

| File | Purpose |
|------|---------|
| `Assets/Scripts/Server/GameManager.lua` | Server-side game session lifecycle — init, player join/leave, tick |
| `Assets/Scripts/Server/PlayerSpawner.lua` | Server-side player instantiation and spawn point selection |
| `Assets/Scripts/Client/PlayerController.lua` | Client-side input capture and movement request sending |
| `Assets/Scripts/Client/CameraController.lua` | Client-side camera positioning relative to player avatar |
| `Assets/Scripts/Shared/Config.lua` | Shared constants: move speed, camera offset, spawn position |
| `Assets/Scripts/Shared/Events.lua` | Event name constants for client-server communication |
| `Assets/Scenes/TestWorld.unity` | Unity scene with ground plane, boundaries, lighting, spawn points |
| `Assets/Prefabs/PlayerAvatar.prefab` | Player avatar prefab with collider and default mesh/sprite |

## Task Breakdown

### Task 1: Project Setup & Shared Config
- **Type:** code
- **Description:** Create the folder structure under `Assets/Scripts/` with `Server/`, `Client/`, and `Shared/` directories. Implement `Config.lua` with default values for move speed, camera offset, world bounds, and spawn position. Implement `Events.lua` defining event name strings for player movement, spawn, and despawn.
- **Depends on:** none
- **Affected files:** `Assets/Scripts/Shared/Config.lua`, `Assets/Scripts/Shared/Events.lua`

### Task 2: Test World Scene
- **Type:** code
- **Description:** Create the `TestWorld.unity` scene with a ground plane (scaled appropriately), invisible boundary colliders at the edges, basic directional lighting, and at least two marked spawn points. The scene should feel like a contained playground area.
- **Depends on:** none
- **Affected files:** `Assets/Scenes/TestWorld.unity`

### Task 3: Server Game Manager
- **Type:** code
- **Description:** Implement `GameManager.lua` as the server-side entry point. On `Init`, register event handlers for player join and leave. On player join, invoke the spawner. On player leave, clean up their avatar. Include a server tick function (initially empty) for future game logic.
- **Depends on:** Task 1
- **Affected files:** `Assets/Scripts/Server/GameManager.lua`

### Task 4: Player Spawner
- **Type:** code
- **Description:** Implement `PlayerSpawner.lua` to instantiate a `PlayerAvatar` prefab at a selected spawn point when a player joins. Pick spawn points round-robin or randomly from the scene's marked positions. Broadcast the spawn event to all clients.
- **Depends on:** Task 2, Task 3
- **Affected files:** `Assets/Scripts/Server/PlayerSpawner.lua`, `Assets/Prefabs/PlayerAvatar.prefab`

### Task 5: Client Player Controller
- **Type:** code
- **Description:** Implement `PlayerController.lua` to capture touch/joystick input on the client, compute a movement direction vector, and send it to the server via the movement event. On receiving server position updates, lerp the local avatar to the authoritative position. Movement should feel responsive despite server authority.
- **Depends on:** Task 1, Task 4
- **Affected files:** `Assets/Scripts/Client/PlayerController.lua`

### Task 6: Camera Controller
- **Type:** code
- **Description:** Implement `CameraController.lua` to position the camera at a fixed offset from the local player's avatar. Use the offset values from `Config.lua`. The camera should follow smoothly with lerp-based movement. Handle the case where the player avatar doesn't exist yet (pre-spawn).
- **Depends on:** Task 5
- **Affected files:** `Assets/Scripts/Client/CameraController.lua`

### Task 7: Integration & Smoke Test
- **Type:** integration
- **Description:** Wire all scripts together: attach `GameManager` and `PlayerSpawner` to server context, attach `PlayerController` and `CameraController` to client context. Verify the full flow: server starts, player joins, avatar spawns, input moves avatar, camera follows. Document any manual test steps.
- **Depends on:** Task 6
- **Affected files:** `Assets/Scripts/Server/GameManager.lua`, `Assets/Scripts/Client/PlayerController.lua`

## Exit Criteria

1. Server initializes without errors when the world loads.
2. A player joining triggers avatar spawn at a valid spawn point.
3. Touch/joystick input moves the avatar with server-authoritative position.
4. Camera follows the local player's avatar smoothly.
5. A second player joining sees both avatars and can move independently.
6. No Lua runtime errors in server or client console.

## Testing Note

Highrise games require manual testing in the Unity editor or the Highrise Studio app. There is no headless build verification command. To test:

1. Open the project in Unity with the Highrise SDK installed.
2. Load `Assets/Scenes/TestWorld.unity`.
3. Enter Play mode — this simulates both server and client locally.
4. Use the on-screen joystick or keyboard input to move.
5. For multiplayer testing, use Highrise Studio's multi-client preview or deploy to a test world.

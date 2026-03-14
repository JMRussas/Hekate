# Highrise — Platform Knowledge

## Platform Overview

Highrise is a **mobile-first social metaverse** platform built on Unity. Developers create interactive worlds using a custom Lua scripting layer — not standard Lua. You cannot write C# or access Unity APIs directly. All game logic runs through Highrise's Lua runtime with platform-provided services and components.

Key constraints:
- **Mobile-first**: target low-end phones, minimize complexity
- **No C# access**: all logic is Lua scripts attached to GameObjects
- **Custom Lua flavor**: Highrise-specific annotations, types, and APIs — do not assume standard Lua libraries are available
- **Manual publishing**: worlds are published through the Highrise Studio interface

## Script Types

Every script file starts with a type annotation that determines where it runs and what APIs are available.

```lua
--!Type(Server)           -- Runs on server only
--!Type(Client)           -- Runs on client only
--!Type(ClientAndServer)  -- Runs on both (has prefixed lifecycle methods)
--!Type(Module)           -- Shared utility module, no lifecycle methods
--!Type(UI)               -- UI controller script, client-side
```

### Server Scripts

Run on the authoritative server. Access to server-only services (Storage, Inventory).

```lua
--!Type(Server)

function self:Awake()
    -- Called once when the script initializes
end

function self:Start()
    -- Called after all Awake() calls complete
end

function self:Update()
    -- Called every frame (use sparingly on server)
end

function self:OnDestroy()
    -- Cleanup
end
```

### Client Scripts

Run on each player's device. Access to client-only services (Audio, Input, UI, PlayerPrefs).

```lua
--!Type(Client)

function self:Awake() end
function self:Start() end
function self:Update() end
function self:OnDestroy() end
```

### ClientAndServer Scripts

Run on both. Lifecycle methods are prefixed to distinguish execution context.

```lua
--!Type(ClientAndServer)

-- Server-side lifecycle
function self:ServerAwake() end
function self:ServerStart() end
function self:ServerUpdate() end
function self:ServerOnDestroy() end

-- Client-side lifecycle
function self:ClientAwake() end
function self:ClientStart() end
function self:ClientUpdate() end
function self:ClientOnDestroy() end
```

### Module Scripts

Shared utility code. No lifecycle methods. Imported by other scripts.

```lua
--!Type(Module)

local Utils = {}

function Utils.Clamp(value, min, max)
    return math.max(min, math.min(max, value))
end

return Utils
```

### UI Scripts

Client-side scripts that bind to UXML UI elements.

```lua
--!Type(UI)

--!Bind
local playButton : VisualElement = nil

--!Bind
local scoreLabel : Label = nil

function self:Awake()
    playButton:RegisterCallback(PointerUpEvent, function()
        scoreLabel.text = "Playing!"
    end)
end
```

## Annotations

```lua
--!Type(Server)              -- Script execution context (required, first line)
--!SerializeField            -- Expose next field in Inspector
--!Bind                      -- Bind next variable to UXML element by name
```

### SerializeField Usage

```lua
--!SerializeField
local speed : number = 5.0

--!SerializeField
local targetObject : GameObject = nil

--!SerializeField
local spawnPoint : Transform = nil
```

## Special Globals

| Global | Scope | Purpose |
|--------|-------|---------|
| `self` | All scripts | Reference to the script component instance |
| `client` | Client scripts | The local player's `Client` object |
| `server` | Server scripts | Server context |
| `scene` | Both | Access to the scene hierarchy |
| `defer()` | Both | Schedule a function to run next frame |

## Networking

### Events (Fire-and-Forget)

```lua
-- Define event (in ClientAndServer script)
local OnScoreChanged = Event.new("OnScoreChanged")

-- Server fires to one client
OnScoreChanged:FireClient(targetPlayer, newScore)

-- Server fires to all clients
OnScoreChanged:FireAllClients(newScore)

-- Client fires to server
OnScoreChanged:FireServer(data)

-- Client listens
OnScoreChanged:Connect(function(score)
    UpdateScoreUI(score)
end)

-- Server listens (for client-to-server events)
OnScoreChanged:Connect(function(player, data)
    -- player is automatically provided as first arg
    ProcessScore(player, data)
end)
```

### NetworkValue (Synced State)

```lua
-- Server sets, automatically synced to all clients
--!SerializeField
local health : NetworkValue(IntValue) = NetworkValue.new(IntValue, 100)

-- Server: set value
health.value = 75

-- Client: read value (read-only on client)
local currentHealth = health.value

-- Both: listen for changes
health.Changed:Connect(function(oldVal, newVal)
    UpdateHealthBar(newVal)
end)
```

### RemoteFunction (Request-Response)

```lua
-- Define
local GetPlayerData = RemoteFunction.new("GetPlayerData")

-- Server: handle requests
GetPlayerData:SetCallback(function(player, playerId)
    return Storage.GetValue("player_" .. playerId)
end)

-- Client: call and await response
local data = GetPlayerData:InvokeServer(playerId)
```

## UI System

### UXML Structure

UI layouts use UXML with the `hr:` namespace for Highrise-specific elements.

```xml
<UXML xmlns:hr="Highrise.UI">
    <hr:Panel name="MainPanel" class="panel-dark">
        <hr:Label name="TitleLabel" text="My World" class="title-text" />
        <hr:Button name="PlayButton" text="Play" class="btn-primary" />
        <hr:Image name="Avatar" class="avatar-frame" />
        <hr:ScrollView name="ItemList" class="scroll-container">
            <hr:Panel name="ItemTemplate" class="item-row" />
        </hr:ScrollView>
    </hr:Panel>
</UXML>
```

### USS Styling

```css
.panel-dark {
    background-color: rgba(0, 0, 0, 0.8);
    padding: 16px;
    border-radius: 8px;
}

.title-text {
    font-size: 24px;
    color: white;
    -unity-text-align: middle-center;
    margin-bottom: 12px;
}

.btn-primary {
    background-color: #4CAF50;
    color: white;
    padding: 12px 24px;
    border-radius: 4px;
}
```

### Lua UI Binding

```lua
--!Type(UI)

--!Bind
local PlayButton : VisualElement = nil

--!Bind
local TitleLabel : Label = nil

--!Bind
local ItemList : ScrollView = nil

function self:Awake()
    PlayButton:RegisterCallback(PointerUpEvent, function(evt)
        -- Handle button click
    end)
end

function self:UpdateTitle(text)
    TitleLabel.text = text
end
```

The `--!Bind` variable name must exactly match the UXML element's `name` attribute.

## Available Services

| Service | Scope | Purpose | Key Methods |
|---------|-------|---------|-------------|
| **Audio** | Client | Play sounds/music | `Audio:PlaySound(clip)`, `Audio:PlayMusic(clip)` |
| **Chat** | Both | In-world chat | `Chat:DisplayMessage(msg)`, `Chat.MessageReceived` |
| **Input** | Client | Touch/click detection | `Input:GetAction(name)`, tap/swipe events |
| **Inventory** | Server | Player items | `Inventory:GetItems(player)`, `Inventory:AddItem(player, item)` |
| **Localization** | Both | Translated strings | `Localization:GetString(key)` |
| **Payments** | Both | In-app purchases | `Payments:PromptPurchase(player, product)` |
| **PlayerPrefs** | Client | Local key-value storage | `PlayerPrefs:SetString(k,v)`, `PlayerPrefs:GetString(k)` |
| **Storage** | Server | Persistent server storage | `Storage:GetValue(key)`, `Storage:SetValue(key, val)` |
| **Time** | Both | Game time | `Time.deltaTime`, `Time.time` |
| **UI** | Client | Show/hide UI | `UI:Show(element)`, `UI:Hide(element)` |

### Storage Constraints

- **Rate-limited**: do not call `Storage:GetValue` or `Storage:SetValue` every frame
- Batch reads at Start/Awake, cache locally, write on meaningful state changes
- Keys are strings, values are serialized automatically

## Pre-Built Scripts

These ship with the platform. Attach them to GameObjects in the editor — do not rewrite them.

| Script | Purpose |
|--------|---------|
| `PlayerCharacterController` | Player movement, animation, collisions |
| `ThirdPersonCamera` | Camera follow with orbit controls |
| `Interactable` | Makes objects clickable/tappable |
| `Teleporter` | Moves player to target position |
| `AnimationController` | State machine for animations |
| `NPCController` | Non-player character movement/behavior |

## Code Organization

```
Assets/
├── Scripts/
│   ├── Core/              # Shared utilities, managers
│   ├── UI/                # UI scripts (--!Type(UI))
│   ├── Gameplay/          # Game mechanics
│   │   ├── Combat/
│   │   ├── Inventory/
│   │   └── Quests/
│   └── Modules/           # --!Type(Module) shared code
├── UI/
│   ├── Layouts/           # .uxml files
│   └── Styles/            # .uss files
└── Scenes/
```

Organize by feature, not by script type. Keep Modules separate since they are imported across features.

## Common Patterns

### Event Handler Pattern

```lua
--!Type(ClientAndServer)

local OnItemCollected = Event.new("OnItemCollected")

--!SerializeField
local itemName : string = "Coin"

--!SerializeField
local itemValue : number = 1

function self:ServerStart()
    -- Server handles collision/trigger
    self.gameObject:GetComponent(Trigger).OnTriggerEnter:Connect(function(other)
        local player = other.gameObject:GetComponent(PlayerCharacterController)
        if player then
            OnItemCollected:FireClient(player.client, self.itemName, self.itemValue)
            OnItemCollected:FireAllClients(player.client.name, self.itemValue)
            self.gameObject:SetActive(false)
            -- Respawn after delay
            defer(function()
                Timer.After(10, function()
                    self.gameObject:SetActive(true)
                end)
            end)
        end
    end)
end

function self:ClientStart()
    OnItemCollected:Connect(function(playerName, value)
        -- Update UI, play effects
        print(playerName .. " collected " .. tostring(value) .. " points!")
    end)
end
```

### Module Utility Pattern

```lua
--!Type(Module)

local MathUtils = {}

function MathUtils.Lerp(a, b, t)
    return a + (b - a) * math.max(0, math.min(1, t))
end

function MathUtils.Distance(pos1, pos2)
    local dx = pos2.x - pos1.x
    local dy = pos2.y - pos1.y
    local dz = pos2.z - pos1.z
    return math.sqrt(dx*dx + dy*dy + dz*dz)
end

return MathUtils
```

### UI Binding Pattern

```lua
--!Type(UI)

--!Bind
local CoinCounter : Label = nil

--!Bind
local HealthBar : VisualElement = nil

--!Bind
local ShopButton : VisualElement = nil

local OnCoinUpdate = Event.new("OnCoinUpdate")

function self:Awake()
    OnCoinUpdate:Connect(function(newCount)
        CoinCounter.text = tostring(newCount)
    end)

    ShopButton:RegisterCallback(PointerUpEvent, function()
        -- Open shop panel
    end)
end

function self:UpdateHealth(percent)
    HealthBar.style.width = StyleLength.new(Length.Percent(percent))
end
```

## Key Constraints

- **No coroutines**: use `Timer.After()` and `defer()` for delayed execution
- **No `require()` for non-Module scripts**: only `--!Type(Module)` scripts can be imported
- **No direct Unity API access**: use only Highrise-provided services and components
- **No file system access**: use Storage service for persistence
- **Type annotations are optional but recommended**: `local x : number = 0`
- **`self` is not `this`**: `self` refers to the script component, use `:` method syntax
- **Events are string-identified**: `Event.new("name")` — names must be unique across the project

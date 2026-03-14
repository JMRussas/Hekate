--!Type(Client)

-- Client-side game logic template
-- Handles input, local rendering, audio, and UI display

-- Events for server communication
local gameStateRequest = Event.new("GameStateRequest")
local gameStateUpdate = Event.new("GameStateUpdate")
local playerActionRequest = Event.new("PlayerActionRequest")
local playerActionResult = Event.new("PlayerActionResult")

-- Local state
local localState = {
    isConnected = false,
    gameState = nil,
}

function self:ClientAwake()
    -- Listen for state updates from server
    gameStateUpdate:Connect(function(state)
        localState.gameState = state
        self:UpdateUI()
    end)

    -- Listen for action results
    playerActionResult:Connect(function(result)
        if result.success then
            -- TODO: Play success feedback
        else
            -- TODO: Show error message
        end
    end)

    -- Request initial state
    gameStateRequest:FireServer()
    localState.isConnected = true
end

function self:ClientUpdate()
    -- Per-frame client logic (input polling, animations, etc.)
end

function self:SendAction(action, data)
    data = data or {}
    data.action = action
    playerActionRequest:FireServer(data)
end

function self:UpdateUI()
    -- TODO: Update UI elements based on localState.gameState
end

-- Input handling via TapHandler (attach to 3D objects)
function self:SetupTapHandler(gameObject, callback)
    local tapHandler = gameObject:GetComponent(TapHandler)
    if tapHandler then
        tapHandler.Tapped:Connect(callback)
    end
end

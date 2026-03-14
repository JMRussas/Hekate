--!Type(Server)

-- Server-side game logic template
-- Handles authoritative state, persistence, and client communication

-- Events for client-server communication
local gameStateRequest = Event.new("GameStateRequest")
local gameStateUpdate = Event.new("GameStateUpdate")
local playerActionRequest = Event.new("PlayerActionRequest")
local playerActionResult = Event.new("PlayerActionResult")

-- Game state (server-authoritative)
local gameState = {
    players = {},
    round = 0,
    isActive = false,
}

function self:ServerAwake()
    -- Listen for player connections
    server.PlayerConnected:Connect(function(player)
        self:OnPlayerJoined(player)
    end)

    server.PlayerDisconnected:Connect(function(player)
        self:OnPlayerLeft(player)
    end)

    -- Listen for client requests
    gameStateRequest:Connect(function(player)
        gameStateUpdate:FireClient(player, gameState)
    end)

    playerActionRequest:Connect(function(player, data)
        self:HandlePlayerAction(player, data)
    end)
end

function self:OnPlayerJoined(player)
    gameState.players[player.name] = {
        score = 0,
        isReady = false,
    }
    -- Broadcast updated state to all clients
    gameStateUpdate:FireAllClients(gameState)
end

function self:OnPlayerLeft(player)
    gameState.players[player.name] = nil
    gameStateUpdate:FireAllClients(gameState)
end

function self:HandlePlayerAction(player, data)
    -- Validate and process action server-side
    local action = data.action
    local result = { success = false, message = "" }

    -- TODO: Implement game-specific action handling

    playerActionResult:FireClient(player, result)
end

-- Persistent storage example
function self:SavePlayerData(player, data)
    Storage.SetValue("player_" .. player.name, data, function(errorCode)
        if errorCode then
            print("Save failed for " .. player.name .. ": " .. tostring(errorCode))
        end
    end)
end

function self:LoadPlayerData(player, callback)
    Storage.GetValue("player_" .. player.name, function(value, errorCode)
        if errorCode then
            print("Load failed for " .. player.name .. ": " .. tostring(errorCode))
            callback(nil)
        else
            callback(value)
        end
    end)
end

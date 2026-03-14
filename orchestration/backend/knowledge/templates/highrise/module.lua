--!Type(Module)

-- Shared utility module template
-- Load in other scripts via: local Utils = require("Utils")

local Utils = {}

-- Table operations
function Utils.IsInTable(tbl, value)
    for _, v in ipairs(tbl) do
        if v == value then return true end
    end
    return false
end

function Utils.GetIndexInTable(tbl, value)
    for i, v in ipairs(tbl) do
        if v == value then return i end
    end
    return nil
end

function Utils.RemoveFromTable(tbl, value)
    local index = Utils.GetIndexInTable(tbl, value)
    if index then
        table.remove(tbl, index)
        return true
    end
    return false
end

function Utils.ShallowCopy(tbl)
    local copy = {}
    for k, v in pairs(tbl) do
        copy[k] = v
    end
    return copy
end

function Utils.DeepCopy(obj)
    if type(obj) ~= "table" then return obj end
    local res = {}
    for k, v in pairs(obj) do
        res[k] = Utils.DeepCopy(v)
    end
    return res
end

-- String utilities
function Utils.IsNullOrEmpty(str)
    return str == nil or str == ""
end

-- Math utilities
function Utils.Clamp(value, min, max)
    if value < min then return min end
    if value > max then return max end
    return value
end

function Utils.Lerp(a, b, t)
    return a + (b - a) * Utils.Clamp(t, 0, 1)
end

-- Timer utility
function Utils.FormatTime(seconds)
    local mins = math.floor(seconds / 60)
    local secs = math.floor(seconds % 60)
    return string.format("%d:%02d", mins, secs)
end

return Utils

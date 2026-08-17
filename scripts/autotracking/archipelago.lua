ScriptHost:LoadScript("scripts/autotracking/item_mapping.lua")
ScriptHost:LoadScript("scripts/autotracking/location_mapping.lua")
ScriptHost:LoadScript("scripts/autotracking/setting_mapping.lua")

CUR_INDEX = -1
SLOT_DATA = nil
HINTS_KEY = nil
PLAYER_ID = -1
TEAM_NUMBER = 0

-- AP HintStatus -> PopTracker Highlight enum for the coloured square drawn under
-- a hinted location. Empty (and hint marking is skipped) on builds without hint
-- support, where the Highlight global is absent.
HINT_STATUS_MAPPING = {}
if Highlight then
    HINT_STATUS_MAPPING = {
        [0]  = Highlight.Unspecified,
        [10] = Highlight.NoPriority,
        [20] = Highlight.Avoid,
        [30] = Highlight.Priority,
        [40] = Highlight.None,
    }
end

local function dump_table(value, depth)
    if depth == nil then depth = 0 end
    if type(value) == "table" then
        local indent = ("\t"):rep(depth)
        local inner = ("\t"):rep(depth + 1)
        local text = "{\n"
        for key, entry in pairs(value) do
            local label = key
            if type(label) ~= "number" then label = '"' .. label .. '"' end
            text = text .. inner .. "[" .. tostring(label) .. "] = "
                .. dump_table(entry, depth + 1) .. ",\n"
        end
        return text .. indent .. "}"
    end
    return tostring(value)
end

local function set_stage(code, stage)
    local object = Tracker:FindObjectForCode(code)
    if not object then
        print("Warning: no tracker object for code:", code)
        return
    end
    object.CurrentStage = stage
end

-- A scalar slot_data value onto its setting's stage.
local function apply_slot_setting(key, value)
    local definition = SLOT_CODES[key]
    if not definition then return end
    local mapped = definition.mapping and definition.mapping[value]
    if mapped == nil and type(value) == "boolean" then
        mapped = definition.mapping[value and 1 or 0]
    end
    if mapped == nil then
        print("Warning: no slot mapping for", key, "value", tostring(value))
        return
    end
    set_stage(definition.code, mapped)
end

-- A set-valued slot_data key (the two lock families). Every member starts off,
-- then the keys the seed selected turn on, so a rule can tell a selected lock
-- from an absent one.
local function apply_slot_set(key, value)
    local members = SLOT_SET_CODES[key]
    if not members then return end
    for _, code in pairs(members) do
        set_stage(code, 0)
    end
    if type(value) ~= "table" then return end
    for _, member in ipairs(value) do
        local code = members[member]
        if code then
            set_stage(code, 1)
        else
            print("Warning: unknown", key, "member", tostring(member))
        end
    end
end

function onClear(slot_data)
    if AUTOTRACKER_ENABLE_DEBUG_LOGGING_AP then
        print(string.format("called onClear, slot_data:\n%s", dump_table(slot_data)))
    end
    SLOT_DATA = slot_data
    CUR_INDEX = -1

    for key, value in pairs(slot_data) do
        if SLOT_SET_CODES[key] then
            apply_slot_set(key, value)
        else
            apply_slot_setting(key, value)
        end
    end

    for _, codes in pairs(LOCATION_MAPPING) do
        if codes[1] then
            local object = Tracker:FindObjectForCode(codes[1])
            if object then
                if codes[1]:sub(1, 1) == "@" then
                    object.AvailableChestCount = object.ChestCount
                else
                    object.Active = false
                end
            end
        end
    end

    for _, item in pairs(ITEM_MAPPING) do
        if item[1] and item[2] then
            local object = Tracker:FindObjectForCode(item[1])
            if object then
                if item[2] == "toggle" then
                    object.Active = false
                elseif item[2] == "consumable" then
                    object.AcquiredCount = 0
                end
            end
        end
    end

    PLAYER_ID = Archipelago.PlayerNumber or -1
    TEAM_NUMBER = Archipelago.TeamNumber or 0

    -- The server's read-only hints list for this slot. Subscribing has to happen
    -- from a ClearHandler.
    HINTS_KEY = "_read_hints_" .. TEAM_NUMBER .. "_" .. PLAYER_ID
    Archipelago:Get({HINTS_KEY})
    Archipelago:SetNotify({HINTS_KEY})
end

-- Retrieved (Get reply) and SetReply share one dispatcher; old_value is nil for
-- retrieved replies and for "_read"-prefixed keys.
function onDataStorageUpdate(key, value, _old_value)
    if key == HINTS_KEY then
        onHintsUpdate(value)
    end
end

-- Mark every hinted location in our own world with its Highlight square. A hint
-- sits in the finder's world regardless of who receives the item, so
-- finding_player is the only filter. Found hints carry status None, which clears
-- the square.
function onHintsUpdate(hints)
    if type(hints) ~= "table" then return end
    for _, hint in ipairs(hints) do
        if hint.finding_player == PLAYER_ID then
            updateHint(hint)
        end
    end
end

function updateHint(hint)
    local highlight = hint.status and HINT_STATUS_MAPPING[hint.status]
    if not highlight then
        -- Older AP without hint.status: fall back to the found flag.
        if hint.found == true then
            highlight = Highlight and Highlight.None
        elseif hint.found == false then
            highlight = Highlight and Highlight.Unspecified
        else
            return
        end
    end
    local codes = LOCATION_MAPPING[hint.location]
    if not codes then
        if AUTOTRACKER_ENABLE_DEBUG_LOGGING_AP then
            print(string.format("updateHint: no location mapping for id %s", hint.location))
        end
        return
    end
    for _, code in ipairs(codes) do
        if code:sub(1, 1) == "@" then
            local object = Tracker:FindObjectForCode(code)
            if object and object.Highlight ~= nil then
                object.Highlight = highlight
            end
        end
    end
end

function onItem(index, item_id, item_name, _player_number)
    if index <= CUR_INDEX then return end
    CUR_INDEX = index
    local item = ITEM_MAPPING[item_id]
    if not item or not item[1] then
        if AUTOTRACKER_ENABLE_DEBUG_LOGGING_AP then
            print(string.format("onItem: no mapping for id %s (%s)",
                item_id, item_name or "?"))
        end
        return
    end
    local object = Tracker:FindObjectForCode(item[1])
    if not object then
        print(string.format("onItem: no tracker object for code %s", item[1]))
        return
    end
    if item[2] == "toggle" then
        object.Active = true
    elseif item[2] == "consumable" then
        object.AcquiredCount = object.AcquiredCount + 1
    end
end

function onLocation(location_id, location_name)
    local codes = LOCATION_MAPPING[location_id]
    if not codes or not codes[1] then
        if AUTOTRACKER_ENABLE_DEBUG_LOGGING_AP then
            print(string.format("onLocation: no mapping for id %s (%s)",
                location_id, location_name or "?"))
        end
        return
    end
    for _, code in ipairs(codes) do
        local object = Tracker:FindObjectForCode(code)
        if object then
            if code:sub(1, 1) == "@" then
                object.AvailableChestCount = math.max(0, object.AvailableChestCount - 1)
            else
                object.Active = true
            end
        end
    end
end

Archipelago:AddClearHandler("clear handler", onClear)
Archipelago:AddItemHandler("item handler", onItem)
Archipelago:AddLocationHandler("location handler", onLocation)
Archipelago:AddRetrievedHandler("data retrieved handler", onDataStorageUpdate)
Archipelago:AddSetReplyHandler("data set handler", onDataStorageUpdate)

-- Helpers the generated access rules and visibility rules are written against.
-- access_rules.lua holds one function per check; everything it calls lives here.

function has(item)
    return Tracker:ProviderCountForCode(item) > 0
end

function count(item)
    return Tracker:ProviderCountForCode(item)
end

-- A progressive strand unlock is a counter, so a mission asks for its nth.
function itemAtLeast(item, needed)
    return Tracker:ProviderCountForCode(item) >= needed
end

-- Reachable maps to Normal, otherwise None. Wrapped rather than returned raw so
-- a rule reads as an AccessibilityLevel, which is what the "^$rule" prefix asks
-- PopTracker for.
function reachAccess(reachable)
    if reachable then return AccessibilityLevel.Normal end
    return AccessibilityLevel.None
end

-- A lock family's term. The setting names the key this item belongs to; a key
-- the seed did not select puts no item in the pool and locks nothing, so the
-- term has to read as already satisfied, exactly how the world filters these
-- terms out of its own rules.
function lockTerm(item, setting)
    if not has(setting .. "_on") then return true end
    return has(item)
end

-- How many of a list of boolean clauses hold. The finale's asset prerequisite is
-- a threshold over the optional income assets, so it counts clauses rather than
-- requiring all of them.
function satisfiedCount(clauses)
    local total = 0
    for _, satisfied in ipairs(clauses) do
        if satisfied then total = total + 1 end
    end
    return total
end

-- Seed options, set from slot_data by the autotracker.

function propertiesEnabled()
    return has("enable_properties_on")
end

-- A class's pins show only when the seed made it checks and the player has not
-- hidden the class. Story missions are always on, so only the display toggle
-- gates them.
local function classShown(class_key, option_code)
    if not has("show_" .. class_key .. "_on") then return false end
    if option_code == nil then return true end
    return has(option_code .. "_on")
end

function visStoryMissions()
    return classShown("story_missions", nil)
end

function visProperties()
    return classShown("properties", "enable_properties")
end

function visHiddenPackages()
    return classShown("hidden_packages", "enable_hidden_packages")
end

function visRampages()
    return classShown("rampages", "enable_rampages")
end

function visStuntJumps()
    return classShown("stunt_jumps", "enable_stunt_jumps")
end

function visRobbableStores()
    return classShown("robbable_stores", "enable_robbable_stores")
end

function visSideEvents()
    return classShown("side_events", "enable_side_events")
end

function visEmergencyVehicles()
    return classShown("emergency_vehicles", "enable_emergency_vehicles")
end

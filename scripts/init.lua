ENABLE_DEBUG_LOG = false
AUTOTRACKER_ENABLE_ITEM_TRACKING = true
AUTOTRACKER_ENABLE_LOCATION_TRACKING = true

Tracker:AddItems("items/items.json")

ScriptHost:LoadScript("scripts/logic/logic.lua")
ScriptHost:LoadScript("scripts/logic/access_rules.lua")

Tracker:AddMaps("maps/maps.json")

ScriptHost:LoadScript("scripts/locations_import.lua")

Tracker:AddLayouts("layouts/items.json")
Tracker:AddLayouts("layouts/tabs.json")
Tracker:AddLayouts("layouts/tracker.json")
Tracker:AddLayouts("layouts/broadcast.json")

if PopVersion and PopVersion >= "0.25.0" then
    ScriptHost:LoadScript("scripts/autotracking.lua")
end

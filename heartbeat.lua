--[[
    GARNT TOOL REJOIN - heartbeat.lua  (SAMPLE, generated for one instance)
    Instance ID : com.roblox.client
    Receiver    : http://127.0.0.1:8765/hb
    Interval    : 15s (config: heartbeat.script_interval)
    WARNING     : executors (request / syn.request / http_request) may violate the
                  Roblox Terms of Use. Use this only on your own accounts and devices,
                  and accept the ban risk yourself.
    NOTE        : The tool rewrites the CONFIG block per instance when you choose
                  "Generate .lua files" in the Heartbeat submenu.
--]]

-- guard against double execution
if _G.GARNT_HEARTBEAT_RUNNING then
    return
end
_G.GARNT_HEARTBEAT_RUNNING = true

local CONFIG = {
    id = "com.roblox.client", -- package name = instance id
    port = 8765,
    interval = 15,            -- seconds between pings
    alive_window = 5,         -- Heartbeat must have advanced within this many seconds
}

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local CoreGui = game:GetService("CoreGui")

local function now()
    local ok, value = pcall(function()
        return tick()
    end)
    if ok then
        return value
    end
    return os.time()
end

local function wait_step(seconds)
    if task and task.wait then
        task.wait(seconds)
    else
        wait(seconds)
    end
end

-- track engine liveness
local last_heartbeat = now()
RunService.Heartbeat:Connect(function()
    last_heartbeat = now()
end)

local function url_encode(value)
    local text = tostring(value or "")
    text = text:gsub("\n", " ")
    return (text:gsub("[^%w%-%._~]", function(char)
        return string.format("%%%02X", string.byte(char))
    end))
end

-- any transport available in the executor, in order of preference
local function http_get(url)
    local syn = rawget(_G, "syn") or (getgenv and getgenv().syn)
    if syn and type(syn.request) == "function" then
        local ok, res = pcall(syn.request, { Url = url, Method = "GET" })
        if ok and type(res) == "table" and (res.StatusCode or 200) < 400 then
            return true
        end
    end

    local request = rawget(_G, "request")
    if type(request) == "function" then
        local ok, res = pcall(request, { Url = url, Method = "GET" })
        if ok and type(res) == "table" and (res.StatusCode or 200) < 400 then
            return true
        end
    end

    local http_request = rawget(_G, "http_request")
    if type(http_request) == "function" then
        local ok, res = pcall(http_request, { Url = url })
        if ok and type(res) == "table" and (res.StatusCode or 200) < 400 then
            return true
        end
    end

    local ok = pcall(function()
        game:HttpGet(url)
    end)
    return ok
end

-- a disconnect / error / rejoin prompt means the session is already dead
local function has_error_prompt()
    local ok, found = pcall(function()
        for _, node in ipairs(CoreGui:GetDescendants()) do
            if node:IsA("TextLabel") and node.Visible then
                local text = string.lower(tostring(node.Text or ""))
                if text ~= "" and (
                    string.find(text, "disconnected", 1, true)
                    or string.find(text, "lost connection", 1, true)
                    or string.find(text, "rejoin", 1, true)
                    or string.find(text, "connection lost", 1, true)
                ) then
                    return true
                end
            end
        end
        return false
    end)
    if ok then
        return found
    end
    return false
end

local function build_url()
    local player = Players.LocalPlayer
    local user_name = player and player.Name or "unknown"
    local user_id = player and player.UserId or 0
    local place_id = 0
    local job_id = ""

    pcall(function()
        place_id = game.PlaceId
    end)
    pcall(function()
        job_id = tostring(game.JobId or "")
    end)

    return string.format(
        "http://127.0.0.1:%d/hb?id=%s&user=%s&uid=%d&place=%d&job=%s",
        CONFIG.port,
        url_encode(CONFIG.id),
        url_encode(user_name),
        tonumber(user_id) or 0,
        tonumber(place_id) or 0,
        url_encode(job_id)
    )
end

local function main_loop()
    while true do
        local player = Players.LocalPlayer
        local loaded = false
        pcall(function()
            loaded = game:IsLoaded()
        end)

        local engine_alive = (now() - last_heartbeat) <= CONFIG.alive_window
        local pinging = player ~= nil and loaded and engine_alive and not has_error_prompt()

        if pinging then
            http_get(build_url())
        end

        wait_step(CONFIG.interval)
    end
end

if task and task.spawn then
    task.spawn(main_loop)
else
    spawn(main_loop)
end

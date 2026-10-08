-- unibridge-node-path-prefix: a base path per upstream node.
--
-- One UniBridge upstream can hold nodes that serve the same API under
-- different base paths, e.g. 10.0.0.1 serves /v1/models and 10.0.0.2 serves
-- /api/v1/models. A node's base path is `metadata.path_prefix` in the
-- upstream's array-form `nodes`. Once the balancer has picked a node, this
-- plugin puts that node's prefix in front of the path the route forwards
-- (after any strip-prefix rewrite). It runs from a global rule that
-- unibridge-service provisions (app/services/node_path_prefix.py).
--
-- nginx builds the upstream request line once, before the first attempt, so a
-- retry cannot change the path. A retry onto a node whose prefix differs from
-- the one the request was built with therefore ends the request with 502
-- instead of reaching that node with another node's path. Nodes that share a
-- prefix still fail over to each other. A refused retry also uses up the next
-- node's balancer turn, so unibridge-service saves upstreams whose nodes mix
-- prefixes with TCP health checks that take a dead node out, and with retries
-- off when no two nodes share a prefix.
--
-- Nodes are told apart by their resolved address, as APISIX's balancer does:
-- two host names that resolve to the same IP and port are one node to APISIX,
-- which then takes the Host of one and could take the prefix of the other, so
-- an address whose nodes ask for different prefixes gets 502.
--
-- This leans on APISIX internals, so re-check it when upgrading APISIX:
-- `before_proxy` runs in the access phase right after the first node is picked
-- and again in the balancer phase before every retry; `ctx.balancer_ip` and
-- `ctx.balancer_port` name the picked node; `ctx.upstream_conf.nodes` holds the
-- nodes with domains resolved, ports filled in and `metadata` kept.

local core = require("apisix.core")
local ngx = ngx
local ipairs = ipairs
local setmetatable = setmetatable
local tostring = tostring
local type = type
local str_byte = string.byte
local str_find = string.find
local str_gmatch = string.gmatch
local str_gsub = string.gsub
local str_sub = string.sub

local plugin_name = "unibridge-node-path-prefix"

local schema = {
    type = "object",
    properties = {},
}

local _M = {
    version = 0.1,
    priority = 1004,
    name = plugin_name,
    schema = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

local SLASH = str_byte("/")
local MAX_PREFIX_LEN = 256

-- The rules unibridge-service validates on save: "/seg[/seg...]" made of RFC
-- 3986 path characters, with no empty, "." or ".." segment. Checked again here
-- so a value written straight to the Admin API can never put whitespace or a
-- line break into the request line.
local function valid_prefix(prefix)
    if type(prefix) ~= "string" or #prefix == 0 or #prefix > MAX_PREFIX_LEN then
        return false
    end
    if str_byte(prefix, 1) ~= SLASH or str_byte(prefix, -1) == SLASH then
        return false
    end
    if str_find(prefix, "[^%w%-%._~!%$&'%(%)%*%+,;=:@%%/]")
            or str_find(prefix, "//", 1, true) then
        return false
    end
    -- every "%" must start a "%XX" escape
    if str_find((str_gsub(prefix, "%%%x%x", "")), "%", 1, true) then
        return false
    end
    for segment in str_gmatch(prefix, "[^/]+") do
        -- "%2e" too: a backend that decodes it would see "." or ".."
        local decoded = str_gsub(segment, "%%2[eE]", ".")
        if decoded == "." or decoded == ".." then
            return false
        end
    end
    return true
end

local function bare_host(host)
    if str_byte(host, 1) == str_byte("[") and str_byte(host, -1) == str_byte("]") then
        return str_sub(host, 2, -2)
    end
    return host
end

local function node_key(host, port)
    return bare_host(host) .. ":" .. tostring(port)
end

-- address -> the prefix its nodes ask for ("" for none), for every node of an
-- upstream; false for the whole upstream when no node has a prefix. An address
-- maps to false when it must get no traffic: its prefix fails validation (its
-- requests would otherwise go out without the base path meant for it), or
-- nodes at that one address ask for different prefixes.
-- Keyed weakly by the nodes table: APISIX swaps in a new one when the upstream
-- changes and when its domains re-resolve (it keeps the conf table itself).
local prefix_indexes = setmetatable({}, {__mode = "k"})

local function prefix_index(up_conf)
    local nodes = up_conf.nodes
    if type(nodes) ~= "table" then
        return false
    end
    local index = prefix_indexes[nodes]
    if index ~= nil then
        return index
    end

    -- Every node, prefixed or not: two host names that resolve to one address
    -- are one node to APISIX (Host from one, path from the other), so
    -- addresses whose nodes ask for different prefixes are refused.
    local by_address = {}
    local any_prefix = false
    local default_port = up_conf.scheme == "https" and 443 or 80
    for _, node in ipairs(nodes) do
        if type(node.host) == "string" then
            local metadata = node.metadata
            local prefix = type(metadata) == "table" and metadata.path_prefix or nil
            local key = node_key(node.host, node.port or default_port)
            local value = ""
            if prefix ~= nil then
                any_prefix = true
                if valid_prefix(prefix) then
                    value = prefix
                else
                    value = false
                    core.log.error(plugin_name, ": invalid path_prefix on node ", key,
                                   "; requests picked for it get 502")
                end
            end
            local seen = by_address[key]
            if seen == nil then
                by_address[key] = value
            elseif seen ~= value then
                by_address[key] = false
                core.log.error(plugin_name, ": nodes at ", key, " (",
                               tostring(node.domain or node.host),
                               ") ask for different path prefixes; requests picked for it get 502")
            end
        end
    end
    index = any_prefix and by_address or false

    prefix_indexes[nodes] = index
    return index
end

function _M.before_proxy(conf, ctx)
    local up_conf = ctx.upstream_conf
    if not up_conf then
        return
    end

    local scheme = ctx.upstream_scheme
    if scheme ~= "http" and scheme ~= "https" then
        return
    end

    local retrying = ngx.get_phase() == "balancer"
    local index
    if retrying then
        -- the nodes this request's picker was built from, not whatever
        -- another request resolved since
        index = ctx.unibridge_node_path_index
        if index == nil then
            index = prefix_index(up_conf)
        end
    else
        index = prefix_index(up_conf)
        ctx.unibridge_node_path_index = index
    end
    if not index then
        return
    end

    local ip, port = ctx.balancer_ip, ctx.balancer_port
    if not ip or not port then
        -- Without the picked node any path could be the wrong one.
        core.log.error(plugin_name, ": APISIX did not report the picked node; ",
                       "refusing the request")
        return 502
    end
    local key = node_key(ip, port)
    local prefix = index[key]
    if prefix == nil then
        -- Every node is indexed, so APISIX picked an address the upstream does
        -- not list; guessing a path could send the wrong one.
        core.log.error(plugin_name, ": picked node ", key, " is not among the ",
                       "upstream's nodes; refusing the request")
        return 502
    end
    if prefix == false then
        return 502
    end

    if retrying then
        -- A retry. The request line was built for the first node and cannot
        -- change, so it is only valid for a node with the same prefix.
        local applied = ctx.unibridge_node_path_prefix or ""
        if prefix ~= applied then
            core.log.warn(plugin_name, ": not retrying on ", ip, ":", tostring(port),
                          " because its path prefix '", prefix, "' differs from '",
                          applied, "', which the request was built with")
            return 502
        end
        return
    end

    if ctx.unibridge_node_path_prefix ~= nil then
        -- Already applied to this request.
        return
    end
    ctx.unibridge_node_path_prefix = prefix
    if prefix == "" then
        return
    end

    local uri = ctx.var.upstream_uri
    if uri == nil or uri == "" then
        -- No plugin rewrote the path, so nginx would send the client's request
        -- target as it came in.
        uri = ctx.var.real_request_uri or ctx.var.request_uri or "/"
    end
    if str_byte(uri, 1) ~= SLASH then
        uri = "/" .. uri
    end
    ctx.var.upstream_uri = prefix .. uri
end

return _M

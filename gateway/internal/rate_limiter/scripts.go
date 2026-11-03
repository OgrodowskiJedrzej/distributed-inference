package ratelimiter

import "github.com/redis/go-redis/v9"

var reserveScript = redis.NewScript(`
local now = redis.call("TIME")
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local request_limit = tonumber(ARGV[1])
local token_limit = tonumber(ARGV[2])
local concurrency_limit = tonumber(ARGV[3])
local requested_tokens = tonumber(ARGV[4])
local reservation_ttl = tonumber(ARGV[5])
local window = tonumber(ARGV[6])

if redis.call("EXISTS", KEYS[4]) == 1 then
    return {1, "existing", 0}
end

redis.call("ZREMRANGEBYSCORE", KEYS[3], "-inf", now_ms)

local function bucket(key, capacity)
    local values = redis.call("HMGET", key, "tokens", "updated_at")
    local available = tonumber(values[1]) or capacity
    local updated_at = tonumber(values[2]) or now_ms
    available = math.min(capacity, available + (now_ms - updated_at) * capacity / window)
    return available
end

local request_tokens = bucket(KEYS[1], request_limit)
local token_tokens = bucket(KEYS[2], token_limit)

if request_tokens < 1 then
    local retry = math.ceil((1 - request_tokens) * window / request_limit)
    return {0, "requests", retry}
end

if token_tokens < requested_tokens then
    local retry = math.ceil((requested_tokens - token_tokens) * window / token_limit)
    return {0, "tokens", retry}
end

if redis.call("ZCARD", KEYS[3]) >= concurrency_limit then
    local earliest = redis.call("ZRANGE", KEYS[3], 0, 0, "WITHSCORES")
    local retry = math.max(1, tonumber(earliest[2]) - now_ms)
    return {0, "concurrency", retry}
end

local bucket_ttl = math.max(window * 2, reservation_ttl)
redis.call("HSET", KEYS[1], "tokens", request_tokens - 1, "updated_at", now_ms)
redis.call("PEXPIRE", KEYS[1], bucket_ttl)
redis.call("HSET", KEYS[2], "tokens", token_tokens - requested_tokens, "updated_at", now_ms)
redis.call("PEXPIRE", KEYS[2], bucket_ttl)

local expires_at = now_ms + reservation_ttl
redis.call("ZADD", KEYS[3], expires_at, KEYS[4])
redis.call("PEXPIRE", KEYS[3], bucket_ttl)
redis.call("HSET", KEYS[4], "reserved_tokens", requested_tokens)
redis.call("PEXPIRE", KEYS[4], reservation_ttl)

return {1, "allowed", 0}
`)

var finishScript = redis.NewScript(`
if redis.call("EXISTS", KEYS[4]) == 0 then
    redis.call("ZREM", KEYS[3], KEYS[4])
    return 0
end

local now = redis.call("TIME")
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local request_limit = tonumber(ARGV[1])
local token_limit = tonumber(ARGV[2])
local actual_tokens = tonumber(ARGV[3])
local cancel = tonumber(ARGV[4])
local window = tonumber(ARGV[5])
local reserved_tokens = tonumber(redis.call("HGET", KEYS[4], "reserved_tokens"))

local function adjust_bucket(key, capacity, adjustment)
    local values = redis.call("HMGET", key, "tokens", "updated_at")
    local available = tonumber(values[1]) or capacity
    local updated_at = tonumber(values[2]) or now_ms
    available = math.min(capacity, available + (now_ms - updated_at) * capacity / window)
    available = math.min(capacity, available + adjustment)
    redis.call("HSET", key, "tokens", available, "updated_at", now_ms)
    redis.call("PEXPIRE", key, window * 2)
end

adjust_bucket(KEYS[2], token_limit, reserved_tokens - actual_tokens)
if cancel == 1 then
    adjust_bucket(KEYS[1], request_limit, 1)
end

redis.call("ZREM", KEYS[3], KEYS[4])
redis.call("DEL", KEYS[4])
return 1
`)

var extendScript = redis.NewScript(`
if redis.call("EXISTS", KEYS[2]) == 0 then
    redis.call("ZREM", KEYS[1], KEYS[2])
    return 0
end

local now = redis.call("TIME")
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local reservation_ttl = tonumber(ARGV[1])
redis.call("ZADD", KEYS[1], now_ms + reservation_ttl, KEYS[2])
redis.call("PEXPIRE", KEYS[1], reservation_ttl)
redis.call("PEXPIRE", KEYS[2], reservation_ttl)
return 1
`)

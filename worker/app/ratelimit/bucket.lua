-- Atomic token bucket shared by every worker.
-- KEYS[1]  bucket key (e.g. rl:llm:rpm)
-- ARGV[1]  capacity (max tokens held)
-- ARGV[2]  refill rate, tokens per second
-- ARGV[3]  tokens requested
-- Returns {allowed (1|0), wait_ms}. Uses the Redis server clock so workers on
-- different hosts with skewed clocks still agree.
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
if requested > capacity then requested = capacity end

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity end
if ts == nil then ts = now end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate / 1000)

local allowed = 0
local wait_ms = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
else
  wait_ms = math.ceil((requested - tokens) * 1000 / rate)
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', key, math.ceil(capacity * 1000 / rate) + 60000)
return {allowed, wait_ms}

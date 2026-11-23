-- Fair, crash-safe counting semaphore shared by every worker.
-- KEYS[1] holders zset  (member -> lease expiry, ms)
-- KEYS[2] queue zset    (member -> ticket number, FIFO order)
-- KEYS[3] seen zset     (member -> last poll, ms; drops waiters that died)
-- KEYS[4] ticket counter
-- ARGV[1] op: "acquire" | "refresh" | "release"
-- ARGV[2] member id
-- ARGV[3] limit (acquire)
-- ARGV[4] lease ttl ms (acquire / refresh)
-- ARGV[5] waiter ttl ms (acquire)
-- Returns {granted (1|0), position in queue (0 = granted), holders}.
local holders, queue, seen, counter = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local op, member = ARGV[1], ARGV[2]

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

if op == 'release' then
  redis.call('ZREM', holders, member)
  redis.call('ZREM', queue, member)
  redis.call('ZREM', seen, member)
  return {0, 0, redis.call('ZCARD', holders)}
end

local ttl = tonumber(ARGV[4])
if op == 'refresh' then
  if redis.call('ZSCORE', holders, member) then
    redis.call('ZADD', holders, now + ttl, member)
    return {1, 0, redis.call('ZCARD', holders)}
  end
  return {0, 0, redis.call('ZCARD', holders)}
end

-- acquire
local limit = tonumber(ARGV[3])
local waiter_ttl = tonumber(ARGV[5])
-- Expire leases of crashed holders and waiters that stopped polling.
redis.call('ZREMRANGEBYSCORE', holders, '-inf', now)
local stale = redis.call('ZRANGEBYSCORE', seen, '-inf', now - waiter_ttl)
for _, m in ipairs(stale) do
  redis.call('ZREM', queue, m)
  redis.call('ZREM', seen, m)
end

if redis.call('ZSCORE', holders, member) then
  redis.call('ZADD', holders, now + ttl, member)
  return {1, 0, redis.call('ZCARD', holders)}
end
if not redis.call('ZSCORE', queue, member) then
  redis.call('ZADD', queue, redis.call('INCR', counter), member)
end
redis.call('ZADD', seen, now, member)

local held = redis.call('ZCARD', holders)
local rank = redis.call('ZRANK', queue, member)
if held < limit and rank < (limit - held) then
  redis.call('ZADD', holders, now + ttl, member)
  redis.call('ZREM', queue, member)
  redis.call('ZREM', seen, member)
  return {1, 0, held + 1}
end
for _, k in ipairs({holders, queue, seen, counter}) do
  redis.call('PEXPIRE', k, 3600000)
end
return {0, rank + 1, held}

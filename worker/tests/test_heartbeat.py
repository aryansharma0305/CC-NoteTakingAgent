import asyncio
import json

from app.heartbeat import KEY_PREFIX, TTL_SEC, Heartbeat


class FakeRedis:
    def __init__(self):
        self.data, self.ttl = {}, {}

    async def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    async def delete(self, key):
        self.data.pop(key, None)


def test_heartbeat_tracks_busy_idle_and_errors():
    r = FakeRedis()
    hb = Heartbeat(r, "w1", {"llm": "openai:qwen2.5:3b"})

    async def go():
        await hb.busy("job-1", "SUMMARIZE")
        busy = json.loads(r.data[KEY_PREFIX + "w1"])
        assert busy["state"] == "busy" and busy["job_id"] == "job-1" and busy["stage"] == "SUMMARIZE"
        assert busy["llm"] == "openai:qwen2.5:3b" and r.ttl[KEY_PREFIX + "w1"] == TTL_SEC
        await hb.idle(error="DeliveryError: nack")
        idle = json.loads(r.data[KEY_PREFIX + "w1"])
        assert idle["state"] == "idle" and idle["job_id"] is None and idle["last_job_id"] == "job-1"
        assert idle["handled"] == 1 and idle["failed"] == 1
        assert idle["last_error"]["stage"] == "SUMMARIZE" and "nack" in idle["last_error"]["message"]
        await hb.stop()
        assert KEY_PREFIX + "w1" not in r.data

    asyncio.run(go())

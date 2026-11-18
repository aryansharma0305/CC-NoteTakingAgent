"""S3 helpers for frame JPEGs (minio client; works with SeaweedFS or MinIO).

Keys are deterministic: jobs/{job_id}/frame_{t}.jpg."""

from __future__ import annotations

import asyncio
import io
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error


def frame_key(job_id: str, t: int) -> str:
    return f"jobs/{job_id}/frame_{int(t)}.jpg"


class Storage:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str):
        u = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        self._client = Minio(u.netloc, access_key=access_key, secret_key=secret_key, secure=u.scheme == "https")
        self.bucket = bucket

    async def ensure_bucket(self) -> None:
        def _do():
            if not self._client.bucket_exists(self.bucket):
                try:
                    self._client.make_bucket(self.bucket)
                except S3Error as e:  # another worker created it first
                    if e.code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        raise

        await asyncio.to_thread(_do)

    async def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> None:
        await asyncio.to_thread(
            self._client.put_object, self.bucket, key, io.BytesIO(data), len(data), content_type=content_type
        )

    async def get(self, key: str) -> bytes:
        def _do() -> bytes:
            resp = self._client.get_object(self.bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()

        try:
            return await asyncio.to_thread(_do)
        except S3Error as e:
            if e.code == "NoSuchKey":
                raise FileNotFoundError(key) from e
            raise

    async def exists(self, key: str) -> bool:
        def _do() -> bool:
            try:
                self._client.stat_object(self.bucket, key)
                return True
            except S3Error as e:
                if e.code in ("NoSuchKey", "NoSuchObject"):
                    return False
                raise

        return await asyncio.to_thread(_do)

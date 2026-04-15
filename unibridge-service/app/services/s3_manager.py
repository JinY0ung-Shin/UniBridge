from __future__ import annotations

import asyncio
import logging
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.models import S3Connection
from app.services.connection_manager import decrypt_password, validate_encryption_key

logger = logging.getLogger(__name__)


class S3ConnectionManager:
    """Singleton that manages boto3 S3 clients per alias."""

    _instance: S3ConnectionManager | None = None
    _clients: dict[str, BaseClient]
    _configs: dict[str, dict[str, Any]]

    def __new__(cls) -> S3ConnectionManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._clients = {}
            cls._instance._configs = {}
        return cls._instance

    async def initialize(self, connections: list[S3Connection]) -> None:
        validate_encryption_key()
        for conn in connections:
            try:
                await self.add_connection(conn)
            except Exception:
                logger.exception("Failed to initialize S3 connection '%s'", conn.alias)

    async def add_connection(self, conn: S3Connection) -> None:
        if conn.alias in self._clients:
            await self.remove_connection(conn.alias)

        access_key = decrypt_password(conn.access_key_id_encrypted)
        secret_key = decrypt_password(conn.secret_access_key_encrypted)

        kwargs: dict[str, Any] = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": conn.region,
            "config": Config(
                signature_version="s3v4",
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        }
        if conn.endpoint_url:
            kwargs["endpoint_url"] = conn.endpoint_url
        kwargs["use_ssl"] = conn.use_ssl

        client = await asyncio.to_thread(
            boto3.client, "s3", **kwargs
        )
        self._clients[conn.alias] = client
        self._configs[conn.alias] = {
            "endpoint_url": conn.endpoint_url,
            "region": conn.region,
            "default_bucket": conn.default_bucket,
        }
        logger.info("S3 connection created for alias '%s'", conn.alias)

    async def remove_connection(self, alias: str) -> None:
        client = self._clients.pop(alias, None)
        self._configs.pop(alias, None)
        if client is not None:
            await asyncio.to_thread(client.close)
            logger.info("S3 client closed for alias '%s'", alias)

    def get_client(self, alias: str) -> BaseClient:
        try:
            return self._clients[alias]
        except KeyError:
            raise KeyError(f"No S3 client registered for alias '{alias}'")

    def get_config(self, alias: str) -> dict[str, Any]:
        return self._configs.get(alias, {})

    def has_connection(self, alias: str) -> bool:
        return alias in self._clients

    def list_aliases(self) -> list[str]:
        return list(self._clients.keys())

    async def test_connection(self, alias: str) -> tuple[bool, str]:
        try:
            client = self.get_client(alias)
            config = self.get_config(alias)
            default_bucket = config.get("default_bucket")
            if default_bucket:
                await asyncio.to_thread(client.head_bucket, Bucket=default_bucket)
            else:
                await asyncio.to_thread(client.list_buckets)
            return True, "Connection successful"
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            logger.warning("S3 connection test failed for '%s': %s %s", alias, code, exc)
            return False, f"Connection failed ({code})" if code else "Connection failed"
        except (BotoCoreError, Exception) as exc:
            logger.exception("S3 connection test failed for '%s'", alias)
            return False, "Connection failed"

    async def list_buckets(self, alias: str) -> list[dict[str, Any]]:
        client = self.get_client(alias)
        resp = await asyncio.to_thread(client.list_buckets)
        return [
            {
                "name": b["Name"],
                "creation_date": b["CreationDate"].isoformat() if b.get("CreationDate") else None,
            }
            for b in resp.get("Buckets", [])
        ]

    async def list_objects(
        self,
        alias: str,
        bucket: str,
        prefix: str = "",
        delimiter: str = "/",
        max_keys: int = 200,
        continuation_token: str | None = None,
    ) -> dict[str, Any]:
        client = self.get_client(alias)
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": prefix,
            "Delimiter": delimiter,
            "MaxKeys": max_keys,
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        resp = await asyncio.to_thread(client.list_objects_v2, **kwargs)

        folders = [
            {"prefix": cp["Prefix"]}
            for cp in resp.get("CommonPrefixes", [])
        ]
        objects = [
            {
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat() if obj.get("LastModified") else None,
                "storage_class": obj.get("StorageClass"),
            }
            for obj in resp.get("Contents", [])
        ]

        return {
            "folders": folders,
            "objects": objects,
            "is_truncated": resp.get("IsTruncated", False),
            "next_continuation_token": resp.get("NextContinuationToken"),
            "key_count": resp.get("KeyCount", 0),
        }

    async def get_object_metadata(
        self, alias: str, bucket: str, key: str
    ) -> dict[str, Any]:
        client = self.get_client(alias)
        resp = await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
        return {
            "key": key,
            "size": resp.get("ContentLength"),
            "content_type": resp.get("ContentType"),
            "last_modified": resp["LastModified"].isoformat() if resp.get("LastModified") else None,
            "etag": resp.get("ETag"),
            "storage_class": resp.get("StorageClass"),
            "metadata": resp.get("Metadata", {}),
        }

    async def generate_presigned_url(
        self, alias: str, bucket: str, key: str, expires_in: int = 3600
    ) -> str:
        client = self.get_client(alias)
        url = await asyncio.to_thread(
            client.generate_presigned_url,
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return url

    async def dispose_all(self) -> None:
        for alias in list(self._clients.keys()):
            await self.remove_connection(alias)


s3_manager = S3ConnectionManager()

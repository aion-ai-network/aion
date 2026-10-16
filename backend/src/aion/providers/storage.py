import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from aion.settings import Settings

KEY = re.compile(r"^[0-9a-f-]{36}/[0-9a-f-]{36}(?:\.[a-z0-9]{1,8})?$")


class BlobStore:
    """Only server-generated owner/UUID keys are accepted; no public bucket URLs."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def path(self, key: str) -> Path:
        if not KEY.fullmatch(key):
            raise ValueError("Invalid object key")
        root = Path(self.settings.storage_path).resolve()
        path = (root / key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Object outside storage root")
        return path

    def client(self):  # type: ignore[no-untyped-def]
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint,
            region_name=self.settings.s3_region,
            aws_access_key_id=self.settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=self.settings.s3_secret_key.get_secret_value(),
        )

    def put(self, key: str, source: Any) -> None:
        path = self.path(key)
        if self.settings.storage_backend == "s3":
            self.client().upload_fileobj(source, self.settings.s3_bucket, key)
            return
        import shutil

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.upload")
        try:
            with temporary.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, key: str) -> Iterator[bytes]:
        path = self.path(key)
        if self.settings.storage_backend == "s3":
            body = self.client().get_object(Bucket=self.settings.s3_bucket, Key=key)["Body"]
            try:
                yield from body.iter_chunks(chunk_size=65536)
            finally:
                body.close()
        else:
            with path.open("rb") as source:
                while chunk := source.read(65536):
                    yield chunk

    def delete(self, key: str) -> None:
        path = self.path(key)
        if self.settings.storage_backend == "s3":
            self.client().delete_object(Bucket=self.settings.s3_bucket, Key=key)
        else:
            path.unlink(missing_ok=True)

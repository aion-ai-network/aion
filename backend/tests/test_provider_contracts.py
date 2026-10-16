import io

import httpx
from pydantic import SecretStr

from aion.providers.llm import complete
from aion.providers.storage import BlobStore
from aion.settings import Settings


async def test_openai_compatible_http_contract(monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def provider(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"reply":"Hello","operations":[]}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(**kw, transport=httpx.MockTransport(provider)),
    )
    result = await complete(
        Settings(
            _env_file=None,
            llm_enabled=True,
            llm_model="test-model",
            llm_api_key=SecretStr("synthetic"),
        ),
        [{"role": "user", "content": "Hello"}],
    )
    assert result.document["reply"] == "Hello" and result.input_tokens == 20
    assert seen[0].url.path == "/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer synthetic"


def test_s3_storage_contract_uses_private_keys(tmp_path, monkeypatch):
    import boto3

    objects = {}

    class Client:
        def upload_fileobj(self, source, bucket, key):
            objects[(bucket, key)] = source.read()

        def get_object(self, *, Bucket, Key):
            class Body(io.BytesIO):
                def iter_chunks(self, chunk_size):
                    while chunk := self.read(chunk_size):
                        yield chunk

            return {"Body": Body(objects[(Bucket, Key)])}

        def delete_object(self, *, Bucket, Key):
            objects.pop((Bucket, Key), None)

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: Client())
    store = BlobStore(Settings(_env_file=None, storage_backend="s3", storage_path=str(tmp_path)))
    key = "11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222"
    store.put(key, io.BytesIO(b"private"))
    assert b"".join(store.read(key)) == b"private"
    store.delete(key)
    assert objects == {}

from pydantic import SecretStr

from aion.settings import Settings


def test_database_password_is_encoded_as_a_url_value_and_hidden_in_logs():
    settings = Settings(_env_file=None, db_password=SecretStr("password@with:/symbols"))
    assert settings.database_url.password == "password@with:/symbols"
    assert "password@with:/symbols" not in str(settings.database_url)
    assert "password@with:/symbols" not in settings.model_dump_json()


def test_runtime_and_migration_credentials_are_distinct():
    settings = Settings(
        _env_file=None,
        db_password=SecretStr("runtime"),
        db_migration_password=SecretStr("migration"),
    )
    assert settings.database_url.username != settings.migration_database_url.username
    assert settings.database_url.password != settings.migration_database_url.password

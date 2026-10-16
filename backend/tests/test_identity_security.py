import pytest
from fastapi import Response
from pydantic import ValidationError

from aion.modules.identity.router import set_session
from aion.modules.identity.schemas import Register
from aion.settings import Settings


def test_production_cookie_requires_https_and_has_security_attributes():
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, environment="production")
    settings = Settings(_env_file=None, environment="production", public_origin="https://aion.test")
    response = Response()
    set_session(response, "test-session", settings)
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "domain=" not in cookie


def test_password_whitespace_is_preserved_and_unexpected_fields_are_rejected():
    password = "  a long passphrase  "
    data = {"token": "a" * 43, "password": password, "display_name": "Name"}
    assert Register.model_validate(data).password.get_secret_value() == password
    with pytest.raises(ValidationError):
        Register.model_validate({**data, "role": "admin"})

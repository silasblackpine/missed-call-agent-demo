import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    for k, v in {
        "TWILIO_AUTH_TOKEN": "test_token",
        "TWILIO_ACCOUNT_SID": "ACtest",
        "TWILIO_PHONE_NUMBER": "+18005550100",
        "CALENDLY_URL": "https://calendly.com/demo/intro",
        "PUBLIC_BASE_URL": "https://demo.example.com",
        "FORWARD_TO_NUMBER": "",
        "ENABLE_HUBSPOT": "false",
        "ENABLE_RESEND": "false",
        "SKIP_TWILIO_VALIDATION": "false",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_optout():
    from app import optout
    optout.reset()
    yield
    optout.reset()

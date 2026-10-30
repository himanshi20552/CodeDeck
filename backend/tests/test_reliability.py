from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.schemas import QuestionRequest
from app.main import health_live, health_ready


def test_health_live_is_independent_of_optional_integrations():
    assert health_live() == {"status": "ok", "service": "codedeck"}


def test_readiness_returns_safe_503_when_database_is_unavailable(monkeypatch):
    class FailedSession:
        def execute(self, _query):
            raise RuntimeError("private connection string should not be returned")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("app.db.database.SessionLocal", FailedSession)
    response = health_ready()
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert b"private connection string" not in response.body


def test_question_input_has_sensible_limits():
    assert QuestionRequest(question="How is routing configured?").question
    try:
        QuestionRequest(question=" ")
    except ValidationError:
        pass
    else:
        raise AssertionError("blank question should be rejected")

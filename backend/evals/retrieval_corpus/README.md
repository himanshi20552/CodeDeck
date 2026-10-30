# Sample service

This small fixture represents the CodeDeck retrieval evaluation corpus.

The backend is a FastAPI application. The ASGI application is exported as `app` from `app.main` and the service listens on port 8000 in the development container. PostgreSQL is configured with `DATABASE_URL`.

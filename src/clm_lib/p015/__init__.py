"""Programme 015 harness path: hosts the real guarded-clm Pydantic AI capability (opt-in, separate environment).

Nothing outside this package imports it; it needs the `p015` dependency group (pydantic-ai-slim and guarded-clm),
installed with `UV_PROJECT_ENVIRONMENT=.venv-p015 uv sync --no-dev --group p015`. See docs/architecture.md,
"Programme 015 guarded-clm integration", and SPEC.md section 28.
"""

SESSION_FORMAT = "guarded-session/1"
EVIDENCE_LABEL = (
    "scripted-offline"  # every record produced in this chunk; live runs are not authorised
)

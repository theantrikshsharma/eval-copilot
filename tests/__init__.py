"""
Test isolation. Python imports this package before any test module, so it runs first.

Tests must never read the developer's .env or reach a real Supabase project, model provider or hosted mode:
MANTIS_TEST_MODE makes db.py, judges.py and the migration script skip .env, and credentials already in the
shell environment are removed here. Tests that need a key or a scratch database set one explicitly.
"""

import os

os.environ["MANTIS_TEST_MODE"] = "1"
for _name in (
    "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY",
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "MANTIS_SEMANTIC_SAFETY", "MANTIS_ADMIN_TOKEN",
    "MANTIS_HOSTED", "VERCEL", "VERCEL_ENV", "LAMBDA_TASK_ROOT", "AWS_LAMBDA_FUNCTION_NAME", "MANTIS_SQLITE_PATH",
):
    os.environ.pop(_name, None)

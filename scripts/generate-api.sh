#!/bin/bash
# Generate OpenAPI TypeScript client from FastAPI schema.
#
# Assumes the repo root is the working directory. The CI drift gate
# (Jenkinsfile Verify: api-drift) runs this then diffs app/src/lib/api/
# against HEAD to fail builds where the committed client has drifted
# from the live backend's OpenAPI schema.

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "Generating OpenAPI client..."

# Check if backend is running
if ! curl -s http://localhost:17493/openapi.json > /dev/null 2>&1; then
    echo "Backend not running. Starting backend..."

    # Check if virtual environment exists at backend/venv
    if [ ! -d "backend/venv" ]; then
        echo "Creating virtual environment..."
        python -m venv backend/venv
    fi

    # Install dependencies if needed
    if ! backend/venv/bin/python -c "import fastapi" 2>/dev/null; then
        echo "Installing backend dependencies..."
        backend/venv/bin/pip install -r backend/requirements.txt
    fi

    # Start backend in background. backend/main.py uses package-relative
    # imports (`from .app import app`) so it must be addressed as
    # `backend.main:app` from the repo root, not `main:app` from inside
    # backend/.
    echo "Starting backend server..."
    backend/venv/bin/uvicorn backend.main:app --port 17493 &
    BACKEND_PID=$!

    # Wait for server to be ready
    echo "Waiting for server to start..."
    for _ in {1..30}; do
        if curl -s http://localhost:17493/openapi.json > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done

    if ! curl -s http://localhost:17493/openapi.json > /dev/null 2>&1; then
        echo "Error: Backend failed to start"
        kill $BACKEND_PID 2>/dev/null || true
        exit 1
    fi

    echo "Backend started (PID: $BACKEND_PID)"
    STARTED_BACKEND=true
else
    STARTED_BACKEND=false
fi

# Download OpenAPI schema
echo "Downloading OpenAPI schema..."
curl -s http://localhost:17493/openapi.json > app/openapi.json

# openapi-typescript-codegen is now a devDependency in the root
# package.json (see commit T-TS-02); `bun install` brings it in. No
# in-script `bun add` — that would mutate the lockfile under CI.
echo "Generating TypeScript client..."
cd app
bun x openapi-typescript-codegen \
    --input openapi.json \
    --output src/lib/api \
    --client fetch \
    --useOptions \
    --exportSchemas true

echo "API client generated in app/src/lib/api"

# Clean up
if [ "$STARTED_BACKEND" = true ]; then
    echo "Stopping backend server..."
    kill $BACKEND_PID 2>/dev/null || true
fi

echo "Done!"

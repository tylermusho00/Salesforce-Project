# Deploy to Render

This Dash/Flask dashboard runs as a Python Web Service. It does not produce a static
publish directory. Deploy the repository source, including app/, lead_router/,
config/, data/, tests/, and the dependency files.

Use the root render.yaml with Render's Blueprint workflow, or create a Web Service
with these settings:

| Setting | Value |
| --- | --- |
| Runtime | Python 3 |
| Root Directory | Leave blank (repository root) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app.main:server --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 360` |
| Publish Directory | None; this is a Web Service |
| Health Check Path | `/` |
| Environment | `PYTHON_VERSION=3.11.11`, `PYTHONUNBUFFERED=1` |

Commit and push the desired local changes before deploying a connected Git branch.
No local Windows path belongs in Render's Root Directory setting.

Gunicorn is included in requirements.txt. The older requirements-render.txt entry
point also works by including that file. If startup reports `gunicorn: command not
found`, verify the deployed branch contains the updated requirements.txt and rebuild.

The application stores routing results in process memory. Keep one worker and one
service instance; visitors share the current routing run, and restarts clear it.
This is a shared demo, not isolated per-user storage. The free plan is configured
for an initial deployment, but its memory and CPU limits may be insufficient for
the 50k dataset plus dashboard tests; size the service using deployed measurements.

Gunicorn runs on Linux. A Windows Flask test-client check validates the application
entry point but cannot prove Gunicorn startup or Render resource limits.

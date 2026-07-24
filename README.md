SentryOps AI 🚀
An AI‑powered incident copilot with observability built in

📌 Problem Statement
Modern cloud platforms face a recurring challenge:

Services go down, pods crash, latency spikes, and engineers scramble to debug.

Logs are scattered, metrics are siloed, and tracing is often missing.

Incident response becomes reactive firefighting instead of structured diagnosis.

For a fresher engineer, the hardest part is not just fixing the bug — it’s seeing the system clearly when everything is noisy.

💡 Solution
SentryOps AI is a demo incident copilot that shows how to combine:

FastAPI → lightweight service to accept questions (/ask) and expose health/metrics endpoints.

Observability stack → JSON logs, OpenTelemetry traces, Prometheus metrics.

AI engine (mocked) → simulates reasoning with retries, fallbacks, and runbook lookups.

Business action → automatically creates incident tickets when outage‑like questions are detected.

Instead of building a huge production system, this project is a teaching scaffold: it demonstrates how observability, resilience, and automation fit together in a cloud service.

🛠️ My Approach
Start from zero → Assume no prior terminal experience. Install Python, VS Code, and extensions step by step.

Isolate dependencies → Use a virtual environment (venv) so packages don’t conflict.

Build file by file → Create observability.py, ai_engine.py, main.py with clear responsibilities.

Instrument everything → Every request gets a request_id, logs in JSON, traces in OpenTelemetry, and metrics in Prometheus.

Simulate reality → Instead of calling OpenAI, simulate failures (timeout, rate_limit) to practice retry/fallback logic.

Expose endpoints → /ask for questions, /health for uptime, /metrics for Prometheus scraping, /tickets for incident tracking.

Test like SREs → Send curl/PowerShell requests, watch logs, metrics, and traces, and confirm tickets are created when outages are reported.

🎯 Why This Matters
For a fresher cloud platform engineer:

You learn observability patterns (logs, metrics, traces).

You practice resilience techniques (retry, fallback).

You see how incident automation can be wired into a service.

You build confidence in debugging workflows before touching Kubernetes or AWS/GCP.

This project is not about “just coding” — it’s about thinking like an SRE.

🚦 Quick Start
bash
# Clone repo
git clone https://github.com/yourusername/sentryops-ai
cd sentryops-ai

# Create virtual environment
python -m venv venv
venv\Scripts\Activate.ps1   # Windows PowerShell
# or source venv/bin/activate (Linux/Mac)

# Install dependencies
pip install -r requirements.txt

# Run server
uvicorn app.main:app --reload --port 8000
📡 Test It
powershell
# Normal question
Invoke-RestMethod -Uri "http://localhost:8000/ask" `
  -Method POST -ContentType "application/json" `
  -Body '{"question":"why is memory usage high on the checkout pods"}'

# Simulated failure
Invoke-RestMethod -Uri "http://localhost:8000/ask" `
  -Method POST -ContentType "application/json" `
  -Body '{"question":"database latency is spiking","simulate_failure":"timeout"}'
🌟 What I Learned
How to structure a Python project with clear modules.

How observability tools integrate into cloud services.

How to simulate and handle failures gracefully.

How to think in terms of problem → solution → approach, not just code.

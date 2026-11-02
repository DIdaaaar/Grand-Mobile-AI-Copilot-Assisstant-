# Grand Mobile Support Copilot

> Submission: see [SUBMISSION.md](SUBMISSION.md) for quickstart, metrics, safety, and limitations.

## Quickstart
The competition-supplied `rules.json` and `taxonomy.json` are **not redistributed**. Download your official handout privately and copy them to `backend/data/`.

```bash
python -m pip install -r backend/requirements.txt
make run
# other terminal
make predict IN=/absolute/path/tickets_valid.jsonl OUT=/absolute/path/pred.jsonl
```

API documentation: http://127.0.0.1:8000/docs. Endpoint: `POST /triage`.

The frontend is optional: `cd frontend && npm install && npm run dev`.

For Windows, run `setup_windows.bat`, then `start_backend.bat` and `start_frontend.bat`. GNU Make is required for the mandatory `make predict` command; on Windows, use WSL or Git Bash with make installed.

**Do not commit** game datasets, rules text, outputs, SQLite, `.env`, or private model artifacts. See [SUBMISSION.md](SUBMISSION.md).

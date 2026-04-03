# Contributing to audioslop

## Getting started

1. Fork and clone the repo
2. Install dependencies: `pip install flask f5-tts whisper python-docx pdfplumber pytest`
3. Create data directories: `mkdir ref uploads jobs content output audio`
4. Copy `.env.example` to `.env` and set your values
5. Run tests: `python -m pytest tests/ -v`
6. Start the dev server: `python app.py`

## Development workflow

1. Create a branch from `main` for your work
2. Write tests first when adding new functionality
3. Run the existing test suite before submitting: `python -m pytest tests/ -v`
4. Keep commits focused and descriptive

## Code style

- No emojis in source code, logs, or UI text
- Plain-language error messages (no jargon, no stack traces in user-facing text)
- Keep log messages short and factual
- Follow existing patterns in the codebase

## Pull requests

- Keep PRs focused on a single change
- Include a description of what changed and why
- Make sure tests pass
- New features should include tests where practical

## Reporting bugs

When filing an issue, include:
- What you did (steps to reproduce)
- What you expected to happen
- What actually happened
- The technical details from the "Show technical details" section if the error occurred in the web UI (copy the full text)
- Your OS and Python version

## Project structure

- `app.py` -- Flask routes and API endpoints
- `worker.py` -- Background job processing
- `audioslop.py` -- Text extraction and TTS cleaning pipeline
- `synthesize.py` -- F5-TTS voice synthesis with QA
- `qa.py` -- Transcription verification and word timing
- `db.py` -- Database layer
- `activity.py` -- Event logging
- `static/player.js` -- Audio player and transcript sync engine
- `templates/` -- Jinja2 HTML templates
- `tests/` -- pytest test suite

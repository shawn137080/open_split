# Contributing to SplitBot

Thanks for your interest! Contributions are welcome.

## Getting Started

1. Fork the repo and clone locally
2. Set up your `.env` (see `.env.example`)
3. Install dependencies: `pip install -r requirements.txt`
4. Run the bot: `python main.py`

## Guidelines

- **Bug fixes and docs**: Open a PR directly
- **New features**: Open an issue first to discuss
- **Code style**: Follow existing patterns (async/await, type hints, HTML parse mode for Telegram messages)

## Project Structure

```
main.py          — entry point, handler registration
database.py      — SQLite schema + CRUD helpers
config.py        — env var loading
tools/           — calculation and OCR utilities
workflows/       — per-command conversation flows
```

## Running Tests

No automated test suite yet — manual testing via Telegram.
If you add a feature, please test the full flow end-to-end.

## Feature Scope

This is the **open-source self-hosted** version. Some features are reserved for the hosted tier:
- Extended OCR models
- Cloud backup / DB export
- Multi-bot management dashboard

## License

MIT — see [LICENSE](LICENSE).

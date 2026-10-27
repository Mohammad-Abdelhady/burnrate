# BurnRate CLI v0.1

See where your AI coding money goes, and where you could save.

## Requirements
Python 3.8 or newer. No other installs needed.

## Run
```bash
python3 burnrate.py                 # analyze the last 30 days
python3 burnrate.py --days 7        # last 7 days
python3 burnrate.py --demo          # try it with sample data
python3 burnrate.py --html report.html
```
On Windows, use `python` instead of `python3`.

## What it reads
- **Claude Code**: `~/.claude/projects/**/*.jsonl`
- **Codex CLI** (experimental): `~/.codex/sessions/**/*.jsonl`
- **MCP servers configured** in Claude Code and Claude Desktop

Only metadata is read: model, token counts, timestamps and tool names.
Prompts, code and responses are never read. Nothing leaves your machine.

## Known limits (v0.1)
- Claude Desktop chat doesn't store token usage locally, so it can't be analyzed yet.
- Costs are API-equivalent estimates. Edit `PRICES` at the top of the file if prices change.
- The concise-output saving is an assumption (25% of output tokens), not a measurement.

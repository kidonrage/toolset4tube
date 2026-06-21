# toolset4tube

Local YouTube playlist evidence collector for AI-assisted curation.

`toolset4tube` v0 does not use the YouTube Data API, OAuth, a server, MCP, or an
embedded LLM. It calls the external `yt-dlp` binary, stores state in SQLite, writes
local evidence files, and renders reports from decisions made by an AI agent.

## Install

```bash
python3 -m pip install -e .
```

`yt-dlp` must be installed separately and available in `PATH` for `scan`.

## Commands

```bash
toolset4tube init
toolset4tube scan "PLAYLIST_URL" --limit 50 --offset 0
toolset4tube context --profile profile.example.yaml --limit 50
toolset4tube report
```

The agent reads `data/reports/agent-context.md`, writes decisions to
`data/reports/decisions.jsonl`, then `toolset4tube report` renders
`data/reports/report.md` and `data/reports/delete-candidates.csv`.

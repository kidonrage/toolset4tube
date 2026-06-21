# toolset4tube

Local YouTube playlist evidence collector for AI-assisted curation.

`toolset4tube` v0 does not use the YouTube Data API, OAuth, a server, MCP, or an
embedded LLM. It calls the external `yt-dlp` binary, stores state in SQLite, writes
local evidence files, and renders reports from decisions made by an AI agent.

## Install

```bash
python3 -m pip install -e .
```

## Dependencies

- Python 3.11+.
- `yt-dlp` installed separately and available in `PATH`.

Use the latest stable `yt-dlp` release. As of 2026-06-21, the latest upstream release is
`2026.06.09`. Check your local version with:

```bash
yt-dlp --version
```

If `scan` starts failing against YouTube, update `yt-dlp` before debugging this tool.

## Commands

```bash
toolset4tube init
toolset4tube scan "PLAYLIST_URL" --limit 50 --offset 0
toolset4tube context --profile profile.example.yaml --limit 50
toolset4tube report
```

`scan` writes live progress to stderr while `yt-dlp` fetches the playlist and while
selected entries are cached locally. Final counts are printed as scanned, errors, and
skipped.

The agent reads `data/reports/agent-context.md`, writes decisions to
`data/reports/decisions.jsonl`, then `toolset4tube report` renders
`data/reports/report.md` and `data/reports/delete-candidates.csv`.

# toolset4tube

Use this skill when the user asks to curate, triage, clean, or prioritize a YouTube playlist with `toolset4tube`.

## Contract

`toolset4tube` is a local evidence collector. It does not call an LLM, does not use the
YouTube Data API, and must not delete or move YouTube videos in v0.

The agent is responsible for classification. The CLI is responsible for facts and files.

## Workflow

1. Initialize local state if needed:

   ```bash
   toolset4tube init
   ```

2. Scan a playlist:

   ```bash
   toolset4tube scan "PLAYLIST_URL" --limit 50 --offset 0
   ```

3. Build agent context:

   ```bash
   toolset4tube context --profile profile.example.yaml --limit 50
   ```

4. Read:

   - `profile.example.yaml`
   - `data/reports/agent-context.md`

5. Write one JSON object per line to:

   - `data/reports/decisions.jsonl`

6. Render the report:

   ```bash
   toolset4tube report
   ```

7. Present the report to the user. Do not apply destructive actions.

## Decision Labels

Use exactly one of:

- `WATCH_FULLY`
- `WATCH_PARTIAL`
- `SUMMARY_ENOUGH`
- `DELETE`
- `LOW_CONFIDENCE`

## Decision Format

```json
{
  "video_id": "abc123",
  "decision": "WATCH_PARTIAL",
  "confidence": 0.78,
  "basis": "metadata",
  "reason": "Relevant to AI agents, but only one section matches the current profile.",
  "watch_ranges": [
    {
      "start": "12:30",
      "end": "28:10",
      "reason": "Agent workflow architecture"
    }
  ],
  "summary": null,
  "created_at": "2026-06-21T00:00:00Z"
}
```

## Classification Rules

- Prefer `DELETE` for low relevance, vague motivation content, outdated tutorials, duplicated topics, and long videos without practical value.
- Prefer `WATCH_FULLY` for practical, high-signal videos directly tied to the user's current goals.
- Prefer `WATCH_PARTIAL` when only a clear section or chapter range is useful.
- Prefer `SUMMARY_ENOUGH` when the idea is useful but the video does not need full attention.
- Use `LOW_CONFIDENCE` when metadata is too weak to safely classify.

If a video is classified from metadata only, set `basis` to `metadata`.

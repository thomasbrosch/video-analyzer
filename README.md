# Video Analyzer

Gemini-powered video analysis for ad creatives — a **web app**, a **desktop app**, a Python **CLI**, and an **MCP server**.

Point it at a video file or a folder of videos. Each video is uploaded to Gemini
(which watches the full video — audio, visuals, on-screen text, transitions) and
returns a structured ad-creative analysis as markdown: scene-by-scene breakdown,
full transcript, creative-structure teardown, performance signals, and an
iteration brief.

## Install

```bash
git clone https://github.com/thomasbrosch/video-analyzer.git
cd video-analyzer
pip install -e .
```

Requires Python 3.10+.

## API key

You bring your own Gemini key; this repo never contains one. Get a free key from https://aistudio.google.com/apikey, then set it once (Windows):

```bash
setx GEMINI_API_KEY <your_key>
```

Open a new terminal after `setx` so the variable is picked up. You can also pass
`--api-key` on the CLI or set it in the MCP server config.

## CLI usage

```bash
video-analyzer "C:\path\to\some_ad.mp4"           # analyze one video
video-analyzer "C:\path\to\ads_folder\"           # analyze a whole folder
video-analyzer "C:\path\to\ads_folder\" -m pro    # use the Pro model
video-analyzer "C:\path\to\ads_folder\" -o ".\reports\"   # save markdown files
video-analyzer "C:\path\to\some_ad.mp4" -v        # verbose (upload/processing logs)
video-analyzer "C:\path\to\some_ad.mp4" --json    # machine-readable JSON output
```

You can also run it as a module: `python -m video_analyzer <path>`.

### Options

| Flag | Meaning |
|------|---------|
| `-m`, `--model` | `flash` (default) or `pro` |
| `-o`, `--output` | Directory to write `<name>_analysis.md` files |
| `-k`, `--api-key` | Gemini API key (overrides `GEMINI_API_KEY`) |
| `--prompt` | Custom analysis prompt (overrides the default) |
| `--no-cleanup` | Keep uploaded files on Gemini after analysis |
| `-v`, `--verbose` | Verbose logging |
| `--json` | Output results as JSON |

## Models

| Name | API ID | Use for |
|------|--------|---------|
| `flash` | latest Gemini Flash | Batch processing a folder of ads, quick turnaround |
| `pro` | latest Gemini Pro | Deep single-video analysis, complex creatives |

## Desktop app (Windows)

```bash
video-analyzer-gui
```

Drag videos in, pick a model and report format (Markdown, Word, PDF), and paste your key once.

## Web app

A browser version with the same analysis. Run it locally:

```bash
pip install -e ".[web]"
export GEMINI_API_KEY=your_key      # optional; visitors can paste their own instead
python -m video_analyzer.webapp     # http://localhost:8080
```

Deploy it anywhere that runs a Docker container (Render, Google Cloud Run, Railway, Fly.io):

```bash
docker build -t video-analyzer .
docker run -p 8080:8080 -e GEMINI_API_KEY=your_key video-analyzer
```

Set `GEMINI_API_KEY` as a **secret environment variable on the host**, never in the repo. If you host it
publicly with your own key, protect your bill:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ACCESS_CODE` | none | Require a passphrase to use the server's key (visitors can still paste their own key) |
| `MAX_UPLOAD_MB` | 200 | Max video size |
| `RATE_LIMIT_PER_HOUR` | 5 | Analyses per IP per hour on the server's key |
| `DAILY_CAP` | 50 | Total analyses per day on the server's key |

Also set a spending cap on the key in Google AI Studio / Cloud billing.

## Supported formats

`.mp4 .mpeg .mov .avi .flv .mpg .webm .wmv .3gp .m4v .mkv`

## MCP server (Claude Desktop)

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "video-analyzer": {
      "command": "video-analyzer-mcp",
      "env": {
        "GEMINI_API_KEY": "<your_key>"
      }
    }
  }
}
```

Exposes two tools: `analyze_video` (file or folder) and `list_videos` (preview a folder).

## Privacy

Videos are sent to Google's Gemini API for analysis, so don't upload footage you aren't allowed to share with Google.
The web app deletes the uploaded file from the server and from Gemini storage when the analysis finishes.

## How it works

Videos are uploaded via the Gemini Files API, polled until they finish processing,
analyzed with `generate_content`, and (by default) deleted from Gemini storage
afterward. Large videos take longer to process; the uploader waits up to 5 minutes.

## License

MIT

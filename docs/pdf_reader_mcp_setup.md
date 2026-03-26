# PDF Reader MCP Setup (Manual Start Mode)

This project uses `pdf-reader-mcp` as an external MCP server.  
The server must be started manually before running the agent.

## 1. Start pdf-reader-mcp

```bash
cd /Users/a/Documents/Program/Lygent/mcps/pdf-reader-mcp
uv run pdf-reader
```

Expected endpoint:

- `http://localhost:8000/sse`

## 2. Configure environment variables

In `/Users/a/Documents/Program/Lygent/.env` add:

```env
PDF_READER_MCP_SSE_URL=http://localhost:8000/sse
PDF_PARSE_TIMEOUT_SEC=60
```

## 3. Run agent with a PDF

```bash
cd /Users/a/Documents/Program/Lygent
.venv/bin/python auto_researcher.py /absolute/path/to/paper.pdf
```

## 4. Troubleshooting

- `paper_path is empty`:
  - pass PDF path after `auto_researcher.py`.

- `Only PDF is supported in paper_parser`:
  - ensure file suffix is `.pdf`.

- `MCP parse error ... ConnectError/Connection refused`:
  - confirm `pdf-reader-mcp` is running.
  - confirm `PDF_READER_MCP_SSE_URL` is reachable.

- parse status is `partial`:
  - the PDF text is readable but abstract/sections are incomplete.
  - you can still continue workflow, but quality may drop.

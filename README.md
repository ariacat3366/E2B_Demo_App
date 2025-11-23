# Hackathon Demo App Overview

This repository hosts our E2B Hackathon demo project. The event rules we must satisfy:

```
• Teams of 1–4 members
• Choose exactly one track: online or offline
• Submission must include working code, a demo under 2 minutes, and proof of using both E2B Sandbox and at least one Docker Hub MCP
• Judging focuses on technical quality, innovation, and overall impression
• Online submission deadline: Nov 22, 09:00 PST
• Offline submission deadline: Nov 22, 17:30 PST
• Anything submitted outside the window or in multiple tracks is invalid
```

## App Description
- Goal: build an AI assistant that visualizes implementation diffs for GitHub `main`.
- Core flow:
  - Detect a new PR (preferred) or push targeting `main`.
  - Compare `main` with the feature branch, run the changed components inside E2B, and narrate the diff.
  - When the change is visualizable (frontend or CLI), capture before/after output and annotate screenshots.
- Trigger choice: PR events via GitHub Actions provide the clearest diff context; push events remain a fallback. Final decision after the first PoC.

## Team & Tooling
- Team setup: 1 PdM + 1 Engineer.
- Build window: Nov 22, 10:00–17:00 (7-hour sprint)
- Stack:
  - Cursor IDE as the primary workspace.
  - GPT5.1 Codex (this assistant) and Gemini 3.0 models to co-develop.
  - An auxiliary repository `Demo_Repository_for_Build_MCP_Agents` for frontend diff demos.

## Implementation Scope
1. GitHub integration to capture PR events and compute diffs.
2. E2B execution pipeline to run the changed code and generate explanations.
3. Capture UI/terminal outputs for meaningful before/after evidence.

### Appendix (stretch goals)
- UI flow that lets users point to diff regions via natural language.
- Screenshot-based diff viewer integrated into the app.
- Automated branch creation and PR scaffolding.

## MCP Configuration
We now use dedicated MCP servers for GitHub metadata and Playwright-driven screenshots. A sample `mcp.json` is included under `E2B_app/mcp.json`; adjust the commands as needed (Docker Hub images or local scripts are both supported). Configure the following environment variables in `.env`:

```
# Required
E2B_API_KEY=sk_e2b_xxx
GITHUB_ACCESS_TOKEN=ghp_xxx              # used by PyGithub + GitHub uploads
MCP_GITHUB_PAT=ghp_xxx                   # GitHub MCP (scopes: repo, read:org)

# GitHub MCP client (defaults point to @modelcontextprotocol/server-github)
GITHUB_MCP_COMMAND=["npx","-y","@modelcontextprotocol/server-github"]
GITHUB_MCP_TOOL=get_pull_request_files   # tool must return files[] with patch/content
MCP_GITHUB_REPOSITORY=owner/repo         # consumed by mcp.json (optional)

# Playwright MCP (defaults to bundled Python server)
PLAYWRIGHT_MCP_COMMAND=["python3","/home/user/playwright_mcp_server.py"]
PLAYWRIGHT_MCP_TOOL=playwright.capture
PLAYWRIGHT_BASE_URL=http://localhost:5173
PLAYWRIGHT_WAIT_AFTER_MS=2000
```

During sandbox execution the agent launches both MCP servers, first running `resources/list` to confirm scope and then invoking `tools/call`. GitHub MCP is asked for the PR’s changed files (including `patch` + `content`),その結果を Groq に渡してページターゲットを JSON で生成し、Playwright MCP へ引き継ぎます。バンドル済み Playwright MCP サーバーは Python + Playwright のみで動作しますが、Docker Hub MCP（例: `ghcr.io/build-mcp/browser`）へ差し替える場合は `PLAYWRIGHT_MCP_COMMAND` を上書きしてください。Keep personal tokens out of tracked files—store them in `.env` and reference via environment variables inside `mcp.json`.
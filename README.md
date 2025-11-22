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
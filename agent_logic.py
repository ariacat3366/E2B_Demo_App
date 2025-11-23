import os
import base64
import json
import time
import re
import shlex
import requests
from pathlib import Path
from string import Template
from e2b_code_interpreter import Sandbox
from dotenv import load_dotenv
from groq import Groq

load_dotenv()


SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


class UnknownMcpToolError(Exception):
    """Raised when the configured MCP server does not expose the requested tool."""


class DiffVisionAgent:
    @staticmethod
    def _parse_command(command_text: str):
        if not command_text:
            return None
        text = command_text.strip()
        if not text:
            return None
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                    return parsed
            except json.JSONDecodeError:
                pass
        try:
            split_cmd = shlex.split(text)
            return split_cmd or None
        except ValueError:
            return None

    def _resolve_playwright_mcp_command(self):
        direct = os.getenv("PLAYWRIGHT_MCP_COMMAND")
        if direct:
            parsed = self._parse_command(direct)
            if parsed:
                return parsed
        server_cmd = os.getenv("MCP_SERVER_COMMAND")
        if server_cmd:
            parts = [server_cmd.strip()]
            server_args = os.getenv("MCP_SERVER_ARGS", "")
            if server_args:
                parts += shlex.split(server_args)
            return parts
        return None

    def __init__(self):
        self.e2b_api_key = os.getenv("E2B_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.github_token = os.getenv("GITHUB_ACCESS_TOKEN")
        self.mcp_github_pat = os.getenv("MCP_GITHUB_PAT")
        self.github_mcp_command = self._parse_command(os.getenv("GITHUB_MCP_COMMAND", ""))
        self.github_mcp_tool = os.getenv(
            "GITHUB_MCP_TOOL", "github_get_pull_request"
        )  # Default to get_pull_request (may need adaptation for files)
        self.github_mcp_pr_tool = os.getenv("GITHUB_MCP_PR_TOOL", "github_get_pull_request")
        self.github_mcp_list_tool = os.getenv("GITHUB_MCP_LIST_TOOL", "github_list_pull_requests")
        self.playwright_mcp_command = self._resolve_playwright_mcp_command()
        self.playwright_mcp_tool = os.getenv("PLAYWRIGHT_MCP_TOOL", "playwright.capture")
        self._playwright_mcp_builtin = False
        self._builtin_playwright_uploaded = False
        if not self.playwright_mcp_command:
            # Will upload bundled MCP server script into the sandbox on demand.
            self.playwright_mcp_command = ["python3", "/home/user/playwright_mcp_server.py"]
            self._playwright_mcp_builtin = True
        self.sandbox_python_cmd = os.getenv("SANDBOX_PYTHON_CMD") or os.getenv("PYTHON_CMD") or "python3"
        self.playwright_base_url = os.getenv("PLAYWRIGHT_BASE_URL", "http://localhost:5173").rstrip("/")
        wait_ms_value = os.getenv("PLAYWRIGHT_WAIT_AFTER_MS", "2000")
        try:
            self.playwright_wait_after_ms = max(0, int(wait_ms_value))
        except ValueError:
            self.playwright_wait_after_ms = 2000
        server_wait_value = os.getenv("PLAYWRIGHT_SERVER_WAIT_SECONDS", "60")
        try:
            self.playwright_server_wait_seconds = max(10, int(server_wait_value))
        except ValueError:
            self.playwright_server_wait_seconds = 60

        if not self.e2b_api_key:
            raise ValueError("E2B_API_KEY not found in .env")

        self.groq_client = Groq(api_key=self.groq_api_key) if self.groq_api_key and Groq else None
        self.selector_model = os.getenv("GROQ_SELECTOR_MODEL", "llama3-8b-8192")
        self.target_model = os.getenv("GROQ_TARGET_MODEL", "llama3-8b-8192")
        self.vision_model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        self.diff_prompt_chars = int(os.getenv("GIT_DIFF_PROMPT_CHARS", "6000"))

        # E2B MCP Gateway settings (will be set during sandbox creation)
        self.mcp_gateway_url = None
        self.mcp_gateway_token = None
        self.use_e2b_mcp_gateway = os.getenv("USE_E2B_MCP_GATEWAY", "true").lower() == "true"

    @staticmethod
    def _parse_repo_info(repo_url):
        """Extract owner and repo name from URL."""
        if not repo_url:
            return None, None
        clean = repo_url.rstrip("/").replace(".git", "")
        parts = clean.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return None, None

    @staticmethod
    def _to_text(stream):
        if isinstance(stream, (bytes, bytearray)):
            return stream.decode("utf-8", errors="ignore")
        return stream or ""

    @staticmethod
    def _ensure_bytes(data):
        if isinstance(data, bytearray):
            return bytes(data)
        return data

    @staticmethod
    def _safe_label(label: str, index: int) -> str:
        base = label or f"page_{index + 1}"
        sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_")
        sanitized = sanitized[:50] or f"page_{index + 1}"
        return sanitized

    @staticmethod
    def _render_template(template_name: str, **context) -> str:
        template_path = SCRIPTS_DIR / template_name
        template = Template(template_path.read_text())
        return template.substitute(**context)

    def _call_mcp(self, sandbox, command, requests, extra_env=None):
        """
        Call MCP server. If E2B MCP Gateway is available, use HTTP.
        Otherwise, fall back to stdio-based JSON-RPC.
        """
        if self.use_e2b_mcp_gateway and self.mcp_gateway_url:
            return self._call_mcp_http(requests)

        # Fallback to stdio-based MCP
        if not command or not requests:
            return None
        env = {}
        env.update(extra_env or {})
        script_text = self._render_template(
            "jsonrpc_client.py.tpl",
            command_json=json.dumps(command),
            requests_json=json.dumps(requests),
            env_updates_json=json.dumps(env),
        )
        sandbox.files.write("/home/user/run_mcp.py", script_text)
        proc = sandbox.commands.run(f"{self.sandbox_python_cmd} /home/user/run_mcp.py")
        stdout_text = self._to_text(proc.stdout).strip()
        if not stdout_text:
            return None
        try:
            return json.loads(stdout_text)
        except json.JSONDecodeError:
            return None

    def _call_mcp_http(self, mcp_requests):
        """
        Call MCP server via E2B MCP Gateway using HTTP.
        Reference: https://docs.docker.com/ai/mcp-catalog-and-toolkit/e2b-sandboxes/
        """
        if not self.mcp_gateway_url or not mcp_requests:
            return None

        headers = {"Content-Type": "application/json"}
        if self.mcp_gateway_token:
            headers["Authorization"] = f"Bearer {self.mcp_gateway_token}"

        results = []
        for req in mcp_requests:
            try:
                response = requests.post(self.mcp_gateway_url, json=req, headers=headers, timeout=30)
                response.raise_for_status()

                # Try to parse JSON response
                try:
                    results.append(response.json())
                except json.JSONDecodeError as json_err:
                    # Log the actual response for debugging
                    error_msg = f"JSON parse error: {json_err}. Response text: {response.text[:200]}"
                    results.append({"error": {"message": error_msg}})

            except requests.exceptions.RequestException as e:
                results.append({"error": {"message": f"HTTP error: {str(e)}"}})
            except Exception as e:
                results.append({"error": {"message": f"Unexpected error: {str(e)}"}})

        return results if results else None

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
        return slug or "page"

    @staticmethod
    def _extract_mcp_image(response):
        if not response:
            return None
        payload = response[0] if isinstance(response, list) else response
        result = payload.get("result")
        if not result:
            return None
        contents = result.get("content", [])
        for item in contents:
            content_type = item.get("type", "").lower()
            data = item.get("data")
            if not data:
                continue
            if content_type in {"image", "image/png", "image/jpeg"}:
                try:
                    return base64.b64decode(data)
                except Exception:
                    continue
            mime_type = item.get("mimeType", "").lower()
            if mime_type in {"image/png", "image/jpeg"}:
                try:
                    return base64.b64decode(data)
                except Exception:
                    continue
        return None

    @staticmethod
    def _split_diff_by_file(diff_text: str):
        chunks = []
        current = None
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                if current:
                    chunks.append(current)
                current = {"file": None, "lines": []}
                match = re.match(r"diff --git a/(.+) b/(.+)", line)
                if match:
                    current["file"] = match.group(2)
            elif current:
                current["lines"].append(line)
        if current:
            chunks.append(current)
        return chunks

    def _guess_route_from_path(self, path: str) -> str:
        if not path:
            return "/"
        path_obj = Path(path)
        candidates = [path_obj.stem.lower(), path_obj.parent.name.lower()]
        for candidate in candidates:
            if candidate and candidate not in ("index", "app"):
                slug = self._slugify(candidate)
                return f"/{slug}"
        return "/"

    @staticmethod
    def _extract_selector_from_lines(lines):
        joined = "\n".join(lines)
        patterns = [
            r'className\s*=\s*"([^"]+)"',
            r"className\s*=\s*'([^']+)'",
            r'id\s*=\s*"([^"]+)"',
            r"id\s*=\s*'([^']+)'",
        ]
        for pattern in patterns:
            match = re.search(pattern, joined)
            if match:
                value = match.group(1)
                if value.startswith(".") or value.startswith("#"):
                    return value
                if pattern.startswith("class"):
                    return "." + value.split()[0]
                return "#" + value
        return None

    def _build_mcp_config(self):
        """
        Compose MCP server configuration for Sandbox.create.
        Uses E2B's built-in MCP support to automatically manage MCP servers.
        Reference: https://docs.docker.com/ai/mcp-catalog-and-toolkit/e2b-sandboxes/
        """
        mcp_servers = {}
        if self.mcp_github_pat:
            # E2B will automatically start the GitHub MCP server
            # using the official Docker Hub image
            mcp_servers["githubOfficial"] = {"githubPersonalAccessToken": self.mcp_github_pat}
        return mcp_servers

    def detect_navigation_targets(
        self, diff_text: str, repo_url: str, sandbox, max_pages: int = 1, pr_number: int = None, logger=None
    ):
        """
        Infer navigation targets (path + selector) from diff data.
        """
        logger = logger or (lambda *_: None)
        max_pages = max(1, max_pages)
        if not diff_text:
            selector = "body"
            return [
                {
                    "label": "root",
                    "path": "/",
                    "selector": selector,
                    "steps": [],
                    "safe_label": self._safe_label("root", 0),
                }
            ]

        # Try GitHub MCP if available (E2B Gateway or stdio)
        mcp_available = (self.use_e2b_mcp_gateway and self.mcp_gateway_url) or self.github_mcp_command

        if mcp_available:
            logger("🧩 GitHub MCP へナビゲーションターゲットを問い合わせ中...")
            mcp_targets = self._github_mcp_targets(
                sandbox, diff_text, repo_url, pr_number, max_pages=max_pages, logger=logger
            )
            if mcp_targets:
                logger(f"✅ GitHub MCP から {len(mcp_targets)} 件のターゲットを取得。")
                return mcp_targets
            logger("⚠️ GitHub MCP がターゲットを返さなかったためヒューリスティックへフォールバック。")
        else:
            logger("ℹ️ GitHub MCP が未設定のためヒューリスティック解析のみを実行。")

        chunks = self._split_diff_by_file(diff_text)
        targets = []
        for chunk in chunks:
            file_path = chunk.get("file")
            if not file_path:
                continue
            label_candidate = Path(file_path).stem or "page"
            route = self._guess_route_from_path(file_path)
            selector = self._extract_selector_from_lines(chunk.get("lines", [])) or "body"
            label = label_candidate.title()
            if not any(t["label"] == label for t in targets):
                targets.append(
                    {
                        "label": label,
                        "path": route,
                        "selector": selector,
                        "steps": [],
                    }
                )
            if len(targets) >= max_pages:
                break

        if not targets:
            selector = self.detect_changed_selector(diff_text)
            targets.append({"label": "root", "path": "/", "selector": selector or "body", "steps": []})

        for idx, target in enumerate(targets):
            target["safe_label"] = self._safe_label(target["label"], idx)
        return targets

    def _github_mcp_targets(self, sandbox, diff_text, repo_url, pr_number, max_pages, logger=None):
        logger = logger or (lambda *_: None)

        # Check if GitHub MCP is available (either via E2B Gateway or stdio)
        if not self.use_e2b_mcp_gateway and not self.github_mcp_command:
            return None
        if self.use_e2b_mcp_gateway and not self.mcp_gateway_url:
            return None

        if not pr_number:
            logger("⚠️ PR番号が不明なため GitHub MCP ターゲット解析をスキップします。")
            return None

        self._github_mcp_list_resources(sandbox, logger=logger)
        file_payloads = self._github_mcp_fetch_files(sandbox, repo_url, pr_number, logger=logger)
        if not file_payloads:
            logger("⚠️ GitHub MCP がターゲット候補となるファイル情報を返しませんでした。")
            return None

        targets = []
        for file_entry in file_payloads:
            file_path = file_entry.get("path") or file_entry.get("file") or file_entry.get("filename")
            if not file_path:
                continue
            diff_text = file_entry.get("patch") or file_entry.get("diff") or ""
            diff_lines = diff_text.splitlines()
            file_content = file_entry.get("content") or file_entry.get("text") or ""
            if not file_content:
                file_content = self._github_mcp_read_file(sandbox, file_path, repo_url=repo_url, pr_number=pr_number)
            groq_targets = self._groq_targets_from_file(file_path, diff_lines, file_content)
            for idx, target in enumerate(groq_targets or []):
                label = target.get("label") or target.get("title") or f"{Path(file_path).stem or 'Page'}"
                targets.append(
                    {
                        "label": label,
                        "path": target.get("path") or target.get("url") or self._guess_route_from_path(file_path),
                        "selector": target.get("selector") or "body",
                        "steps": target.get("steps") or [],
                        "safe_label": self._safe_label(label, len(targets)),
                    }
                )
                if len(targets) >= max_pages:
                    break
            if len(targets) >= max_pages:
                break
        return targets or None

    def _github_mcp_list_resources(self, sandbox, logger=None):
        # Skip if not using E2B MCP Gateway
        if not self.use_e2b_mcp_gateway or not self.mcp_gateway_url:
            return

        logger = logger or (lambda *_: None)
        mcp_requests = [
            {
                "jsonrpc": "2.0",
                "method": "resources/list",
                "id": 111,
                "params": {},
            }
        ]
        try:
            response = self._call_mcp(sandbox, None, mcp_requests, None)
        except Exception as exc:
            logger(f"⚠️ GitHub MCP resources/list 失敗: {exc}")
            return
        if not response:
            logger("ℹ️ GitHub MCP resources/list は空の応答でした。")
            return
        entry = response[0]
        if entry.get("error"):
            logger(f"⚠️ GitHub MCP resources/list エラー: {entry['error']}")
            return
        logger(f"📚 GitHub MCP resources: {json.dumps(entry.get('result', {}), indent=2)}")

    def _github_mcp_fetch_files(self, sandbox, repo_url, pr_number, logger=None):
        """
        Ask the configured GitHub MCP tool for PR file details (diff + content).
        When using E2B MCP Gateway, uses HTTP; otherwise uses stdio.
        """
        # Skip if MCP not configured
        if not self.use_e2b_mcp_gateway and not self.github_mcp_command:
            return None
        if self.use_e2b_mcp_gateway and not self.mcp_gateway_url:
            return None

        logger = logger or (lambda *_: None)
        owner, repo = self._parse_repo_info(repo_url)
        arguments = {
            "owner": owner,
            "repo": repo,
            "pull_number": pr_number,  # E2B uses snake_case
        }
        # Note: 'includeDiff' etc might be ignored by standard MCP servers but we keep them just in case
        # arguments.update({"includeDiff": True}) # Custom args often not supported by standard

        # Standard GitHub MCP uses get_pull_request but it may not return files list directly.
        # But user wants to try MCP. Let's assume the tool provided (github_mcp_tool) is capable.
        # If the user is using the standard server, they might rely on 'github_list_commits' or similar?
        # Actually, let's keep the user's previous assumption that 'github_mcp_tool' fetches files,
        # but align arguments to what we used in fetch_pr_metadata.
        requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 77,
                "params": {
                    "name": self.github_mcp_tool,
                    "arguments": arguments,
                },
            }
        ]
        env_updates = {}
        if self.mcp_github_pat:
            env_updates["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.mcp_github_pat
        try:
            response = self._call_mcp(sandbox, self.github_mcp_command, requests, env_updates)
        except Exception as exc:
            logger(f"⚠️ GitHub MCP から PR ファイル情報を取得できませんでした: {exc}")
            return None
        if not response:
            logger("ℹ️ GitHub MCP からの応答が空でした。")
            return None
        entry = response[0]
        if entry.get("error"):
            logger(f"⚠️ GitHub MCP tools/call エラー: {entry['error']}")
            return None
        contents = entry.get("result", {}).get("content", [])
        for item in contents:
            if item.get("type") == "json" and item.get("data"):
                data = item["data"]
            elif item.get("type") == "text" and item.get("text"):
                try:
                    data = json.loads(item["text"])
                except json.JSONDecodeError:
                    continue
            else:
                continue
            files = data.get("files") if isinstance(data, dict) else None
            if isinstance(files, list):
                logger(f"📄 GitHub MCP から {len(files)} 件のファイル情報を取得。")
                return files
        logger("ℹ️ GitHub MCP 応答にファイル詳細が含まれていませんでした。")
        return None

    def _prepare_targets(self, targets, max_pages):
        prepared = []
        effective = targets or [{"label": "root", "path": "/", "selector": "body", "steps": []}]
        for idx, target in enumerate(effective[: max(1, max_pages)]):
            prepared.append(
                {
                    "label": target.get("label") or f"page_{idx + 1}",
                    "safe_label": target.get("safe_label") or self._safe_label(target.get("label", ""), idx),
                    "path": self._normalize_path(target.get("path")),
                    "selector": target.get("selector") or "body",
                    "steps": target.get("steps") or [],
                }
            )
        return prepared

    def _github_mcp_read_file(self, sandbox, file_path, repo_url=None, pr_number=None):
        if not self.github_mcp_command:
            return None

        owner, repo = self._parse_repo_info(repo_url)
        # Using read_file tool from standard GitHub MCP
        requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": "github_read_file",  # Assuming standard name
                    "arguments": {
                        "owner": owner,
                        "repo": repo,
                        "path": file_path,
                        # "branch": ... # If we need specific branch
                    },
                },
            }
        ]
        env_updates = {}
        if self.mcp_github_pat:
            env_updates["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.mcp_github_pat
        response = self._call_mcp(sandbox, self.github_mcp_command, requests, env_updates)
        if not response:
            return None
        entry = response[0]
        if entry.get("error"):
            return None
        contents = entry.get("result", {}).get("content", [])
        for item in contents:
            if item.get("type") == "text" and item.get("text"):
                return item["text"]
            if item.get("type") == "json" and item.get("data"):
                return item["data"]
        return None

    def _groq_targets_from_file(self, file_path, diff_lines, file_content):
        if not self.groq_client:
            return None
        diff_excerpt = "\n".join(diff_lines or [])[:2000]
        file_excerpt = (file_content or "")[:2000]
        prompt = f"""
You are a UI diff analyst. Given a Git diff snippet and the current file content, identify the UI elements most likely impacted.
Return a JSON array where each element has:
- label: human friendly name
- path: route or relative URL to visit (guess if needed)
- selector: CSS selector for the primary element
- steps: optional list of text instructions (click, input) to reach the element

File Path: {file_path}

Git Diff Snippet:
```
{diff_excerpt}
```

File Content Snippet:
```
{file_excerpt}
```

Respond with JSON only.
"""
        try:
            completion = self.groq_client.chat.completions.create(
                model=self.target_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content
            data = json.loads(content)
            if isinstance(data, dict) and "targets" in data:
                data = data["targets"]
            if isinstance(data, list):
                return data
        except Exception:
            return None
        return None

    def _capture_branch_pages(self, sandbox, branch_name, label, branch_key, targets, logger=None):
        logger = logger or (lambda *_: None)
        # Always use local Playwright (not MCP) for better reliability
        logger(f"🎥 ローカル Playwright で {label} のスクリーンショットを取得します。")
        return self._capture_branch_pages_local(sandbox, branch_name, label, branch_key, targets, logger=logger)

    def _capture_branch_pages_local(self, sandbox, branch_name, label, branch_key, targets, logger=None):
        logger = logger or (lambda *_: None)
        log_prefix = f"{label}: {branch_name}"
        target_count = len(targets) if targets else 0
        prepared_targets = self._prepare_targets(targets, target_count or 1)
        sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}")
        sandbox.commands.run("cd /home/user/repo && npm install")
        sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True)
        self._wait_for_dev_server(sandbox, logger=logger)

        targets_literal = json.dumps(prepared_targets)
        script_text = self._render_template(
            "capture_targets.py.tpl",
            targets_json=targets_literal,
            base_url=self.playwright_base_url,
        )
        sandbox.files.write("/home/user/take_shot.py", script_text)
        proc = sandbox.commands.run("python3 /home/user/take_shot.py")
        stdout_text = self._to_text(proc.stdout)
        stderr_text = self._to_text(proc.stderr)
        if stderr_text:
            print(f"[Agent] ℹ️ Screenshot script stderr ({log_prefix}): {stderr_text}")
        if stdout_text:
            print(f"[Agent] ℹ️ Screenshot script stdout ({log_prefix}): {stdout_text}")

        branch_results = {}
        for target in prepared_targets:
            orig_label = target["label"]
            safe_label = target["safe_label"]
            branch_results[orig_label] = {"full": None, "partial": None}
            full_path = f"/home/user/{safe_label}_full.png"
            partial_path = f"/home/user/{safe_label}_partial.png"
            try:
                full_bytes = sandbox.files.read(full_path, format="bytes")
                branch_results[orig_label]["full"] = self._ensure_bytes(full_bytes)
            except Exception:
                pass
            try:
                partial_bytes = sandbox.files.read(partial_path, format="bytes")
                branch_results[orig_label]["partial"] = self._ensure_bytes(partial_bytes)
            except Exception:
                pass

        sandbox.commands.run("pkill -f node")
        return branch_results

    def _capture_branch_pages_code_mcp(self, sandbox, branch_name, label, branch_key, targets, logger=None):
        logger = logger or (lambda *_: None)
        log_prefix = f"{label}: {branch_name}"
        target_count = len(targets) if targets else 0
        prepared_targets = self._prepare_targets(targets, target_count or 1)
        sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}")

        logger(f"📦 Installing dependencies for {branch_name}...")
        install_proc = sandbox.commands.run("cd /home/user/repo && npm install")
        if install_proc.exit_code != 0:
            logger(f"❌ npm install failed: {self._to_text(install_proc.stderr)}")
            return {}

        logger(f"🚀 Starting dev server for {branch_name}...")

        # Check if dev script exists in package.json
        check_script = sandbox.commands.run("cd /home/user/repo && cat package.json | grep -A 5 '\"scripts\"'")
        logger(f"📋 package.json scripts: {self._to_text(check_script.stdout)[:200]}")

        # Start dev server in background
        sandbox.commands.run(
            "cd /home/user/repo && npm run dev -- --host > /home/user/server.log 2>&1 &", background=True
        )

        # Wait a bit for server to start
        time.sleep(3)

        # Check if server process is running
        ps_check = sandbox.commands.run("ps aux | grep -v grep | grep 'npm\\|node\\|vite'")
        if ps_check.exit_code == 0:
            logger(f"✅ Dev server process is running")
        else:
            logger(f"⚠️ Dev server process not found, checking logs...")
            log_check = sandbox.commands.run("tail -n 50 /home/user/server.log")
            logger(f"📄 Server log: {self._to_text(log_check.stdout)}")

        self._wait_for_dev_server(sandbox, logger=logger)

        if self._playwright_mcp_builtin:
            self._ensure_builtin_playwright_mcp(sandbox, logger=logger)

        available_tools = self._list_available_mcp_tools(sandbox, logger=logger)
        if available_tools and self.playwright_mcp_tool not in available_tools:
            fallback_tool = self._select_fallback_tool(available_tools)
            logger(f"⚠️ MCP ツール {self.playwright_mcp_tool} が利用リストに無いため {fallback_tool} に切り替えます。")
            self.playwright_mcp_tool = fallback_tool

        branch_results = {}
        try:
            for target in prepared_targets:
                branch_results[target["label"]] = {"full": None, "partial": None}
                url = self._compose_target_url(target.get("path"))
                selector = target.get("selector") or "body"
                steps = target.get("steps") or []

                logger(f"📸 {log_prefix} -> {target['label']} を {url} で撮影します。")
                entry = self._invoke_playwright_mcp(
                    sandbox,
                    {
                        "url": url,
                        "selector": selector,
                        "steps": steps,
                        "waitAfterNavigateMs": self.playwright_wait_after_ms,
                        "label": target["label"],
                    },
                )

                full_bytes, focus_bytes, error_msg = self._parse_playwright_mcp_entry(entry)
                if full_bytes:
                    branch_results[target["label"]]["full"] = full_bytes
                if focus_bytes:
                    branch_results[target["label"]]["partial"] = focus_bytes
                if not full_bytes:
                    logger(f"⚠️ {log_prefix} -> {target['label']} のフルスクリーンショット取得に失敗しました。")
                    if error_msg:
                        logger(f"   MCP Error: {error_msg}")
                    log_proc = sandbox.commands.run("tail -n 200 /home/user/server.log")
                    full_log = self._to_text(log_proc.stdout)
                    if full_log:
                        logger(f"   Server Log (last 500 chars): {full_log[-500:]}")
        finally:
            sandbox.commands.run("pkill -f node")
        return branch_results

    def _ensure_builtin_playwright_mcp(self, sandbox, logger=None):
        if not self._playwright_mcp_builtin:
            return
        if self._builtin_playwright_uploaded:
            return
        script_path = SCRIPTS_DIR / "playwright_mcp_server.py"
        sandbox.files.write("/home/user/playwright_mcp_server.py", script_path.read_text())
        self._builtin_playwright_uploaded = True
        if logger:
            logger("🛠️ Bundled Playwright MCP サーバースクリプトを配置しました。")

    def _list_available_mcp_tools(self, sandbox, logger=None):
        logger = logger or (lambda *_: None)
        if not self.playwright_mcp_command:
            return []
        attempts = 3
        delay_seconds = 3
        for attempt in range(1, attempts + 1):
            try:
                logger(f"🔍 Debug: Checking available MCP tools... (attempt {attempt}/{attempts})")
                tool_list_req = [{"jsonrpc": "2.0", "method": "tools/list", "id": 999, "params": {}}]
                tools_resp = self._call_mcp(sandbox, self.playwright_mcp_command, tool_list_req, None)
                if tools_resp:
                    logger(f"🛠 Available MCP tools: {json.dumps(tools_resp, indent=2)}")
                    entry = tools_resp[0] if isinstance(tools_resp, list) and tools_resp else {}
                    result = entry.get("result", {}) if isinstance(entry, dict) else {}
                    return [tool.get("name") for tool in result.get("tools", []) or [] if tool.get("name")]
                logger("⚠️ tools/list 応答が空でした。")
            except Exception as exc:
                logger(f"⚠️ Failed to list MCP tools (attempt {attempt}/{attempts}): {exc}")
            if attempt < attempts:
                wait = min(delay_seconds * attempt, 10)
                logger(f"⏳ MCP tools/list 再試行まで {wait} 秒待機します...")
                time.sleep(wait)
        logger("⚠️ MCP tools/list が連続で失敗したため、利用可能ツールは不明です。")
        return []

    @staticmethod
    def _select_fallback_tool(available_tools):
        for candidate in ("playwright.capture", "playwright/screenshot", "capture"):
            if candidate in available_tools:
                return candidate
        return available_tools[0]

    @staticmethod
    def _normalize_path(path):
        if not path:
            return "/"
        return path if path.startswith("/") else f"/{path}"

    def _compose_target_url(self, path):
        suffix = path or "/"
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return f"{self.playwright_base_url}{suffix}"

    def _wait_for_dev_server(self, sandbox, logger=None):
        logger = logger or (lambda *_: None)
        health_url = self._compose_target_url("/")
        deadline = time.time() + self.playwright_server_wait_seconds
        attempt = 0

        logger(f"⏳ Waiting for dev server at {health_url}...")

        while time.time() < deadline:
            attempt += 1
            # Use curl with built-in timeout and max-time options
            command = (
                f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 5 {shlex.quote(health_url)}"
            )
            try:
                proc = sandbox.commands.run(command)
                status_code = self._to_text(proc.stdout).strip()

                if proc.exit_code == 0 and status_code:
                    if status_code.startswith(("2", "3")):
                        logger(f"✅ Dev server ready (HTTP {status_code}) at {health_url}")
                        return True
                    logger(f"⚠️ Dev server responded with HTTP {status_code}; retrying ({attempt})...")
                else:
                    # Log more details about the failure
                    if proc.exit_code == 7:
                        logger(
                            f"ℹ️ Connection refused (attempt {attempt}/~{self.playwright_server_wait_seconds//2}). Server may still be starting..."
                        )
                    else:
                        logger(f"ℹ️ curl exit code {proc.exit_code} (attempt {attempt}); retrying...")

                    # Every 5 attempts, check server logs
                    if attempt % 5 == 0:
                        log_check = sandbox.commands.run(
                            "tail -n 10 /home/user/server.log 2>/dev/null || echo 'No log file'"
                        )
                        log_output = self._to_text(log_check.stdout).strip()
                        if log_output and log_output != "No log file":
                            logger(f"📄 Server log (last 10 lines): {log_output[-300:]}")

            except Exception as e:
                logger(f"ℹ️ Dev server check failed (attempt {attempt}): {e}")

            time.sleep(3)  # Increased from 2 to 3 seconds

        logger(f"⚠️ Dev server readiness check timed out after {self.playwright_server_wait_seconds} 秒: {health_url}")

        # Final log check
        final_log = sandbox.commands.run("tail -n 50 /home/user/server.log 2>/dev/null || echo 'No log file'")
        logger(f"📄 Final server log: {self._to_text(final_log.stdout)}")

        return False

    def _invoke_playwright_mcp(self, sandbox, arguments):
        if not self.playwright_mcp_command:
            return None
        requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": arguments.get("label") or 1,
                "params": {
                    "name": self.playwright_mcp_tool,
                    "arguments": arguments,
                },
            }
        ]
        response = self._call_mcp(sandbox, self.playwright_mcp_command, requests, None)
        if response:
            entry = response[0]
            error = entry.get("error")
            if error and "Unknown tool" in (error.get("message") or ""):
                raise UnknownMcpToolError(error.get("message") or "Unknown tool")
            return entry
        return None

    def _parse_playwright_mcp_entry(self, entry):
        if not entry:
            return (None, None, "No response from MCP server")
        if entry.get("error"):
            message = entry["error"].get("message") if isinstance(entry["error"], dict) else str(entry["error"])
            return (None, None, message)
        contents = entry.get("result", {}).get("content", [])
        images = []
        focus = None
        text_blob = []
        for item in contents:
            item_type = (item.get("type") or "").lower()
            if item_type in {"image", "image/png", "image/jpeg"} and item.get("data"):
                try:
                    decoded = base64.b64decode(item["data"])
                    images.append(decoded)
                except Exception:
                    continue
            elif item_type == "json" and item.get("data"):
                data = item["data"]
                full_data = data.get("full") or data.get("full_png")
                focus_data = data.get("focus") or data.get("selector_png")
                if full_data:
                    try:
                        images.append(base64.b64decode(full_data))
                    except Exception:
                        pass
                if focus_data:
                    try:
                        focus = base64.b64decode(focus_data)
                    except Exception:
                        pass
            elif item_type == "text" and item.get("text"):
                text_blob.append(item["text"])
        text_joined = "\n".join(text_blob)
        full_bytes = None
        focus_bytes = focus
        for marker in ("FULL_B64::", "FOCUS_B64::"):
            data = self._extract_marker_image(text_joined, marker)
            if data:
                if marker.startswith("FULL") and not full_bytes:
                    full_bytes = data
                elif marker.startswith("FOCUS") and not focus_bytes:
                    focus_bytes = data
        if not full_bytes and images:
            full_bytes = images[0]
        if not focus_bytes and len(images) > 1:
            focus_bytes = images[1]
        return (full_bytes, focus_bytes, None)

    @staticmethod
    def _extract_marker_image(text_blob, marker):
        if not text_blob or marker not in text_blob:
            return None
        pattern = rf"{re.escape(marker)}([A-Za-z0-9+/=]+)"
        match = re.search(pattern, text_blob)
        if not match:
            return None
        try:
            return base64.b64decode(match.group(1))
        except Exception:
            return None

    def analyze_pr(
        self,
        repo_url: str,
        pr_number: int = None,
        status_callback=None,
        skip_ai: bool = False,
        notify_github: bool = False,
        max_pages: int = 1,
    ):
        """
        Main Logic: Diff -> Selectors -> Screenshots (Full & Partial) -> GitHub Comment
        """
        results = {
            "main_screenshot": None,
            "feature_screenshot": None,
            "partial_screenshot": None,
            "git_diff": "",
            "ai_report": "",
            "logs": [],
            "page_screenshots": {},
            "page_order": [],
        }

        def log(message):
            results["logs"].append(message)
            if status_callback:
                status_callback(message)
            print(f"[Agent] {message}")

        envs = {}
        if self.github_token:
            envs["GITHUB_ACCESS_TOKEN"] = self.github_token

        log("🚀 Starting E2B Sandbox...")

        sandbox_kwargs = {"api_key": self.e2b_api_key}
        if envs:
            sandbox_kwargs["envs"] = envs
        mcp_config = self._build_mcp_config()
        if mcp_config:
            sandbox_kwargs["mcp"] = mcp_config

        with Sandbox.create(**sandbox_kwargs) as sandbox:
            log("📦 Sandbox ready. Installing tools...")

            # E2B MCP Gateway URL (if MCP servers are configured)
            if mcp_config and self.use_e2b_mcp_gateway:
                try:
                    self.mcp_gateway_url = sandbox.get_mcp_url()
                    log(f"🔗 E2B MCP Gateway URL: {self.mcp_gateway_url}")

                    # Get MCP Gateway token (if available)
                    try:
                        self.mcp_gateway_token = sandbox.get_mcp_token()
                        log("🔑 E2B MCP Gateway token acquired")
                    except Exception:
                        log("ℹ️ MCP Gateway token not required or not available")
                except Exception as e:
                    log(f"⚠️ MCP Gateway not available, falling back to stdio: {e}")
                    self.use_e2b_mcp_gateway = False

            sandbox.commands.run("pip install playwright PyGithub")
            sandbox.commands.run("playwright install chromium --with-deps")
            sandbox.commands.run("npm --version")

            repo_name = repo_url.replace("https://github.com/", "").replace(".git", "")
            log(f"🔍 Fetching PR info for {repo_name}...")

            # --- Step 1: Get PR Info via MCP ---
            pr_info = None
            if self.github_mcp_command:
                log("🧩 Using GitHub MCP to fetch PR metadata...")
                pr_info = self._fetch_pr_metadata_mcp(sandbox, repo_url, pr_number, logger=log)

            # Fallback to PyGithub script if MCP failed or not configured (though user prefers MCP)
            if not pr_info:
                log("⚠️ GitHub MCP failed or not configured. Fallback to PyGithub script.")
                pr_fetch_script = f"""
import os
import json
from github import Github

token = os.getenv("GITHUB_ACCESS_TOKEN")
g = Github(token)
repo = g.get_repo("{repo_name}")
pr_number = {pr_number if pr_number else 'None'}

if pr_number:
    pr = repo.get_pull(pr_number)
else:
    pulls = repo.get_pulls(state='open', sort='created', direction='desc')
    if pulls.totalCount > 0:
        pr = pulls[0]
    else:
        print(json.dumps({{"error": "No open PR found"}}))
        exit()

print(json.dumps({{
    "number": pr.number,
    "title": pr.title,
    "base_branch": pr.base.ref,
    "head_branch": pr.head.ref,
    "html_url": pr.html_url
}}))
"""
                sandbox.files.write("/home/user/get_pr_info.py", pr_fetch_script)
                pr_proc = sandbox.commands.run("python3 /home/user/get_pr_info.py")

                try:
                    pr_info = json.loads(self._to_text(pr_proc.stdout))
                    if "error" in pr_info:
                        log(f"❌ {pr_info['error']}")
                        return results
                except json.JSONDecodeError:
                    log("❌ Error parsing PR info")
                    return results

            base_branch = pr_info["base_branch"]
            head_branch = pr_info["head_branch"]
            current_pr_number = pr_info["number"]
            log(f"✅ Target PR #{current_pr_number}: {base_branch} <- {head_branch}")

            sandbox.commands.run(f"git clone {repo_url} /home/user/repo")
            sandbox.commands.run(
                f"cd /home/user/repo && git fetch origin {base_branch} && git fetch origin {head_branch}"
            )

            log("📝 Extracting Git diff...")
            # --- Step 2: Get Diff via MCP (preferred) or git command ---
            diff_text = ""
            mcp_files = None

            if self.github_mcp_command and self.github_mcp_tool:
                log("🧩 Using GitHub MCP to fetch PR files & diff...")
                mcp_files = self._github_mcp_fetch_files(sandbox, repo_url, current_pr_number, logger=log)
                if mcp_files:
                    # Compose diff from MCP file entries
                    diff_parts = []
                    for f in mcp_files:
                        fname = f.get("filename") or f.get("path")
                        patch = f.get("patch") or f.get("diff")
                        if fname and patch:
                            diff_parts.append(f"diff --git a/{fname} b/{fname}")
                            diff_parts.append(patch)
                    if diff_parts:
                        diff_text = "\n".join(diff_parts)
                        log(f"✅ Reconstructed diff from MCP ({len(diff_parts)//2} files)")

            if not diff_text:
                if not mcp_files and self.github_mcp_command:
                    log("⚠️ MCP did not return diffs. Falling back to local git diff.")
                diff_proc = sandbox.commands.run(
                    f"cd /home/user/repo && git diff origin/{base_branch}..origin/{head_branch}"
                )
                diff_text = self._to_text(diff_proc.stdout)

            results["git_diff"] = diff_text

            nav_targets = [{"label": "root", "path": "/", "selector": "body", "steps": []}]
            if results["git_diff"]:
                log("🧠 Analyzing diff to identify navigation targets...")
                # Pass already fetched mcp_files to avoid re-fetching
                if mcp_files:
                    # reuse logic inside but optimized?
                    # detect_navigation_targets calls _github_mcp_targets which calls _github_mcp_fetch_files again.
                    # optimization: pass mcp_files explicitly if possible, or just let it re-fetch (safest for now)
                    pass

                nav_targets = self.detect_navigation_targets(
                    results["git_diff"],
                    repo_url,
                    sandbox,
                    max_pages=max_pages,
                    pr_number=current_pr_number,
                    logger=log,
                )
                log(f"🎯 Target count for screenshots: {len(nav_targets)}")
            results["page_order"] = [target["label"] for target in nav_targets]

            def capture_branch_pages(branch_name, label, branch_key, targets):
                log(f"🔀 Processing {label}: {branch_name}")
                sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}")
                sandbox.commands.run("cd /home/user/repo && npm install")
                sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True)
                time.sleep(5)

                prepared_targets = []
                effective_targets = targets or [{"label": "root", "path": "/", "selector": "body", "steps": []}]
                for idx, target in enumerate(effective_targets):
                    prepared_targets.append(
                        {
                            "label": target.get("label") or f"page_{idx + 1}",
                            "safe_label": self._safe_label(target.get("label"), idx),
                            "path": target.get("path") or "/",
                            "selector": target.get("selector") or "body",
                            "steps": target.get("steps") or [],
                        }
                    )

                script_text = self._render_template(
                    "capture_targets.py.tpl",
                    targets_json=json.dumps(prepared_targets),
                    base_url="http://localhost:5173",
                )
                sandbox.files.write("/home/user/take_shot.py", script_text)
                proc = sandbox.commands.run("python3 /home/user/take_shot.py")
                stdout_text = self._to_text(proc.stdout)
                stderr_text = self._to_text(proc.stderr)
                if stderr_text:
                    log(f"ℹ️ Screenshot script stderr ({label}): {stderr_text}")
                if stdout_text:
                    log(f"ℹ️ Screenshot script stdout ({label}): {stdout_text}")

                branch_results = {}
                for target in prepared_targets:
                    orig_label = target["label"]
                    safe_label = target["safe_label"]
                    branch_results[orig_label] = {"full": None, "partial": None}
                    full_path = f"/home/user/{safe_label}_full.png"
                    partial_path = f"/home/user/{safe_label}_partial.png"
                    try:
                        full_bytes = sandbox.files.read(full_path, format="bytes")
                        branch_results[orig_label]["full"] = self._ensure_bytes(full_bytes)
                    except Exception:
                        pass
                    try:
                        partial_bytes = sandbox.files.read(partial_path, format="bytes")
                        branch_results[orig_label]["partial"] = self._ensure_bytes(partial_bytes)
                    except Exception:
                        pass

                sandbox.commands.run("pkill -f node")
                return branch_results

            base_pages = self._capture_branch_pages(
                sandbox, base_branch, "Main Branch", "base", nav_targets, logger=log
            )
            feature_pages = self._capture_branch_pages(
                sandbox, head_branch, "Feature Branch", "feature", nav_targets, logger=log
            )

            results["page_screenshots"] = {}
            for idx, target in enumerate(nav_targets):
                label_name = target["label"]
                results["page_screenshots"][label_name] = {
                    "safe_label": target.get("safe_label") or self._safe_label(label_name, idx),
                    "base": base_pages.get(label_name, {}),
                    "feature": feature_pages.get(label_name, {}),
                }

            first_label = nav_targets[0]["label"] if nav_targets else None
            if first_label:
                results["main_screenshot"] = results["page_screenshots"][first_label]["base"].get("full")
                results["feature_screenshot"] = results["page_screenshots"][first_label]["feature"].get("full")
                results["partial_screenshot"] = results["page_screenshots"][first_label]["feature"].get("partial")

            if skip_ai:
                log("ℹ️ AI analysis skipped by mode setting.")
                return results

            if self.groq_client and results["main_screenshot"] and results["feature_screenshot"]:
                log("🧠 Generating final report with Groq Vision...")
                try:
                    report = self.generate_analysis_report(
                        results["main_screenshot"], results["feature_screenshot"], results["git_diff"]
                    )
                    results["ai_report"] = report
                except Exception as exc:  # pragma: no cover
                    log(f"⚠️ AI analysis failed: {exc}")
                    results["ai_report"] = "Analysis failed."

            if results["ai_report"] and notify_github:
                if not self.github_token:
                    log("⚠️ GitHub token missing. Skipping notification.")
                else:
                    log("📤 Uploading assets and commenting on GitHub PR...")
                    sandbox.files.write("/home/user/report.txt", results["ai_report"])
                    if results["main_screenshot"]:
                        sandbox.files.write("/home/user/before.png", results["main_screenshot"])
                    if results["feature_screenshot"]:
                        sandbox.files.write("/home/user/after.png", results["feature_screenshot"])
                    if results["partial_screenshot"]:
                        sandbox.files.write("/home/user/diff_focus.png", results["partial_screenshot"])

                    screens_dir = "/home/user/screens"
                    sandbox.commands.run(f"rm -rf {screens_dir} && mkdir -p {screens_dir}")
                    manifest_entries = []
                    ordered_labels = results.get("page_order") or list(results["page_screenshots"].keys())
                    for idx, label_name in enumerate(ordered_labels):
                        page_data = results["page_screenshots"].get(label_name)
                        if not page_data:
                            continue
                        safe_label = page_data.get("safe_label") or self._safe_label(label_name, idx)
                        manifest_entries.append({"label": label_name, "safe_label": safe_label})
                        page_dir = f"{screens_dir}/{safe_label}"
                        sandbox.commands.run(f"mkdir -p {page_dir}")
                        base_full = page_data.get("base", {}).get("full")
                        if base_full:
                            sandbox.files.write(f"{page_dir}/base_full.png", base_full)
                        feature_full = page_data.get("feature", {}).get("full")
                        if feature_full:
                            sandbox.files.write(f"{page_dir}/feature_full.png", feature_full)
                        feature_focus = page_data.get("feature", {}).get("partial")
                        if feature_focus:
                            sandbox.files.write(f"{page_dir}/feature_focus.png", feature_focus)
                    sandbox.files.write(f"{screens_dir}/manifest.json", json.dumps(manifest_entries).encode("utf-8"))

                    github_script = self._render_template(
                        "upload_github_report.py.tpl",
                        repo_name=repo_name,
                        pr_number=current_pr_number,
                    )
                    sandbox.files.write("/home/user/post_gh.py", github_script)
                    gh_proc = sandbox.commands.run("python3 /home/user/post_gh.py")
                    if "GITHUB_SUCCESS" in self._to_text(gh_proc.stdout):
                        log("✅ GitHub comment posted with artifacts.")
                    else:
                        log(f"⚠️ GitHub post failed: {self._to_text(gh_proc.stdout)} {self._to_text(gh_proc.stderr)}")
            elif results["ai_report"] and not notify_github:
                log("ℹ️ Skipping GitHub notification per mode setting.")

            log("✅ Process finished.")

        return results

    def detect_changed_selector(self, diff_text):
        """
        Infer one CSS selector that most likely changed based on the diff.
        """
        if not self.groq_client or not diff_text:
            return "body"

        prompt = f"""
Analyze the following Git diff and return exactly one CSS selector for the UI element most likely impacted.

Diff:
{diff_text[:1500]}

Respond with JSON only, e.g. {{ "selector": ".class-name" }}
"""
        try:
            completion = self.groq_client.chat.completions.create(
                model=self.selector_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            res = json.loads(completion.choices[0].message.content)
            return res.get("selector", "body")
        except Exception:
            return "body"

    def generate_analysis_report(self, img_before, img_after, diff_text):
        if not self.groq_client:
            raise ValueError("Groq client is not configured.")

        b64_before = base64.b64encode(img_before).decode("utf-8")
        b64_after = base64.b64encode(img_after).decode("utf-8")
        diff_excerpt = (diff_text or "")[: self.diff_prompt_chars]
        if diff_text and len(diff_text) > self.diff_prompt_chars:
            diff_excerpt = f"{diff_excerpt}\n... (diff truncated)"

        prompt = f"""
Use the Git diff and the before/after screenshots to explain the change.

Git Diff:
{diff_excerpt}

## Output Format (Markdown)
- **Summary**: single sentence
- **UI Changes**: concrete visual differences
- **Code Changes**: technical reasoning from the diff
- **Notes for Reviewers**: any caveats or follow-ups
"""

        completion = self.groq_client.chat.completions.create(
            model=self.vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_before}"}},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_after}"}},
                    ],
                }
            ],
            max_tokens=1024,
        )
        return completion.choices[0].message.content

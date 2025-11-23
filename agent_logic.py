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
        # Updated models: llama3-8b-8192 has been decommissioned
        self.selector_model = os.getenv("GROQ_SELECTOR_MODEL", "llama-3.3-70b-versatile")
        self.target_model = os.getenv("GROQ_TARGET_MODEL", "llama-3.3-70b-versatile")
        self.vision_model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        self.diff_prompt_chars = int(os.getenv("GIT_DIFF_PROMPT_CHARS", "6000"))

        # E2B MCP Gateway settings (will be set during sandbox creation)
        self.mcp_gateway_url = None
        self.mcp_gateway_token = None
        self.mcp_session_id = None  # MCP Session ID from initialize response
        self.use_e2b_mcp_gateway = os.getenv("USE_E2B_MCP_GATEWAY", "true").lower() == "true"
        self.skip_mcp_initialization = os.getenv("SKIP_MCP_INITIALIZATION", "false").lower() == "true"
        self._mcp_initialized = False

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
        proc = sandbox.commands.run(f"{self.sandbox_python_cmd} /home/user/run_mcp.py", timeout=60)
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

        E2B MCP Gateway may auto-manage server lifecycle, so we attempt calls
        even if explicit initialization hasn't completed.
        """
        if not self.mcp_gateway_url or not mcp_requests:
            print(
                f"[Agent] ⚠️ Cannot make MCP HTTP call: gateway_url={self.mcp_gateway_url}, requests={len(mcp_requests) if mcp_requests else 0}"
            )
            return None

        # Validate MCP Gateway URL format
        if not isinstance(self.mcp_gateway_url, str) or len(self.mcp_gateway_url.strip()) == 0:
            print(f"[Agent] ⚠️ Invalid MCP Gateway URL (empty or not a string): {repr(self.mcp_gateway_url)}")
            return None

        if not self.mcp_gateway_url.startswith(("http://", "https://")):
            print(f"[Agent] ⚠️ Invalid MCP Gateway URL (missing protocol): {self.mcp_gateway_url}")
            return None

        # Log warning if not initialized, but still attempt the call
        # E2B may not require explicit initialization
        if not self._mcp_initialized:
            print("[Agent] ℹ️ Attempting MCP call without explicit initialization (E2B may auto-manage)")

        print(f"[Agent] 🔗 Using MCP Gateway URL: {self.mcp_gateway_url}")

        headers = {"Content-Type": "application/json"}
        if self.mcp_gateway_token:
            headers["Authorization"] = f"Bearer {self.mcp_gateway_token}"
        if self.mcp_session_id:
            headers["Mcp-Session-Id"] = self.mcp_session_id

        results = []
        for req in mcp_requests:
            try:
                method = req.get("method", "unknown")
                print(f"[Agent] 🔄 Sending MCP request: {method}")

                response = requests.post(self.mcp_gateway_url, json=req, headers=headers, timeout=30)

                print(f"[Agent] 📥 MCP response status for {method}: {response.status_code}")

                response.raise_for_status()

                # Try to parse JSON response (handle both plain JSON and SSE format)
                try:
                    # First try direct JSON parsing
                    result = response.json()
                    if result.get("error"):
                        error_detail = result["error"]
                        error_msg = error_detail.get("message", str(error_detail))
                        error_code = error_detail.get("code", "N/A")
                        print(f"[Agent] ⚠️ MCP error for {method} [code: {error_code}]: {error_msg}")
                    results.append(result)
                except json.JSONDecodeError:
                    # Try parsing as SSE format
                    parsed = self._parse_sse_response(response.text)
                    if parsed:
                        results.append(parsed)
                    else:
                        error_msg = f"JSON parse error for {method}. Response text: {response.text[:300]}"
                        print(f"[Agent] ⚠️ {error_msg}")
                        results.append({"error": {"message": error_msg}})

            except requests.exceptions.RequestException as e:
                error_msg = f"HTTP error for {req.get('method', 'unknown')}: {str(e)}"
                print(f"[Agent] ⚠️ {error_msg}")
                print(f"[Agent] 🔍 Request URL: {self.mcp_gateway_url}")
                print(f"[Agent] 🔍 Exception type: {type(e).__name__}")
                print(f"[Agent] 🔍 Exception args: {e.args}")
                if hasattr(e, "response") and e.response is not None:
                    print(f"[Agent] 📋 Response body: {e.response.text[:300]}")
                results.append({"error": {"message": error_msg}})
            except OSError as e:
                error_msg = f"OS error for {req.get('method', 'unknown')}: {str(e)}"
                print(f"[Agent] ⚠️ {error_msg}")
                print(f"[Agent] 🔍 Request URL: {self.mcp_gateway_url}")
                print(f"[Agent] 🔍 OS error code: {e.errno}")
                print(f"[Agent] 🔍 OS error message: {e.strerror}")
                results.append({"error": {"message": error_msg}})
            except Exception as e:
                error_msg = f"Unexpected error for {req.get('method', 'unknown')}: {str(e)}"
                print(f"[Agent] ⚠️ {error_msg}")
                print(f"[Agent] 🔍 Request URL: {self.mcp_gateway_url}")
                print(f"[Agent] 🔍 Exception type: {type(e).__name__}")
                results.append({"error": {"message": error_msg}})

        return results if results else None

    def _parse_sse_response(self, sse_text):
        """Parse Server-Sent Events (SSE) format response"""
        try:
            lines = sse_text.strip().split("\n")
            for line in lines:
                if line.startswith("data: "):
                    data_str = line[6:]  # Remove 'data: ' prefix
                    return json.loads(data_str)
            return None
        except (json.JSONDecodeError, Exception):
            return None

    def _initialize_mcp_session(self):
        """
        Initialize MCP session following the MCP protocol.
        Reference: https://modelcontextprotocol.io/docs/concepts/lifecycle

        E2B MCP Gateway may not require explicit initialization if it auto-manages servers.
        This method attempts initialization but doesn't fail if the gateway is already ready.
        """
        if not self.mcp_gateway_url:
            print("[Agent] ⚠️ MCP Gateway URL not set, cannot initialize")
            return False

        headers = {"Content-Type": "application/json"}
        if self.mcp_gateway_token:
            headers["Authorization"] = f"Bearer {self.mcp_gateway_token}"

        try:
            print("[Agent] 🔄 Initializing MCP session...")

            # Step 1: Send initialize request
            init_request = {
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 1,
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                    "clientInfo": {"name": "e2b-hackathon-agent", "version": "1.0.0"},
                },
            }

            response = requests.post(self.mcp_gateway_url, json=init_request, headers=headers, timeout=30)

            # E2B MCP Gateway might return 200 even if initialization isn't needed
            # Check response content and extract session ID from headers
            response_text = response.text
            print(f"[Agent] 🔍 Initialize response status: {response.status_code}")
            print(f"[Agent] 🔍 Initialize response headers: {dict(response.headers)}")
            print(f"[Agent] 🔍 Initialize response body (first 300 chars): {response_text[:300]}")

            # Extract MCP-Session-Id from response headers if present
            session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
            if session_id:
                self.mcp_session_id = session_id
                print(f"[Agent] 🔑 MCP Session ID acquired: {session_id[:20]}...")

            response.raise_for_status()

            # Parse response (handle both plain JSON and SSE format)
            init_result = None
            try:
                init_result = response.json()
            except json.JSONDecodeError:
                init_result = self._parse_sse_response(response_text)

            if not init_result:
                print("[Agent] ⚠️ MCP initialize returned no parseable result")
                # E2B might not need initialization - try to proceed anyway
                self._mcp_initialized = True
                return True

            if init_result.get("error"):
                error_detail = init_result.get("error")
                error_code = error_detail.get("code", "N/A") if isinstance(error_detail, dict) else "N/A"
                error_msg = (
                    error_detail.get("message", str(error_detail))
                    if isinstance(error_detail, dict)
                    else str(error_detail)
                )
                print(f"[Agent] ⚠️ MCP initialize error [code: {error_code}]: {error_msg}")

                # If error indicates already initialized or initialization not needed, consider it success
                if "already initialized" in str(error_msg).lower() or error_code == -32600:
                    print("[Agent] ℹ️ MCP may already be initialized or doesn't require explicit initialization")
                    self._mcp_initialized = True
                    return True
                return False

            print(f"[Agent] ✅ MCP initialize successful")
            result_summary = json.dumps(init_result.get("result", {}), indent=2)[:300]
            print(f"[Agent] 📋 Server capabilities: {result_summary}")

            # Step 2: Send initialized notification with params
            initialized_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},  # Empty params as per MCP spec
            }

            try:
                response = requests.post(
                    self.mcp_gateway_url, json=initialized_notification, headers=headers, timeout=10
                )
                print(f"[Agent] 🔍 Initialized notification response: {response.status_code}")
                # Don't fail on notification errors - it's fire-and-forget
                if response.status_code != 200:
                    print(f"[Agent] ℹ️ Notification response: {response.text[:200]}")
                else:
                    print("[Agent] ✅ MCP initialized notification sent successfully")
            except Exception as e:
                print(f"[Agent] ℹ️ MCP initialized notification response: {e}")
                # Continue anyway - notification is optional

            # Wait a moment for servers to fully initialize
            time.sleep(1)

            self._mcp_initialized = True
            print("[Agent] ✅ MCP session initialization complete")
            return True

        except requests.exceptions.RequestException as e:
            print(f"[Agent] ⚠️ MCP session initialization network error: {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"[Agent] 📋 Error response: {e.response.text[:500]}")
            return False
        except Exception as e:
            print(f"[Agent] ⚠️ MCP session initialization failed: {e}")
            import traceback

            print(f"[Agent] 📋 Traceback: {traceback.format_exc()}")
            return False

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
                logger(f"✅ GitHub MCP から {len(mcp_targets)} 件のターゲットを取得しました。")
                return mcp_targets
            logger(
                "⚠️ GitHub MCP からターゲット情報への変換に失敗したため、ヒューリスティック解析にフォールバックします。"
            )
            logger("   （注：ファイル情報は取得できていても、ターゲット情報への変換で問題が発生した可能性があります）")
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

        # List available tools first
        available_tools = self._github_mcp_list_tools(sandbox, logger=logger)

        self._github_mcp_list_resources(sandbox, logger=logger)
        file_payloads = self._github_mcp_fetch_files(
            sandbox, repo_url, pr_number, logger=logger, available_tools=available_tools
        )
        if not file_payloads:
            logger("⚠️ GitHub MCP がターゲット候補となるファイル情報を返しませんでした。")
            return None

        logger(f"🔍 取得したファイル数: {len(file_payloads)}")
        targets = []
        for idx, file_entry in enumerate(file_payloads):
            file_path = file_entry.get("path") or file_entry.get("file") or file_entry.get("filename")
            if not file_path:
                logger(f"   ⚠️ File {idx+1}: ファイルパスが取得できませんでした。Keys: {list(file_entry.keys())}")
                continue

            logger(f"   📄 File {idx+1}: {file_path}")
            diff_text = file_entry.get("patch") or file_entry.get("diff") or ""
            diff_lines = diff_text.splitlines()
            logger(f"      Diff lines: {len(diff_lines)}")

            file_content = file_entry.get("content") or file_entry.get("text") or ""
            if not file_content:
                logger(f"      ファイル内容がレスポンスに含まれていないため、個別に読み込みます...")
                file_content = self._github_mcp_read_file(
                    sandbox, file_path, repo_url=repo_url, pr_number=pr_number, logger=logger
                )

            content_len = len(file_content) if file_content else 0
            logger(f"      Content length: {content_len}")
            if file_content and content_len < 200:
                logger(f"      🔍 Content (first 200 chars): {file_content[:200]}")

            groq_targets = self._groq_targets_from_file(file_path, diff_lines, file_content, logger=logger)
            if groq_targets:
                logger(f"      ✅ Groq から {len(groq_targets)} 件のターゲットを取得")
            else:
                logger(f"      ⚠️ Groq がターゲットを返しませんでした（Groq未設定またはエラー）")

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

        if not targets:
            logger("⚠️ ファイル情報は取得できましたが、ターゲット情報への変換に失敗しました。")
            logger("   原因候補: Groqクライアント未設定、ファイルがUI関連ではない、APIエラーなど")

        return targets or None

    def _get_mcp_server_url(self, server_name):
        """
        Get the MCP server URL for a specific server.
        E2B may expose each MCP server at a different endpoint.
        Try: base_url, base_url/server_name, base_url?server=server_name
        """
        if not self.mcp_gateway_url:
            return None

        # Check if E2B uses path-based routing (e.g., /github, /playwright)
        # This is a common pattern for multi-server gateways
        if server_name:
            # Try appending server name to path
            base = self.mcp_gateway_url.rstrip("/")
            return f"{base}/{server_name}"

        return self.mcp_gateway_url

    def _github_mcp_list_tools(self, sandbox, logger=None):
        """List available tools from GitHub MCP server"""
        if not self.use_e2b_mcp_gateway or not self.mcp_gateway_url:
            return []

        logger = logger or (lambda *_: None)

        # E2B MCP Gateway uses a single endpoint for all servers
        # Try base URL only (server routing is handled internally)
        logger(f"🔍 Listing tools from MCP Gateway: {self.mcp_gateway_url}")
        mcp_requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "id": 999,
                "params": {},
            }
        ]
        try:
            response = self._call_mcp(sandbox, None, mcp_requests, None)

            if response and isinstance(response, list):
                entry = response[0]
                if entry.get("error"):
                    error_msg = entry["error"].get("message", "")
                    logger(f"   ⚠️ tools/list error: {error_msg}")
                    return []

                tools = entry.get("result", {}).get("tools", [])
                if tools:
                    tool_names = [t.get("name") for t in tools if t.get("name")]
                    logger(f"   ✅ Found {len(tools)} tools:")
                    for tool in tools:
                        logger(f"      - {tool.get('name')}")
                    return tool_names
                else:
                    logger(f"   ℹ️ No tools in response. Full response:")
                    logger(f"      {json.dumps(entry, indent=2)[:500]}")
        except Exception as exc:
            logger(f"   ⚠️ tools/list failed: {exc}")

        return []

    def _github_mcp_list_resources(self, sandbox, logger=None):
        # Skip if not using E2B MCP Gateway
        if not self.use_e2b_mcp_gateway or not self.mcp_gateway_url:
            return

        logger = logger or (lambda *_: None)

        # E2B MCP Gateway uses a single endpoint
        logger(f"🔍 Listing resources from MCP Gateway")
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

            if response:
                entry = response[0]
                if entry.get("error"):
                    error_msg = entry["error"].get("message", "")
                    logger(f"   ⚠️ resources/list error: {error_msg}")
                    return
                logger(f"   📚 Resources:")
                logger(f"      {json.dumps(entry.get('result', {}), indent=2)[:300]}")
        except Exception as exc:
            logger(f"   ⚠️ resources/list failed: {exc}")

    def _github_mcp_fetch_files(self, sandbox, repo_url, pr_number, logger=None, available_tools=None):
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

        if not pr_number:
            logger("⚠️ PR番号が指定されていないため、ファイル取得をスキップします")
            return None

        owner, repo = self._parse_repo_info(repo_url)

        # Use github-official-pull_request_read with method="get_files"
        tool_name = "github-official-pull_request_read"
        logger(f"🔍 Fetching PR files with tool: {tool_name}")

        mcp_requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 77,
                "params": {
                    "name": tool_name,
                    "arguments": {
                        "method": "get_files",  # Get list of changed files
                        "owner": owner,
                        "repo": repo,
                        "pullNumber": float(pr_number),  # Convert to float as expected by GitHub MCP
                        "perPage": 100,  # Get as many files as possible
                    },
                },
            }
        ]

        try:
            response = self._call_mcp(sandbox, None, mcp_requests, None)

            if not response:
                logger(f"   ⚠️ No response")
                return None

            entry = response[0]
            if entry.get("error"):
                error_msg = entry["error"].get("message", "")
                logger(f"   ⚠️ Error: {error_msg}")
                return None

            logger(f"   ✅ Got response")

            # Debug: レスポンス全体をログ出力
            logger(f"   🔍 Response detail: {json.dumps(entry, indent=2)[:800]}")

            # Parse response
            contents = entry.get("result", {}).get("content", [])
            if not contents:
                logger(
                    f"   ⚠️ Response has no content array. Full result: {json.dumps(entry.get('result', {}), indent=2)[:500]}"
                )

            for item in contents:
                logger(f"   🔍 Processing content item type: {item.get('type')}")
                data = None
                if item.get("type") == "json" and item.get("data"):
                    data = item["data"]
                elif item.get("type") == "text" and item.get("text"):
                    try:
                        data = json.loads(item["text"])
                    except json.JSONDecodeError:
                        logger(f"   ⚠️ Failed to parse text as JSON: {item.get('text')[:200]}")
                        continue

                if data:
                    logger(f"   🔍 Parsed data type: {type(data).__name__}")
                    if isinstance(data, dict):
                        logger(f"   🔍 Data keys: {list(data.keys())}")
                    elif isinstance(data, list):
                        logger(f"   🔍 Data is list with {len(data)} items")
                        if data:
                            logger(
                                f"   🔍 First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'Not a dict'}"
                            )

                    # The response might be an array of files or an object containing files
                    files = None
                    if isinstance(data, list):
                        files = data
                    elif isinstance(data, dict):
                        files = data.get("files") or data.get("data")

                    if isinstance(files, list) and files:
                        logger(f"   ✅ {len(files)} 件のファイル情報を取得しました")
                        # Debug: ファイルの詳細情報
                        for idx, f in enumerate(files[:3]):  # 最初の3件のみ
                            logger(
                                f"   🔍 File {idx+1} keys: {list(f.keys()) if isinstance(f, dict) else 'Not a dict'}"
                            )
                        return files
                    else:
                        logger(f"   ⚠️ Files extraction failed. files type: {type(files).__name__ if files else 'None'}")

        except Exception as exc:
            logger(f"   ⚠️ Failed: {exc}")
            import traceback

            logger(f"   📋 Traceback: {traceback.format_exc()[:300]}")

        logger("⚠️ GitHub MCP: PRファイル情報を取得できませんでした。")
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

    def _fetch_pr_metadata_mcp(self, sandbox, repo_url, pr_number, logger=None):
        """
        Fetch PR metadata (base_branch, head_branch, etc.) using GitHub MCP.
        """
        if not self.use_e2b_mcp_gateway and not self.github_mcp_command:
            return None
        if self.use_e2b_mcp_gateway and not self.mcp_gateway_url:
            return None

        logger = logger or (lambda *_: None)
        owner, repo = self._parse_repo_info(repo_url)

        # If pr_number is None, try to get the latest open PR
        if pr_number is None:
            logger("🔍 PR番号が指定されていないため、最新のオープンPRを取得します...")
            list_tool = "github-official-list_pull_requests"
            list_requests = [
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 87,
                    "params": {
                        "name": list_tool,
                        "arguments": {
                            "owner": owner,
                            "repo": repo,
                            "state": "open",
                            "perPage": 1.0,
                        },
                    },
                }
            ]

            try:
                response = self._call_mcp(sandbox, None, list_requests, None)
                if response and isinstance(response, list):
                    entry = response[0]
                    if not entry.get("error"):
                        contents = entry.get("result", {}).get("content", [])
                        for item in contents:
                            if item.get("type") == "text" and item.get("text"):
                                try:
                                    data = json.loads(item["text"])
                                    if isinstance(data, list) and len(data) > 0:
                                        pr_number = data[0].get("number")
                                        logger(f"   ✅ 最新のPR #{pr_number} を見つけました")
                                        break
                                except json.JSONDecodeError:
                                    pass
            except Exception as exc:
                logger(f"   ⚠️ 最新PR取得に失敗: {exc}")

            if pr_number is None:
                logger("   ⚠️ オープンなPRが見つかりませんでした")
                return None

        # E2B MCP Gateway uses tools with format: github-official-{tool_name}
        # The pull_request_read tool requires a "method" parameter

        tool_name = "github-official-pull_request_read"
        logger(f"🔍 Fetching PR metadata with tool: {tool_name}")

        mcp_requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 88,
                "params": {
                    "name": tool_name,
                    "arguments": {
                        "method": "get",  # Required: get PR details
                        "owner": owner,
                        "repo": repo,
                        "pullNumber": float(pr_number),  # Convert to float as expected by GitHub MCP
                    },
                },
            }
        ]

        try:
            response = self._call_mcp(sandbox, None, mcp_requests, None)

            if not response:
                logger(f"   ⚠️ No response")
                return None

            entry = response[0]
            if entry.get("error"):
                error_detail = entry.get("error")
                logger(f"   ⚠️ Error response: {json.dumps(error_detail, indent=2)[:200]}")
                return None

            # Log successful response for debugging
            logger(f"   ✅ Got PR metadata response")

            # Debug: レスポンス全体をログ出力
            logger(f"   🔍 Response detail: {json.dumps(entry, indent=2)[:500]}")

            # Parse response
            contents = entry.get("result", {}).get("content", [])
            if not contents:
                logger(f"   ⚠️ Response has no content array")

            for item in contents:
                data = None
                if item.get("type") == "json" and item.get("data"):
                    data = item["data"]
                elif item.get("type") == "text" and item.get("text"):
                    try:
                        data = json.loads(item["text"])
                    except json.JSONDecodeError:
                        logger(f"   ⚠️ Failed to parse text content as JSON: {item.get('text')[:200]}")
                        continue

                if data:
                    logger(f"   🔍 Parsed data keys: {list(data.keys())}")
                    logger(f"   🔍 Data snippet: {json.dumps(data, indent=2)[:500]}")

                    # Extract PR metadata
                    pr_info = {
                        "number": data.get("number") or pr_number,
                        "title": data.get("title", ""),
                        "base_branch": (
                            data.get("base", {}).get("ref") if isinstance(data.get("base"), dict) else None
                        ),
                        "head_branch": (
                            data.get("head", {}).get("ref") if isinstance(data.get("head"), dict) else None
                        ),
                        "html_url": data.get("html_url", ""),
                    }

                    logger(
                        f"   🔍 Extracted base_branch: {pr_info['base_branch']}, head_branch: {pr_info['head_branch']}"
                    )

                    # Validate required fields
                    if pr_info["base_branch"] and pr_info["head_branch"]:
                        logger(
                            f"✅ GitHub MCP からPRメタデータを取得: {pr_info['base_branch']} <- {pr_info['head_branch']}"
                        )
                        return pr_info
                    else:
                        logger(f"   ⚠️ base_branchまたはhead_branchが取得できませんでした")

        except Exception as exc:
            logger(f"   ⚠️ Failed: {exc}")
            import traceback

            logger(f"   📋 Traceback: {traceback.format_exc()[:300]}")

        logger("⚠️ GitHub MCP: PRメタデータを取得できませんでした")
        return None

    def _github_mcp_read_file(self, sandbox, file_path, repo_url=None, pr_number=None, logger=None):
        # Check if MCP is available (E2B Gateway or stdio)
        if not self.use_e2b_mcp_gateway and not self.github_mcp_command:
            return None
        if self.use_e2b_mcp_gateway and not self.mcp_gateway_url:
            return None

        logger = logger or (lambda *_: None)
        owner, repo = self._parse_repo_info(repo_url)

        # E2B MCP Gateway uses github-official-get_file_contents
        tool_name = "github-official-get_file_contents" if self.use_e2b_mcp_gateway else "github_read_file"

        requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": tool_name,
                    "arguments": {
                        "owner": owner,
                        "repo": repo,
                        "path": file_path,
                        # "ref": ... # If we need specific branch
                    },
                },
            }
        ]
        env_updates = {}
        if self.mcp_github_pat:
            env_updates["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.mcp_github_pat
        response = self._call_mcp(sandbox, self.github_mcp_command, requests, env_updates)
        if not response:
            logger(f"      🔍 No response from MCP for file: {file_path}")
            return None
        entry = response[0] if isinstance(response, list) else response

        # Debug: レスポンス全体をログ出力（最初の500文字）
        logger(f"      🔍 Full response: {json.dumps(entry, indent=2)[:500]}")

        if entry.get("error"):
            error_msg = entry.get("error", {}).get("message", "Unknown error")
            logger(f"      ⚠️ MCP error reading file: {error_msg}")
            return None
        contents = entry.get("result", {}).get("content", [])
        logger(f"      🔍 Response has {len(contents)} content items")

        for item in contents:
            item_type = item.get("type")
            logger(f"      🔍 Processing content type: {item_type}")

            # GitHub MCP returns file content in type: "resource" with text field
            if item_type == "resource" and item.get("resource"):
                resource = item["resource"]
                if resource.get("text"):
                    text_content = resource["text"]
                    logger(f"      ✅ Got resource text content: {len(text_content)} chars")
                    return text_content

            # Fallback to plain text type
            if item_type == "text" and item.get("text"):
                text_content = item["text"]
                # Skip status messages like "successfully downloaded..."
                if not text_content.startswith("successfully"):
                    logger(f"      ✅ Got text content: {len(text_content)} chars")
                    return text_content
                else:
                    logger(f"      ⏭️ Skipping status message: {text_content[:50]}")

            if item_type == "json" and item.get("data"):
                # GitHub returns base64-encoded content
                data = item["data"]
                logger(f"      🔍 JSON data keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")

                if isinstance(data, dict) and data.get("content"):
                    import base64

                    try:
                        decoded = base64.b64decode(data["content"]).decode("utf-8")
                        logger(f"      ✅ Decoded base64 content: {len(decoded)} chars")
                        return decoded
                    except Exception as e:
                        logger(f"      ⚠️ Failed to decode base64: {e}")
                        pass

                # Try returning as string
                str_data = str(data)
                logger(f"      ⚠️ Returning data as string: {len(str_data)} chars")
                return str_data

        logger(f"      ⚠️ No usable content found in response")
        return None

    def _groq_targets_from_file(self, file_path, diff_lines, file_content, logger=None):
        logger = logger or (lambda *_: None)

        if not self.groq_client:
            logger(f"      ⚠️ Groq client not configured")
            return None

        diff_excerpt = "\n".join(diff_lines or [])[:2000]
        file_excerpt = (file_content or "")[:2000]

        logger(
            f"      🔍 Calling Groq with diff_excerpt: {len(diff_excerpt)} chars, file_excerpt: {len(file_excerpt)} chars"
        )

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
            logger(f"      🔍 Groq response: {content[:200]}")

            data = json.loads(content)
            # Groqがラップされた辞書形式で返す場合に対応
            if isinstance(data, dict):
                if "targets" in data:
                    data = data["targets"]
                elif "impactedElements" in data:
                    data = data["impactedElements"]

            if isinstance(data, list):
                logger(f"      ✅ Groq returned {len(data)} targets")
                return data
            else:
                logger(f"      ⚠️ Groq response is not a list: {type(data).__name__}")
                logger(f"      📋 Response keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
        except Exception as e:
            logger(f"      ⚠️ Groq error: {e}")
            import traceback

            logger(f"      📋 Traceback: {traceback.format_exc()[:300]}")
            return None
        return None

    def _capture_branch_pages(self, sandbox, branch_name, label, branch_key, targets, logger=None):
        logger = logger or (lambda *_: None)

        # Playwright MCP is disabled - always use local Playwright for more reliable screenshots
        # MCP has issues with session initialization and tool availability
        logger(f"🎥 ローカル Playwright で {label} のスクリーンショットを取得します。")
        return self._capture_branch_pages_local(sandbox, branch_name, label, branch_key, targets, logger=logger)

    def _capture_branch_pages_local(self, sandbox, branch_name, label, branch_key, targets, logger=None):
        logger = logger or (lambda *_: None)
        log_prefix = f"{label}: {branch_name}"
        target_count = len(targets) if targets else 0
        prepared_targets = self._prepare_targets(targets, target_count or 1)
        sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}", timeout=60)
        sandbox.commands.run("cd /home/user/repo && npm install", timeout=300)
        sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True, timeout=30)
        self._wait_for_dev_server(sandbox, logger=logger)

        targets_literal = json.dumps(prepared_targets)
        script_text = self._render_template(
            "capture_targets.py.tpl",
            targets_json=targets_literal,
            base_url=self.playwright_base_url,
        )
        sandbox.files.write("/home/user/take_shot.py", script_text)
        proc = sandbox.commands.run("python3 /home/user/take_shot.py", timeout=300)
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

        sandbox.commands.run("pkill -f node", timeout=10)
        return branch_results

    def _capture_branch_pages_code_mcp(self, sandbox, branch_name, label, branch_key, targets, logger=None):
        logger = logger or (lambda *_: None)
        log_prefix = f"{label}: {branch_name}"
        target_count = len(targets) if targets else 0
        prepared_targets = self._prepare_targets(targets, target_count or 1)
        sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}", timeout=60)

        logger(f"📦 Installing dependencies for {branch_name}...")
        install_proc = sandbox.commands.run("cd /home/user/repo && npm install", timeout=300)
        if install_proc.exit_code != 0:
            logger(f"❌ npm install failed: {self._to_text(install_proc.stderr)}")
            return {}

        logger(f"🚀 Starting dev server for {branch_name}...")

        # Check if dev script exists in package.json
        check_script = sandbox.commands.run(
            "cd /home/user/repo && cat package.json | grep -A 5 '\"scripts\"'", timeout=10
        )
        logger(f"📋 package.json scripts: {self._to_text(check_script.stdout)[:200]}")

        # Start dev server in background
        sandbox.commands.run(
            "cd /home/user/repo && npm run dev -- --host > /home/user/server.log 2>&1 &", background=True, timeout=30
        )

        # Wait a bit for server to start
        time.sleep(3)

        # Check if server process is running
        ps_check = sandbox.commands.run("ps aux | grep -v grep | grep 'npm\\|node\\|vite'", timeout=10)
        if ps_check.exit_code == 0:
            logger(f"✅ Dev server process is running")
        else:
            logger(f"⚠️ Dev server process not found, checking logs...")
            log_check = sandbox.commands.run("tail -n 50 /home/user/server.log", timeout=10)
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
                    log_proc = sandbox.commands.run("tail -n 200 /home/user/server.log", timeout=10)
                    full_log = self._to_text(log_proc.stdout)
                    if full_log:
                        logger(f"   Server Log (last 500 chars): {full_log[-500:]}")
        finally:
            sandbox.commands.run("pkill -f node", timeout=10)
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
                proc = sandbox.commands.run(command, timeout=10)
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
                            "tail -n 10 /home/user/server.log 2>/dev/null || echo 'No log file'", timeout=10
                        )
                        log_output = self._to_text(log_check.stdout).strip()
                        if log_output and log_output != "No log file":
                            logger(f"📄 Server log (last 10 lines): {log_output[-300:]}")

            except Exception as e:
                logger(f"ℹ️ Dev server check failed (attempt {attempt}): {e}")

            time.sleep(3)  # Increased from 2 to 3 seconds

        logger(f"⚠️ Dev server readiness check timed out after {self.playwright_server_wait_seconds} 秒: {health_url}")

        # Final log check
        final_log = sandbox.commands.run(
            "tail -n 50 /home/user/server.log 2>/dev/null || echo 'No log file'", timeout=10
        )
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
                    self._mcp_initialized = False  # Reset initialization flag for new sandbox

                    # Validate and log MCP Gateway URL
                    if not self.mcp_gateway_url:
                        log("⚠️ MCP Gateway URL is empty!")
                        raise ValueError("MCP Gateway URL is empty")

                    log(f"🔗 E2B MCP Gateway URL: {self.mcp_gateway_url}")
                    log(f"   URL type: {type(self.mcp_gateway_url)}, length: {len(str(self.mcp_gateway_url))}")

                    # Validate URL format
                    if not isinstance(self.mcp_gateway_url, str):
                        log(f"⚠️ MCP Gateway URL is not a string: {type(self.mcp_gateway_url)}")
                        raise ValueError(f"MCP Gateway URL must be a string, got {type(self.mcp_gateway_url)}")

                    if not self.mcp_gateway_url.startswith(("http://", "https://")):
                        log(f"⚠️ MCP Gateway URL missing protocol: {self.mcp_gateway_url}")
                        raise ValueError(
                            f"MCP Gateway URL must start with http:// or https://, got: {self.mcp_gateway_url}"
                        )

                    # Get MCP Gateway token (if available)
                    try:
                        self.mcp_gateway_token = sandbox.get_mcp_token()
                        log("🔑 E2B MCP Gateway token acquired")
                    except Exception as token_error:
                        log(f"ℹ️ MCP Gateway token not required or not available: {token_error}")

                    # Initialize MCP session immediately after getting the URL
                    if self.skip_mcp_initialization:
                        log("ℹ️ Skipping MCP initialization (SKIP_MCP_INITIALIZATION=true)")
                        log("   E2B will auto-manage MCP servers. Attempting direct tool calls.")
                        self._mcp_initialized = True  # Mark as initialized to allow calls
                    else:
                        if not self._initialize_mcp_session():
                            log("⚠️ MCP session initialization failed")
                            log("   Trying to proceed anyway - E2B may auto-manage servers")
                            # Don't fall back to stdio - try to use gateway anyway
                            self._mcp_initialized = True
                except Exception as e:
                    log(f"⚠️ MCP Gateway not available, falling back to stdio: {e}")
                    self.use_e2b_mcp_gateway = False

            # Install dependencies with extended timeout (0 = no timeout)
            sandbox.commands.run("pip install playwright PyGithub", timeout=300)
            sandbox.commands.run("playwright install chromium --with-deps", timeout=600)
            sandbox.commands.run("npm --version", timeout=30)

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
                pr_proc = sandbox.commands.run("python3 /home/user/get_pr_info.py", timeout=60)

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

            # Git operations with extended timeout
            sandbox.commands.run(f"git clone {repo_url} /home/user/repo", timeout=180)
            sandbox.commands.run(
                f"cd /home/user/repo && git fetch origin {base_branch} && git fetch origin {head_branch}", timeout=120
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
                sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}", timeout=60)
                sandbox.commands.run("cd /home/user/repo && npm install", timeout=300)
                sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True, timeout=30)
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
                proc = sandbox.commands.run("python3 /home/user/take_shot.py", timeout=300)
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

                sandbox.commands.run("pkill -f node", timeout=10)
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

                # デバッグログ：スクリーンショット取得状況
                if not results["main_screenshot"]:
                    log(f"⚠️ Base（main）ブランチのスクリーンショットが取得できませんでした（ページ: {first_label}）")
                else:
                    log(f"✅ Base（main）ブランチのスクリーンショット取得成功（ページ: {first_label}）")
                if not results["feature_screenshot"]:
                    log(f"⚠️ Feature（PR）ブランチのスクリーンショットが取得できませんでした（ページ: {first_label}）")
                else:
                    log(f"✅ Feature（PR）ブランチのスクリーンショット取得成功（ページ: {first_label}）")

            if skip_ai:
                log("ℹ️ AI analysis skipped by mode setting.")
            elif not self.groq_client:
                log("⚠️ Groq client not configured. Skipping AI analysis.")
            else:
                # スクリーンショットがなくてもGit diffだけでAI分析を実行
                has_screenshots = results["main_screenshot"] and results["feature_screenshot"]
                if has_screenshots:
                    log("🧠 Generating final report with Groq Vision (with screenshots)...")
                else:
                    log("🧠 Generating final report with Groq (diff-only mode)...")

                try:
                    report = self.generate_analysis_report(
                        results["main_screenshot"], results["feature_screenshot"], results["git_diff"]
                    )
                    results["ai_report"] = report
                except Exception as exc:  # pragma: no cover
                    log(f"⚠️ AI analysis failed: {exc}")
                    results["ai_report"] = "Analysis failed."

            if notify_github:
                if not self.github_token:
                    log("⚠️ GitHub token missing. Skipping notification.")
                else:
                    log("📤 Uploading assets and commenting on GitHub PR...")
                    # AI分析レポートが存在する場合のみ書き込む
                    if results["ai_report"]:
                        sandbox.files.write("/home/user/report.txt", results["ai_report"])
                    else:
                        # AI分析が失敗した場合はデフォルトメッセージ
                        sandbox.files.write(
                            "/home/user/report.txt",
                            "スクリーンショットのみ撮影しました。AI分析は実行されませんでした。",
                        )

                    if results["main_screenshot"]:
                        sandbox.files.write("/home/user/before.png", results["main_screenshot"])
                        log(f"   ✅ before.png written ({len(results['main_screenshot'])} bytes)")
                    else:
                        log("   ⚠️ main_screenshot is None, skipping before.png")

                    if results["feature_screenshot"]:
                        sandbox.files.write("/home/user/after.png", results["feature_screenshot"])
                        log(f"   ✅ after.png written ({len(results['feature_screenshot'])} bytes)")
                    else:
                        log("   ⚠️ feature_screenshot is None, skipping after.png")

                    if results["partial_screenshot"]:
                        sandbox.files.write("/home/user/diff_focus.png", results["partial_screenshot"])
                        log(f"   ✅ diff_focus.png written ({len(results['partial_screenshot'])} bytes)")
                    else:
                        log("   ℹ️ partial_screenshot is None, skipping diff_focus.png")

                    screens_dir = "/home/user/screens"
                    sandbox.commands.run(f"rm -rf {screens_dir} && mkdir -p {screens_dir}", timeout=30)
                    manifest_entries = []
                    ordered_labels = results.get("page_order") or list(results["page_screenshots"].keys())
                    log(f"   📁 Processing {len(ordered_labels)} pages for GitHub upload...")

                    for idx, label_name in enumerate(ordered_labels):
                        page_data = results["page_screenshots"].get(label_name)
                        if not page_data:
                            log(f"   ⚠️ Page '{label_name}' has no data, skipping")
                            continue
                        safe_label = page_data.get("safe_label") or self._safe_label(label_name, idx)
                        manifest_entries.append({"label": label_name, "safe_label": safe_label})
                        page_dir = f"{screens_dir}/{safe_label}"
                        sandbox.commands.run(f"mkdir -p {page_dir}", timeout=10)

                        base_full = page_data.get("base", {}).get("full")
                        if base_full:
                            sandbox.files.write(f"{page_dir}/base_full.png", base_full)
                            log(f"   ✅ {label_name}/base_full.png ({len(base_full)} bytes)")
                        else:
                            log(f"   ⚠️ {label_name}/base_full.png is None")

                        feature_full = page_data.get("feature", {}).get("full")
                        if feature_full:
                            sandbox.files.write(f"{page_dir}/feature_full.png", feature_full)
                            log(f"   ✅ {label_name}/feature_full.png ({len(feature_full)} bytes)")
                        else:
                            log(f"   ⚠️ {label_name}/feature_full.png is None")

                        feature_focus = page_data.get("feature", {}).get("partial")
                        if feature_focus:
                            sandbox.files.write(f"{page_dir}/feature_focus.png", feature_focus)
                            log(f"   ✅ {label_name}/feature_focus.png ({len(feature_focus)} bytes)")
                        else:
                            log(f"   ℹ️ {label_name}/feature_focus.png is None")

                    sandbox.files.write(f"{screens_dir}/manifest.json", json.dumps(manifest_entries).encode("utf-8"))
                    log(f"   ✅ manifest.json written with {len(manifest_entries)} entries")

                    github_script = self._render_template(
                        "upload_github_report.py.tpl",
                        repo_name=repo_name,
                        pr_number=current_pr_number,
                    )
                    sandbox.files.write("/home/user/post_gh.py", github_script)
                    log("   🚀 Running GitHub upload script...")
                    gh_proc = sandbox.commands.run("python3 /home/user/post_gh.py", timeout=120)

                    stdout = self._to_text(gh_proc.stdout)
                    stderr = self._to_text(gh_proc.stderr)

                    if stdout:
                        log(f"   📤 Upload stdout: {stdout[:500]}")
                    if stderr:
                        log(f"   ⚠️ Upload stderr: {stderr[:500]}")

                    if "GITHUB_SUCCESS" in stdout:
                        log("✅ GitHub comment posted with artifacts.")
                    else:
                        log(f"⚠️ GitHub post failed.")
                        log(f"   Exit code: {gh_proc.exit_code}")
                        if "UPLOAD_FAIL" in stdout:
                            log("   ⚠️ Some files failed to upload to GitHub")
            else:
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

        diff_excerpt = (diff_text or "")[: self.diff_prompt_chars]
        if diff_text and len(diff_text) > self.diff_prompt_chars:
            diff_excerpt = f"{diff_excerpt}\n... (diff truncated)"

        # スクリーンショットの有無で処理を分岐
        has_images = img_before and img_after

        if has_images:
            # Vision APIを使用（スクリーンショット付き）
            b64_before = base64.b64encode(img_before).decode("utf-8")
            b64_after = base64.b64encode(img_after).decode("utf-8")

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
        else:
            # テキストのみでAI分析（スクリーンショットなし）
            prompt = f"""
Analyze the following Git diff and explain the changes for a code review.

Git Diff:
{diff_excerpt}

## Output Format (Markdown)
- **Summary**: single sentence describing the change
- **Code Changes**: detailed explanation of what changed and why
- **Impact**: potential impact on functionality
- **Notes for Reviewers**: things reviewers should pay attention to
- **Recommendation**: suggest if screenshots would help review (if UI changes detected)
"""

            completion = self.groq_client.chat.completions.create(
                model=self.target_model,  # Vision不要なのでテキストモデルを使用
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                max_tokens=1024,
            )

        return completion.choices[0].message.content

import os
import base64
import json
import time
import re
import shlex
import textwrap
from pathlib import Path
from string import Template
from e2b_code_interpreter import Sandbox
from dotenv import load_dotenv
from groq import Groq

load_dotenv()


SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


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

    def _resolve_code_mcp_command(self):
        direct = os.getenv("CODE_MCP_COMMAND")
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
        fallback = os.getenv("PLAYWRIGHT_MCP_COMMAND", "")
        parsed_fallback = self._parse_command(fallback)
        if parsed_fallback:
            return parsed_fallback
        return ["npx", "-y", "@e2b/mcp-server"]

    def __init__(self):
        self.e2b_api_key = os.getenv("E2B_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.github_token = os.getenv("GITHUB_ACCESS_TOKEN")
        self.mcp_github_pat = os.getenv("MCP_GITHUB_PAT")
        self.github_mcp_command = self._parse_command(os.getenv("GITHUB_MCP_COMMAND", ""))
        self.github_mcp_tool = os.getenv("GITHUB_MCP_TOOL")
        self.code_mcp_command = self._resolve_code_mcp_command()
        self.code_mcp_tool = os.getenv("CODE_MCP_TOOL", "execute_code")
        self.sandbox_python_cmd = os.getenv("SANDBOX_PYTHON_CMD") or os.getenv("PYTHON_CMD") or "python3"

        if not self.e2b_api_key:
            raise ValueError("E2B_API_KEY not found in .env")

        self.groq_client = Groq(api_key=self.groq_api_key) if self.groq_api_key and Groq else None
        self.selector_model = os.getenv("GROQ_SELECTOR_MODEL", "llama3-8b-8192")
        self.target_model = os.getenv("GROQ_TARGET_MODEL", "llama3-8b-8192")
        self.vision_model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        self.diff_prompt_chars = int(os.getenv("GIT_DIFF_PROMPT_CHARS", "6000"))

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
        """
        mcp_servers = {}
        if self.mcp_github_pat:
            mcp_servers["github"] = {"personalAccessToken": self.mcp_github_pat}
        return mcp_servers

    def detect_navigation_targets(self, diff_text: str, repo_url: str, sandbox, max_pages: int = 1, logger=None):
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

        if self.github_mcp_command and self.github_mcp_tool:
            logger("🧩 GitHub MCP へナビゲーションターゲットを問い合わせ中...")
            mcp_targets = self._github_mcp_targets(sandbox, diff_text, repo_url, max_pages=max_pages)
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

    def _github_mcp_targets(self, sandbox, diff_text, repo_url, max_pages):
        if not self.github_mcp_command or not self.github_mcp_tool:
            return None
        chunks = self._split_diff_by_file(diff_text)
        arguments = {
            "paths": [chunk.get("file") for chunk in chunks if chunk.get("file")],
            "maxPages": max_pages,
        }
        targets_data = None
        if arguments["paths"]:
            requests = [
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {
                        "name": self.github_mcp_tool,
                        "arguments": arguments,
                    },
                }
            ]
            env_updates = {}
            if self.mcp_github_pat:
                env_updates["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.mcp_github_pat
            response = self._call_mcp(sandbox, self.github_mcp_command, requests, env_updates)
            if response:
                entry = response[0]
                if not entry.get("error"):
                    contents = entry.get("result", {}).get("content", [])
                    for item in contents:
                        if item.get("type") == "json" and item.get("data"):
                            targets_data = item["data"]
                            break
                        if item.get("type") == "text" and item.get("text"):
                            try:
                                targets_data = json.loads(item["text"])
                                break
                            except json.JSONDecodeError:
                                continue
        if isinstance(targets_data, str):
            try:
                targets_data = json.loads(targets_data)
            except json.JSONDecodeError:
                targets_data = None
        targets = []
        for chunk in chunks:
            file_path = chunk.get("file")
            if not file_path:
                continue
            file_content = self._github_mcp_read_file(sandbox, file_path)
            groq_targets = self._groq_targets_from_file(file_path, chunk.get("lines", []), file_content)
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

    def _prepare_targets(self, targets, max_pages):
        prepared = []
        effective = targets or [{"label": "root", "path": "/", "selector": "body", "steps": []}]
        for idx, target in enumerate(effective[: max(1, max_pages)]):
            prepared.append(
                {
                    "label": target.get("label") or f"page_{idx + 1}",
                    "safe_label": target.get("safe_label") or self._safe_label(target.get("label", ""), idx),
                    "path": target.get("path") or "/",
                    "selector": target.get("selector") or "body",
                    "steps": target.get("steps") or [],
                }
            )
        return prepared

    def _github_mcp_read_file(self, sandbox, file_path):
        if not self.github_mcp_command:
            return None
        requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": self.github_mcp_tool,
                    "arguments": {"path": file_path},
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
        if self.code_mcp_command:
            logger(f"🎥 E2B Code Interpreter MCP を使用して {label} のスクリーンショットを取得します。")
            return self._capture_branch_pages_code_mcp(sandbox, branch_name, label, branch_key, targets, logger=logger)
        logger(f"🎥 Code Interpreter MCP が無効のためローカル Playwright で {label} を撮影します。")
        return self._capture_branch_pages_local(sandbox, branch_name, label, branch_key, targets, logger=logger)

    def _capture_branch_pages_local(self, sandbox, branch_name, label, branch_key, targets, logger=None):
        logger = logger or (lambda *_: None)
        log_prefix = f"{label}: {branch_name}"
        target_count = len(targets) if targets else 0
        prepared_targets = self._prepare_targets(targets, target_count or 1)
        sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}")
        sandbox.commands.run("cd /home/user/repo && npm install")
        sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True)
        time.sleep(5)

        targets_literal = json.dumps(prepared_targets)
        script_text = self._render_template(
            "capture_targets.py.tpl",
            targets_json=targets_literal,
            base_url="http://localhost:5173",
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
        sandbox.commands.run("cd /home/user/repo && npm install")
        sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True)
        time.sleep(5)

        branch_results = {}
        base_url = "http://localhost:5173"
        for target in prepared_targets:
            branch_results[target["label"]] = {"full": None, "partial": None}
            url = base_url + (target["path"] or "/")
            selector = target.get("selector") or "body"
            code = self._build_code_executor_script(url, selector)
            response = self._run_code_mcp(sandbox, code)
            full_bytes, focus_bytes = self._parse_code_executor_response(response)
            if full_bytes:
                branch_results[target["label"]]["full"] = full_bytes
            if focus_bytes:
                branch_results[target["label"]]["partial"] = focus_bytes
            if not full_bytes:
                logger(f"⚠️ {log_prefix} -> {target['label']} のフルスクリーンショット取得に失敗しました。")

        sandbox.commands.run("pkill -f node")
        return branch_results

    def _build_code_executor_script(self, url, selector):
        url_literal = json.dumps(url)
        selector_literal = json.dumps(selector or "body")
        return textwrap.dedent(
            f"""
            import asyncio
            import base64
            from playwright.async_api import async_playwright

            async def main():
                async with async_playwright() as p:
                    browser = await p.chromium.launch()
                    page = await browser.new_page()
                    await page.goto({url_literal}, timeout=20000)
                    await page.wait_for_timeout(2000)
                    full_bytes = await page.screenshot(full_page=True)
                    print("FULL_B64::" + base64.b64encode(full_bytes).decode())
                    selector = {selector_literal}
                    if selector:
                        locator = page.locator(selector)
                        if await locator.count() > 0:
                            focus_bytes = await locator.first.screenshot()
                            print("FOCUS_B64::" + base64.b64encode(focus_bytes).decode())
                    await browser.close()

            asyncio.run(main())
            """
        ).strip()

    def _run_code_mcp(self, sandbox, code, language="python"):
        if not self.code_mcp_command:
            return None
        requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": self.code_mcp_tool,
                    "arguments": {"language": language, "code": code},
                },
            }
        ]
        env_updates = {}
        if self.e2b_api_key:
            env_updates["E2B_API_KEY"] = self.e2b_api_key
        return self._call_mcp(
            sandbox,
            self.code_mcp_command,
            requests,
            env_updates if env_updates else None,
        )

    def _parse_code_executor_response(self, response):
        if not response:
            return (None, None)
        entry = response[0]
        if entry.get("error"):
            return (None, None)
        contents = entry.get("result", {}).get("content", [])
        text_output = []
        images = []
        for item in contents:
            if item.get("type") == "text" and item.get("text"):
                text_output.append(item["text"])
            elif item.get("type") == "image" and item.get("data"):
                try:
                    images.append(base64.b64decode(item["data"]))
                except Exception:
                    continue
        text_blob = "\n".join(text_output)
        full_bytes = self._extract_b64_from_text(text_blob, "FULL_B64::")
        focus_bytes = self._extract_b64_from_text(text_blob, "FOCUS_B64::")
        if not full_bytes and images:
            full_bytes = images[0]
        if not focus_bytes and images[1:]:
            focus_bytes = images[1]
        return (full_bytes, focus_bytes)

    @staticmethod
    def _extract_b64_from_text(text_blob, marker):
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

            sandbox.commands.run("pip install playwright PyGithub")
            sandbox.commands.run("playwright install chromium --with-deps")
            sandbox.commands.run("npm --version")

            repo_name = repo_url.replace("https://github.com/", "").replace(".git", "")
            log(f"🔍 Fetching PR info for {repo_name}...")

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
            diff_proc = sandbox.commands.run(
                f"cd /home/user/repo && git diff origin/{base_branch}..origin/{head_branch}"
            )
            results["git_diff"] = self._to_text(diff_proc.stdout)

            nav_targets = [{"label": "root", "path": "/", "selector": "body", "steps": []}]
            if results["git_diff"]:
                log("🧠 Analyzing diff to identify navigation targets...")
                nav_targets = self.detect_navigation_targets(
                    results["git_diff"], repo_url, sandbox, max_pages=max_pages, logger=log
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

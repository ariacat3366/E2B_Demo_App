import os
import base64
import time
import importlib
from e2b_code_interpreter import Sandbox
from dotenv import load_dotenv

Groq = None
_groq_spec = importlib.util.find_spec("groq")
if _groq_spec:
    GroqModule = importlib.import_module("groq")
    Groq = getattr(GroqModule, "Groq", None)

# .envの読み込み
load_dotenv()


class DiffVisionAgent:
    def __init__(self):
        self.e2b_api_key = os.getenv("E2B_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.slack_token = os.getenv("SLACK_BOT_TOKEN")
        self.github_token = os.getenv("GITHUB_ACCESS_TOKEN")

        if not self.e2b_api_key:
            raise ValueError("E2B_API_KEY not found in .env")

        self.groq_client = Groq(api_key=self.groq_api_key) if self.groq_api_key and Groq else None

    @staticmethod
    def _decode_output(stream):
        if isinstance(stream, (bytes, bytearray)):
            return stream.decode("utf-8", errors="ignore")
        return stream or ""

    @staticmethod
    def _ensure_bytes(data):
        if isinstance(data, bytearray):
            return bytes(data)
        return data

    def analyze_pr(
        self,
        repo_url: str,
        main_branch: str,
        feature_branch: str,
        status_callback=None,
        use_slack: bool = False,
        slack_channel: str = "",
        skip_ai: bool = False,
        use_github: bool = False,
    ):
        """
        Main Flow:
        1. E2B起動 (Tools環境)
        2. Git/Browser Toolで画像取得
        3. Groq (Local) で解析
        4. Slack/GitHub Tool (E2B内) で通知
        """
        results = {
            "main_screenshot": None,
            "feature_screenshot": None,
            "git_diff": "",
            "ai_report": "",
            "logs": [],
        }

        def log(message):
            results["logs"].append(message)
            if status_callback:
                status_callback(message)
            print(f"[Agent] {message}")

        log("🚀 Starting E2B Sandbox (Tool Environment)...")

        env_vars = {}
        if self.slack_token:
            env_vars["SLACK_BOT_TOKEN"] = self.slack_token
        if self.github_token:
            env_vars["GITHUB_ACCESS_TOKEN"] = self.github_token

        sandbox_kwargs = {}
        if self.e2b_api_key:
            sandbox_kwargs["api_key"] = self.e2b_api_key
        if env_vars:
            sandbox_kwargs["env_vars"] = env_vars

        with Sandbox.create(**sandbox_kwargs) as sandbox:
            log("📦 Sandbox Ready. Installing Tools...")
            setup_cmds = [
                "pip install playwright slack_sdk PyGithub",
                "playwright install chromium --with-deps",
                # Vite系テンプレートに同梱されているnpmで問題ないためグローバル更新は行わない
                "npm --version",
            ]

            for cmd in setup_cmds:
                log(f"⚙️ Installing Tool: {cmd}...")
                sandbox.commands.run(cmd)

            log(f"🔍 Cloning Repository: {repo_url}")
            sandbox.commands.run(f"git clone {repo_url} /home/user/repo")

            def capture_branch_state(branch_name, label):
                log(f"🔀 Processing {label}: {branch_name}")
                sandbox.commands.run(f"cd /home/user/repo && git fetch origin && git checkout {branch_name}")
                log(f"📦 Installing dependencies for {branch_name}...")
                sandbox.commands.run("cd /home/user/repo && npm install")

                log(f"🚀 Starting Dev Server for {branch_name}...")
                sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True)
                time.sleep(5)

                log(f"📸 Taking Screenshot: {label}")
                screenshot_script = """
import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto("http://localhost:5173", timeout=10000)
            await page.wait_for_timeout(2000)
            await page.screenshot(path="/home/user/screenshot.png", full_page=True)
            print("SUCCESS")
        except Exception as e:
            print(f"ERROR: {e}")
        finally:
            await browser.close()

asyncio.run(run())
"""
                sandbox.files.write("/home/user/take_shot.py", screenshot_script)
                proc = sandbox.commands.run("python /home/user/take_shot.py")
                stdout_text = self._decode_output(proc.stdout)

                if "SUCCESS" in stdout_text:
                    img_bytes = sandbox.files.read("/home/user/screenshot.png", format="bytes")
                    img_bytes = self._ensure_bytes(img_bytes)
                    sandbox.commands.run("pkill -f node")
                    return img_bytes

                stderr_text = self._decode_output(proc.stderr)
                log(f"❌ Screenshot failed: {stdout_text} {stderr_text}")
                sandbox.commands.run("pkill -f node")
                return None

            results["main_screenshot"] = capture_branch_state(main_branch, "Main Branch")
            results["feature_screenshot"] = capture_branch_state(feature_branch, "Feature Branch")

            log("📝 Extracting Git Diff...")
            diff_proc = sandbox.commands.run(
                f"cd /home/user/repo && git diff origin/{main_branch}..origin/{feature_branch}"
            )
            results["git_diff"] = self._decode_output(diff_proc.stdout)

            if skip_ai:
                log("🛑 Test Mode: AI Analysis & Notification skipped.")
                return results

            if self.groq_client and results["main_screenshot"] and results["feature_screenshot"]:
                log("🧠 Analyzing Differences with Groq Vision AI (Local)...")
                try:
                    report = self.generate_analysis_report(
                        results["main_screenshot"], results["feature_screenshot"], results["git_diff"]
                    )
                    results["ai_report"] = report
                    log("✨ AI Analysis Generated.")
                except Exception as exc:
                    log(f"⚠️ AI Analysis failed: {exc}")
                    results["ai_report"] = f"AI Analysis Failed: {exc}"

            if use_slack and self.slack_token and results["ai_report"]:
                log(f"📢 Executing Slack Tool inside E2B ({slack_channel})...")
                if results["main_screenshot"]:
                    sandbox.files.write("/home/user/before.png", results["main_screenshot"])
                if results["feature_screenshot"]:
                    sandbox.files.write("/home/user/after.png", results["feature_screenshot"])
                sandbox.files.write("/home/user/report.txt", results["ai_report"])

                slack_script = f"""
import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

token = os.getenv("SLACK_BOT_TOKEN")
client = WebClient(token=token)
channel = "{slack_channel}"

try:
    if os.path.exists("/home/user/before.png"):
        with open("/home/user/before.png", "rb") as f:
            client.files_upload_v2(channel=channel, file=f, filename="before.png", title="🟥 Before")

    if os.path.exists("/home/user/after.png"):
        with open("/home/user/after.png", "rb") as f:
            client.files_upload_v2(channel=channel, file=f, filename="after.png", title="🟩 After")

    with open("/home/user/report.txt", "r") as f:
        report = f.read()

    client.chat_postMessage(
        channel=channel,
        text=f"*🤖 Diff-Vision Agent Report (from E2B)*\\n\\n{{report}}",
        mrkdwn=True
    )
    print("SLACK_SUCCESS")
except Exception as e:
    print(f"SLACK_ERROR: {{e}}")
"""
                sandbox.files.write("/home/user/notify_slack.py", slack_script)
                slack_proc = sandbox.commands.run("python /home/user/notify_slack.py")
                slack_stdout = self._decode_output(slack_proc.stdout)
                slack_stderr = self._decode_output(slack_proc.stderr)
                if "SLACK_SUCCESS" in slack_stdout:
                    log("✅ Slack Notification Sent via E2B.")
                else:
                    log(f"⚠️ Slack Tool Failed: {slack_stdout} {slack_stderr}")

            if use_github and self.github_token and results["ai_report"]:
                try:
                    repo_name = repo_url.replace("https://github.com/", "").replace(".git", "")
                    log(f"🐱 Executing GitHub Tool inside E2B ({repo_name})...")
                    sandbox.files.write("/home/user/report.txt", results["ai_report"])

                    github_script = f"""
import os
from github import Github

token = os.getenv("GITHUB_ACCESS_TOKEN")
g = Github(token)
repo = g.get_repo("{repo_name}")

with open("/home/user/report.txt", "r") as f:
    report = f.read()

pulls = repo.get_pulls(state='open', sort='created', direction='desc')
if pulls.totalCount > 0:
    pr = pulls[0]
    pr.create_issue_comment("## 🤖 Diff-Vision Analysis Report\\n\\n" + report + "\\n\\n*Generated by E2B Agent*")
    print("GITHUB_SUCCESS")
else:
    print("GITHUB_NO_PR")
"""
                    sandbox.files.write("/home/user/comment_github.py", github_script)
                    gh_proc = sandbox.commands.run("python /home/user/comment_github.py")
                    gh_stdout = self._decode_output(gh_proc.stdout)
                    gh_stderr = self._decode_output(gh_proc.stderr)

                    if "GITHUB_SUCCESS" in gh_stdout:
                        log("✅ GitHub Comment Posted via E2B.")
                    else:
                        log(f"ℹ️ GitHub Tool Log: {gh_stdout} {gh_stderr}")
                except Exception as exc:
                    log(f"⚠️ GitHub Tool Error: {exc}")

            log("✅ All Tasks Completed.")

        return results

    def generate_analysis_report(self, img_before: bytes, img_after: bytes, diff_text: str):
        """
        Groq Vision API (Llama 3.2 Vision) を使用してレポート生成
        """
        b64_before = base64.b64encode(img_before).decode("utf-8")
        b64_after = base64.b64encode(img_after).decode("utf-8")
        truncated_diff = f"{diff_text[:2000]} ... (truncated)" if diff_text else "Diff not available."
        prompt = f"""
あなたは熟練したUI/UXエンジニア兼コードレビュアーです。
以下の2つの画像（変更前、変更後）と、GitのDiff情報を分析し、変更内容の解説レポートを作成してください。

## Input Data
- Image 1: Before Change (Main Branch)
- Image 2: After Change (Feature Branch)
- Git Diff:
```
{truncated_diff}
```

## Output Format (Markdown)
以下のセクションで簡潔に日本語で記述してください：
1. **変更概要**: 何が変わったか（見た目、機能）
2. **デザイン変更点**: 色、レイアウト、追加された要素など視覚的な違い
3. **コード解析**: Diffから読み取れる技術的な変更点（コンポーネント、ロジック）
4. **レビュワーコメント**: 改善点や注意点があれば

出力はMarkdownのみにしてください。
"""
        completion = self.groq_client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_before}"},
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_after}"},
                        },
                    ],
                }
            ],
            temperature=0.7,
            max_tokens=1024,
        )
        return completion.choices[0].message.content

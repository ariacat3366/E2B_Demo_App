import os
import base64
import json
import time
from e2b_code_interpreter import Sandbox
from dotenv import load_dotenv

try:
    from groq import Groq
except ImportError:  # pragma: no cover
    Groq = None

load_dotenv()


class DiffVisionAgent:
    def __init__(self):
        self.e2b_api_key = os.getenv("E2B_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.github_token = os.getenv("GITHUB_ACCESS_TOKEN")

        if not self.e2b_api_key:
            raise ValueError("E2B_API_KEY not found in .env")

        self.groq_client = Groq(api_key=self.groq_api_key) if self.groq_api_key and Groq else None
        self.selector_model = os.getenv("GROQ_SELECTOR_MODEL", "llama3-8b-8192")
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

    def analyze_pr(
        self, repo_url: str, pr_number: int = None, status_callback=None, skip_ai=False, notify_github=False
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
            pr_proc = sandbox.commands.run("python /home/user/get_pr_info.py")

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

            target_selector = "body"
            if not skip_ai and results["git_diff"]:
                log("🧠 Analyzing diff to identify UI selectors...")
                target_selector = self.detect_changed_selector(results["git_diff"])
                log(f"🎯 Targeted selector for screenshot: '{target_selector}'")

            def capture_branch_state(branch_name, label, selector="body"):
                log(f"🔀 Processing {label}: {branch_name}")
                sandbox.commands.run(f"cd /home/user/repo && git checkout {branch_name}")
                sandbox.commands.run("cd /home/user/repo && npm install")
                sandbox.commands.run("cd /home/user/repo && npm run dev -- --host &", background=True)
                time.sleep(5)

                screenshot_script = f"""
import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto("http://localhost:5173", timeout=10000)
            await page.wait_for_timeout(2000)

            await page.screenshot(path="/home/user/full.png", full_page=True)

            try:
                locator = page.locator("{selector}")
                if await locator.count() > 0:
                    await locator.first.screenshot(path="/home/user/partial.png")
                    print("PARTIAL_SUCCESS")
                else:
                    print("SELECTOR_NOT_FOUND")
            except Exception:
                print("PARTIAL_FAIL")

            print("FULL_SUCCESS")
        except Exception as e:
            print(f"ERROR: {{e}}")
        finally:
            await browser.close()

asyncio.run(run())
"""
                sandbox.files.write("/home/user/take_shot.py", screenshot_script)
                proc = sandbox.commands.run("python /home/user/take_shot.py")
                stdout_text = self._to_text(proc.stdout)

                full_bytes = None
                partial_bytes = None

                if "FULL_SUCCESS" in stdout_text:
                    full_bytes = sandbox.files.read("/home/user/full.png", format="bytes")
                    full_bytes = self._ensure_bytes(full_bytes)

                if "PARTIAL_SUCCESS" in stdout_text:
                    partial_bytes = sandbox.files.read("/home/user/partial.png", format="bytes")
                    partial_bytes = self._ensure_bytes(partial_bytes)

                sandbox.commands.run("pkill -f node")
                return full_bytes, partial_bytes

            main_full, _ = capture_branch_state(base_branch, "Main Branch")
            feature_full, feature_partial = capture_branch_state(head_branch, "Feature Branch", target_selector)
            results["main_screenshot"] = main_full
            results["feature_screenshot"] = feature_full
            results["partial_screenshot"] = feature_partial

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

                    github_script = f"""
import os
import time
from github import Github

token = os.getenv("GITHUB_ACCESS_TOKEN")
g = Github(token)
repo = g.get_repo("{repo_name}")
pr = repo.get_pull({current_pr_number})

branch_name = "diff-artifacts"
try:
    repo.get_branch(branch_name)
except Exception:
    sb = repo.get_branch(repo.default_branch)
    repo.create_git_ref(ref=f"refs/heads/{{branch_name}}", sha=sb.commit.sha)

timestamp = int(time.time())
base_path = f"reports/pr_{current_pr_number}/{{timestamp}}"

def upload_file(path, content, msg):
    try:
        repo.create_file(
            path=f"{{base_path}}/{{path}}",
            message=msg,
            content=content,
            branch=branch_name
        )
        return f"https://raw.githubusercontent.com/{repo_name}/{{branch_name}}/{{base_path}}/{{path}}"
    except Exception as e:
        print(f"UPLOAD_FAIL: {{e}}")
        return None

print("Uploading Before Image...")
url_before = upload_file("before.png", open("/home/user/before.png", "rb").read(), "Add before img")

print("Uploading After Image...")
url_after = upload_file("after.png", open("/home/user/after.png", "rb").read(), "Add after img")

url_partial = None
if os.path.exists("/home/user/diff_focus.png"):
    print("Uploading Partial Image...")
    url_partial = upload_file("focus.png", open("/home/user/diff_focus.png", "rb").read(), "Add focus img")

report_text = open("/home/user/report.txt").read()

comment_body = f\"\"\"
## 🤖 Diff-Vision Analysis Report

{{report_text}}

### 📸 Visual Differences

| Before (Main) | After (Feature) |
|:---:|:---:|
| ![]({{url_before}}) | ![]({{url_after}}) |
\"\"\"

if url_partial:
    comment_body += f"\\n### 🔍 Focus Area\\n![]({{url_partial}})"

pr.create_issue_comment(comment_body)
print("GITHUB_SUCCESS")
"""
                    sandbox.files.write("/home/user/post_gh.py", github_script)
                    gh_proc = sandbox.commands.run("python /home/user/post_gh.py")
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

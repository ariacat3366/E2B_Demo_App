import os
import time
import json
from github import Github

token = os.getenv("GITHUB_ACCESS_TOKEN")
g = Github(token)
repo = g.get_repo("$repo_name")
pr = repo.get_pull($pr_number)

branch_name = "diff-artifacts"
try:
    repo.get_branch(branch_name)
except Exception:
    sb = repo.get_branch(repo.default_branch)
    repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=sb.commit.sha)

timestamp = int(time.time())
base_path = f"reports/pr_$pr_number/{timestamp}"

def upload_file(path, content, msg):
    try:
        repo.create_file(
            path=f"{base_path}/{path}",
            message=msg,
            content=content,
            branch=branch_name
        )
        return f"https://raw.githubusercontent.com/{repo_name}/{branch_name}/{base_path}/{path}"
    except Exception as e:
        print(f"UPLOAD_FAIL: {e}")
        return None

page_manifest = []
screens_dir = "/home/user/screens"
manifest_path = os.path.join(screens_dir, "manifest.json")
ordered_entries = []
if os.path.exists(manifest_path):
    try:
        ordered_entries = json.load(open(manifest_path))
    except Exception:
        ordered_entries = []
if not ordered_entries and os.path.exists(screens_dir):
    for label in sorted(os.listdir(screens_dir)):
        ordered_entries.append({"label": label, "safe_label": label})

for entry in ordered_entries:
    safe_label = entry.get("safe_label") or entry.get("label")
    display_label = entry.get("label") or safe_label
    label_path = os.path.join(screens_dir, safe_label)
    if not os.path.isdir(label_path):
        continue
    record = {"label": display_label, "base_full": None, "feature_full": None, "feature_focus": None}
    base_full = os.path.join(label_path, "base_full.png")
    feature_full = os.path.join(label_path, "feature_full.png")
    feature_focus = os.path.join(label_path, "feature_focus.png")
    if os.path.exists(base_full):
        record["base_full"] = upload_file(f"{safe_label}/base_full.png", open(base_full, "rb").read(), f"Add base full {display_label}")
    if os.path.exists(feature_full):
        record["feature_full"] = upload_file(f"{safe_label}/feature_full.png", open(feature_full, "rb").read(), f"Add feature full {display_label}")
    if os.path.exists(feature_focus):
        record["feature_focus"] = upload_file(f"{safe_label}/feature_focus.png", open(feature_focus, "rb").read(), f"Add feature focus {display_label}")
    page_manifest.append(record)

url_before = upload_file("before.png", open("/home/user/before.png", "rb").read(), "Add before img") if os.path.exists("/home/user/before.png") else None
url_after = upload_file("after.png", open("/home/user/after.png", "rb").read(), "Add after img") if os.path.exists("/home/user/after.png") else None
url_partial = upload_file("focus.png", open("/home/user/diff_focus.png", "rb").read(), "Add focus img") if os.path.exists("/home/user/diff_focus.png") else None

report_text = open("/home/user/report.txt").read() if os.path.exists("/home/user/report.txt") else ""

def cell(url):
    return f"![]({url})" if url else "N/A"

table_rows = []
for entry in page_manifest:
    table_rows.append(f"| {entry['label']} | {cell(entry['base_full'])} | {cell(entry['feature_full'])} | {cell(entry['feature_focus'])} |")

comment_body = f"""
## 🤖 Diff-Vision Analysis Report

{report_text}

### 📸 Visual Differences (Top view)

| Before (Main) | After (Feature) |
|:---:|:---:|
| {cell(url_before)} | {cell(url_after)} |
"""

if table_rows:
    comment_body += "\n### 🔁 Per-Page Comparisons\n"
    comment_body += "| Page | Base | Feature | Focus |\n|:--|:--|:--|:--|\n"
    comment_body += "\n".join(table_rows)

if url_partial:
    comment_body += f"\n### 🔍 Focus Area\n![]({url_partial})"

pr.create_issue_comment(comment_body)
print("GITHUB_SUCCESS")


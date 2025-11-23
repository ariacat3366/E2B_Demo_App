import os
import time
import json
from github import Github, Auth

token = os.getenv("GITHUB_ACCESS_TOKEN")
auth = Auth.Token(token)
g = Github(auth=auth)
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
        full_path = f"{base_path}/{path}"
        # Check if file exists and update if it does, otherwise create
        try:
            existing = repo.get_contents(full_path, ref=branch_name)
            repo.update_file(
                path=full_path,
                message=msg,
                content=content,
                sha=existing.sha,
                branch=branch_name
            )
            print(f"✅ Updated: {full_path}")
        except Exception:
            # File doesn't exist, create it
            repo.create_file(
                path=full_path,
                message=msg,
                content=content,
                branch=branch_name
            )
            print(f"✅ Created: {full_path}")
        return f"https://raw.githubusercontent.com/{repo_name}/{branch_name}/{base_path}/{path}"
    except Exception as e:
        print(f"UPLOAD_FAIL ({path}): {type(e).__name__}: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()[:500]}")
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
        content = open(base_full, "rb").read()
        print(f"📤 Uploading {safe_label}/base_full.png ({len(content)} bytes)")
        record["base_full"] = upload_file(f"{safe_label}/base_full.png", content, f"Add base full {display_label}")
    if os.path.exists(feature_full):
        content = open(feature_full, "rb").read()
        print(f"📤 Uploading {safe_label}/feature_full.png ({len(content)} bytes)")
        record["feature_full"] = upload_file(f"{safe_label}/feature_full.png", content, f"Add feature full {display_label}")
    if os.path.exists(feature_focus):
        content = open(feature_focus, "rb").read()
        print(f"📤 Uploading {safe_label}/feature_focus.png ({len(content)} bytes)")
        record["feature_focus"] = upload_file(f"{safe_label}/feature_focus.png", content, f"Add feature focus {display_label}")
    page_manifest.append(record)

url_before = None
url_after = None
url_partial = None

if os.path.exists("/home/user/before.png"):
    content = open("/home/user/before.png", "rb").read()
    print(f"📤 Uploading before.png ({len(content)} bytes)")
    url_before = upload_file("before.png", content, "Add before img")
else:
    print("⚠️ before.png does not exist")

if os.path.exists("/home/user/after.png"):
    content = open("/home/user/after.png", "rb").read()
    print(f"📤 Uploading after.png ({len(content)} bytes)")
    url_after = upload_file("after.png", content, "Add after img")
else:
    print("⚠️ after.png does not exist")

if os.path.exists("/home/user/diff_focus.png"):
    content = open("/home/user/diff_focus.png", "rb").read()
    print(f"📤 Uploading focus.png ({len(content)} bytes)")
    url_partial = upload_file("focus.png", content, "Add focus img")
else:
    print("ℹ️ diff_focus.png does not exist (optional)")

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

print("\n📊 Upload Summary:")
print(f"  - Main images: before={url_before is not None}, after={url_after is not None}, focus={url_partial is not None}")
print(f"  - Page screenshots: {len(page_manifest)} pages processed")
for entry in page_manifest:
    print(f"    - {entry['label']}: base={entry['base_full'] is not None}, feature={entry['feature_full'] is not None}, focus={entry['feature_focus'] is not None}")

pr.create_issue_comment(comment_body)
print("GITHUB_SUCCESS")


import streamlit as st
import traceback
from agent_logic import DiffVisionAgent
from dotenv import load_dotenv

# Load env vars
load_dotenv()

# --- Page Config ---
st.set_page_config(page_title="Diff-Vision Agent", page_icon="🤖", layout="wide", initial_sidebar_state="expanded")

# --- Custom CSS ---
st.markdown(
    """
<style>
    .stApp {
        background-color: #0e1117;
        color: #fafafa;
    }
    .stStatusWidget {
        background-color: #262730;
    }
    .report-box {
        background-color: #1e2130;
        padding: 20px;
        border-radius: 10px;
        border-left: 5px solid #4ecdc4;
    }
</style>
""",
    unsafe_allow_html=True,
)

# --- Header ---
st.title("🤖 Diff-Vision Agent")
st.markdown("### GitHub PRの実装差分を可視化・解説するAIエージェント")

MODE_PRESETS = {
    "Screenshots Only": {
        "description": "アプリのスクリーンショットとGit Diffのみ取得（AI解析・通知なし）",
        "skip_ai": True,
        "slack": False,
        "github": False,
    },
    "Screenshots + AI Analysis": {
        "description": "スクショ取得後にGroqで解析のみ実行（通知なし）",
        "skip_ai": False,
        "slack": False,
        "github": False,
    },
    "Screenshots + AI Analysis + GitHub PR Notify": {
        "description": "AIレポートをGitHub PRコメントとして投稿",
        "skip_ai": False,
        "slack": False,
        "github": True,
    },
    "Screenshots + AI Analysis + Slack Notify": {
        "description": "AIレポートをSlackチャンネルへ投稿",
        "skip_ai": False,
        "slack": True,
        "github": False,
    },
    "Screenshots + AI Analysis + All Notify": {
        "description": "AIレポートをSlackとGitHubの両方に投稿",
        "skip_ai": False,
        "slack": True,
        "github": True,
    },
}

# --- Sidebar ---
with st.sidebar:
    st.header("⚙️ Settings")

    st.markdown("#### Execution Mode")
    exec_mode = st.radio("Choose Mode:", list(MODE_PRESETS.keys()), index=0)
    st.caption(MODE_PRESETS[exec_mode]["description"])

    st.divider()

    slack_required = MODE_PRESETS[exec_mode]["slack"]
    slack_channel = st.text_input("Slack Channel", value="#dev-alerts", disabled=not slack_required)

    st.divider()
    st.info("Supported: GitHub Public Repositories")
    st.warning("Hackathon Demo Mode")

# --- Main Input ---
col1, col2 = st.columns([3, 1])
with col1:
    default_url = "https://github.com/ariacat3366/Demo_Repository_for_Build_MCP_Agents"
    repo_url = st.text_input("Git Repository URL", value=default_url)

    b_col1, b_col2 = st.columns(2)
    with b_col1:
        base_branch = st.text_input("Base Branch", value="main")
    with b_col2:
        head_branch = st.text_input("Head Branch", value="feature/renew_ui")

with col2:
    st.write("")
    st.write("")
    st.write("")
    button_label = f"Run: {exec_mode}"
    start_btn = st.button(button_label, use_container_width=True, type="primary")

# --- Processing Logic ---
if start_btn and repo_url:
    result_container = st.container()

    try:
        agent = DiffVisionAgent()
    except ValueError as config_err:
        st.error(f"Configuration Error: {config_err}")
        st.stop()

    try:
        with st.status(f"Running Agent ({exec_mode})...", expanded=True) as status:

            def update_status(msg):
                st.write(msg)

            preset = MODE_PRESETS[exec_mode]
            skip_ai_flag = preset["skip_ai"]
            use_slack_flag = preset["slack"]
            use_github_flag = preset["github"]

            results = agent.analyze_pr(
                repo_url=repo_url,
                main_branch=base_branch,
                feature_branch=head_branch,
                status_callback=update_status,
                use_slack=use_slack_flag,
                slack_channel=slack_channel,
                skip_ai=skip_ai_flag,
                use_github=use_github_flag,
            )

            status.update(label="Process Complete!", state="complete", expanded=False)

        with result_container:
            st.divider()
            st.subheader("📸 Visual Regression")

            img_col1, img_col2 = st.columns(2)

            with img_col1:
                st.markdown(f"#### 🟥 Before ({base_branch})")
                if results["main_screenshot"]:
                    st.image(results["main_screenshot"], caption="Base Branch UI")
                else:
                    st.error("Screenshot failed")

            with img_col2:
                st.markdown(f"#### 🟩 After ({head_branch})")
                if results["feature_screenshot"]:
                    st.image(results["feature_screenshot"], caption="Head Branch UI")
                else:
                    st.error("Screenshot failed")

            st.divider()

            if skip_ai_flag:
                st.info("ℹ️ Test Mode Complete. AI Analysis & Notifications were skipped.")
            else:
                st.subheader("🧐 AI Analysis Report")
                if results["ai_report"]:
                    st.markdown(f'<div class="report-box">{results["ai_report"]}</div>', unsafe_allow_html=True)
                else:
                    st.warning("Report generation failed or was skipped.")

            with st.expander("Show Raw Git Diff"):
                st.code(results["git_diff"], language="diff")

    except Exception as exec_err:
        st.error(f"An error occurred: {exec_err}")
        st.code(traceback.format_exc())
else:
    if not repo_url:
        st.info("リポジトリURLを入力して分析を開始してください。")

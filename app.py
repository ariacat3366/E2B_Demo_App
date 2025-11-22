import streamlit as st
import traceback
from agent_logic import DiffVisionAgent
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Diff-Vision Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

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

st.title("🤖 Diff-Vision Agent")
st.markdown("### AI agent that visualizes GitHub PR changes")

with st.sidebar:
    st.header("⚙️ Settings")

    MODES = {
        "Screenshots Only": {
            "description": "Capture app screenshots and Git diff only (no AI).",
            "skip_ai": True,
            "notify_github": False,
        },
        "Screenshots + AI Analysis": {
            "description": "Capture screenshots and run Groq analysis (no notifications).",
            "skip_ai": False,
            "notify_github": False,
        },
        "Screenshots + AI Analysis + GitHub PR Notify": {
            "description": "Post the AI report and images as a GitHub PR comment.",
            "skip_ai": False,
            "notify_github": True,
        },
    }

    st.markdown("#### Execution Mode")
    selected_mode_name = st.radio("Choose mode:", list(MODES.keys()), index=0)
    current_mode = MODES[selected_mode_name]
    st.caption(f"ℹ️ {current_mode['description']}")

    st.divider()
    if current_mode["notify_github"]:
        st.info("✅ GitHub notification is enabled.")

    max_pages = st.number_input(
        "Max auto-detected pages",
        min_value=1,
        max_value=5,
        value=1,
        step=1,
        help="Controls how many pages the agent will attempt to auto-capture.",
    )

    st.warning("Hackathon demo mode")

col1, col2 = st.columns([3, 1])
with col1:
    default_url = "https://github.com/ariacat3366/Demo_Repository_for_Build_MCP_Agents"
    repo_url = st.text_input("Git repository URL", value=default_url)

    pr_number_input = st.number_input(
        "Target PR number (optional, 0 = latest)",
        min_value=0,
        value=0,
        step=1,
    )

with col2:
    st.write("")
    st.write("")
    st.write("")
    start_btn = st.button("Run Agent 🚀", use_container_width=True, type="primary")

if start_btn and repo_url:
    result_container = st.container()

    try:
        agent = DiffVisionAgent()
    except ValueError as config_err:
        st.error(f"Configuration error: {config_err}")
        st.stop()

    try:
        with st.status(f"Running: {selected_mode_name}...", expanded=True) as status:

            def update_status(msg):
                st.write(msg)

            target_pr = pr_number_input if pr_number_input > 0 else None
            results = agent.analyze_pr(
                repo_url=repo_url,
                pr_number=target_pr,
                status_callback=update_status,
                skip_ai=current_mode["skip_ai"],
                notify_github=current_mode["notify_github"],
                max_pages=int(max_pages),
            )

            status.update(label="Process complete!", state="complete", expanded=False)

        with result_container:
            st.divider()
            st.subheader("📸 Visual Regression")

            img_col1, img_col2 = st.columns(2)

            with img_col1:
                st.markdown("#### 🟥 Before (Base)")
                if results["main_screenshot"]:
                    st.image(results["main_screenshot"], caption="Base branch UI")
                else:
                    st.error("Screenshot failed.")

            with img_col2:
                st.markdown("#### 🟩 After (Head)")
                if results["feature_screenshot"]:
                    st.image(results["feature_screenshot"], caption="Head branch UI")
                else:
                    st.error("Screenshot failed.")

            if results.get("partial_screenshot"):
                st.markdown("#### 🔍 Focus area (AI detected)")
                st.image(results["partial_screenshot"], caption="Auto-focused selector")

            st.divider()

            if current_mode["skip_ai"]:
                st.info("ℹ️ AI analysis skipped (Screenshots Only mode).")
            else:
                st.subheader("🧐 AI Analysis Report")
                if results["ai_report"]:
                    st.markdown(f'<div class="report-box">{results["ai_report"]}</div>', unsafe_allow_html=True)
                    if current_mode["notify_github"]:
                        st.success("🚀 Report and images were posted to the GitHub PR.")
                else:
                    st.warning("Report generation failed.")

            with st.expander("Show raw Git diff"):
                st.code(results["git_diff"], language="diff")

            page_screens = results.get("page_screenshots") or {}
            page_order = results.get("page_order") or list(page_screens.keys())
            if page_order:
                st.divider()
                st.subheader("🗂 Per-Page Views")
                for idx, label in enumerate(page_order):
                    page = page_screens.get(label)
                    if not page:
                        continue
                    with st.expander(f"Page: {label}", expanded=(idx == 0)):
                        b_col, f_col = st.columns(2)
                        base_full = page.get("base", {}).get("full")
                        feature_full = page.get("feature", {}).get("full")
                        feature_focus = page.get("feature", {}).get("partial")

                        with b_col:
                            st.markdown("**Base**")
                            if base_full:
                                st.image(base_full, caption=f"{label} (Base)")
                            else:
                                st.warning("No base screenshot")

                        with f_col:
                            st.markdown("**Feature**")
                            if feature_full:
                                st.image(feature_full, caption=f"{label} (Feature)")
                            else:
                                st.warning("No feature screenshot")

                        if feature_focus:
                            st.image(feature_focus, caption="Focus (Feature)", width=400)

    except Exception as exec_err:  # pragma: no cover
        st.error(f"An error occurred: {exec_err}")
        st.code(traceback.format_exc())
else:
    if not repo_url:
        st.info("Enter a repository URL to start the analysis.")

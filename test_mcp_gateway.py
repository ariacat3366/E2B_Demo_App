#!/usr/bin/env python3
"""
E2B MCP Gateway のテストスクリプト
"""
import os
from dotenv import load_dotenv
from agent_logic import DiffVisionAgent

load_dotenv()

def test_mcp_gateway():
    print("=" * 60)
    print("E2B MCP Gateway テスト")
    print("=" * 60)
    
    # エージェントを初期化
    try:
        agent = DiffVisionAgent()
        print(f"✅ エージェント初期化成功")
        print(f"   - E2B MCP Gateway 使用: {agent.use_e2b_mcp_gateway}")
        print(f"   - GitHub PAT 設定: {'あり' if agent.mcp_github_pat else 'なし'}")
    except Exception as e:
        print(f"❌ エージェント初期化失敗: {e}")
        return
    
    # テスト用のリポジトリとPR番号
    repo_url = "https://github.com/ariacat3366/Demo_Repository_for_Build_MCP_Agents"
    pr_number = 1  # 実際のPR番号に変更してください
    
    print(f"\n📋 テスト対象:")
    print(f"   - リポジトリ: {repo_url}")
    print(f"   - PR番号: {pr_number}")
    
    # analyze_pr を実行（Screenshots Only モード）
    print(f"\n🚀 PR解析を開始...")
    try:
        results = agent.analyze_pr(
            repo_url=repo_url,
            pr_number=pr_number,
            skip_ai=True,  # AI解析をスキップ（スクリーンショットのみ）
            notify_github=False,
            max_pages=1
        )
        
        print(f"\n✅ PR解析完了")
        print(f"   - ログ件数: {len(results.get('logs', []))}")
        print(f"   - Git diff 取得: {'成功' if results.get('git_diff') else '失敗'}")
        print(f"   - Base スクリーンショット: {'成功' if results.get('main_screenshot') else '失敗'}")
        print(f"   - Feature スクリーンショット: {'成功' if results.get('feature_screenshot') else '失敗'}")
        
        # ログを表示
        print(f"\n📝 実行ログ:")
        for log in results.get('logs', []):
            print(f"   {log}")
            
    except Exception as e:
        print(f"\n❌ PR解析失敗: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_mcp_gateway()


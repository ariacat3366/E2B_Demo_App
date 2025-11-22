import os
from e2b_code_interpreter import Sandbox

# APIキーをセット（本来は環境変数に入れるのが安全ですが、テストなのでここに書きます）
# 例: os.environ["E2B_API_KEY"] = "e2b_12345..."
os.environ["E2B_API_KEY"] = "e2b_39e8c928b8068804564a7f2b02c6b70fb6e23c20"


def run_simple_demo():
    print("🚀 サンドボックスを起動中...")

    # E2Bのサンドボックス（クラウド上の仮想環境）を立ち上げます
    # NOTE: Sandboxの直接インスタンス化は非推奨になったため、create()を使用する
    with Sandbox.create() as sandbox:
        print("✅ サンドボックスが起動しました！")

        # 実行させたいPythonコード
        # （わざと計算やリスト操作をさせて、Pythonが動いているか確認します）
        code_to_run = """
x = 10
y = 20
result = x * y
print(f"計算結果: {x} x {y} = {result}")
print("これはE2Bのクラウド環境からこんにちは！")
"""

        print("📤 コードを送信して実行中...")

        # コードを実行！
        execution = sandbox.run_code(code_to_run)

        # 結果を表示
        if execution.error:
            print("❌ エラーが発生しました:", execution.error)
        else:
            print("\n--- 実行結果 (クラウドからの返信) ---")
            stdout_text = "".join(execution.logs.stdout).rstrip()
            print(stdout_text or "(標準出力はありません)")
            print("-----------------------------------")


if __name__ == "__main__":
    run_simple_demo()

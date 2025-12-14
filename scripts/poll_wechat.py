import time
import subprocess
import argparse
import sys
import os

def run_auto_glm(target_user, webhook_url, model_args):
    """
    Runs the Auto-GLM agent with a specific instruction to check for messages.
    """
    # Construct the natural language prompt
    prompt = (
        f"打开微信。在消息列表中寻找用户'{target_user}'。"
        f"仔细观察该用户的行是否有红色未读消息圆点（红点或数字）。"
        f"1. 如果没有红点，直接 finish 结束任务，不要做其他操作。"
        f"2. 如果有红点，点击进入聊天界面。"
        f"3. 进入后，请找到最后一条由'我'发送的消息（通常在右侧，绿色背景）。"
        f"4. 提取在该消息之后的所有对方发送的消息内容（通常在左侧，白色背景）。如果找不到我的历史消息，就提取屏幕上可见的所有对方发来的新消息。"
        f"5. 将提取的消息内容合并，使用 Call_API 动作发送到 '{webhook_url}' (数据格式: {{'user': '{target_user}', 'content': '.../...'}})。"
        f"6. **根据 Call_API 的执行结果（观察上一步的输出）决定下一步**："
        f"   - 如果 API 调用成功 (Success)，请在微信输入框中回复：'接收到任务，已为您提交任务'。"
        f"   - 如果 API 调用失败 (Failed)，请在微信输入框中回复：'接收到任务，触发失败'。"
        f"7. 发送回复后，finish 结束任务。"
    )

    cmd = [sys.executable, "main.py", prompt] + model_args
    
    print(f"[*] Starting Auto-GLM check for user: {target_user}...")
    try:
        # Run the agent as a subprocess
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # Run from root
        )
        
        if result.returncode == 0:
            print("[*] Agent task completed successfully.")
        else:
            print(f"[!] Agent task failed with code {result.returncode}.")
            print("Stderr:", result.stderr)
            
    except Exception as e:
        print(f"[!] Error running agent: {e}")

def main():
    parser = argparse.ArgumentParser(description="Poll WeChat for messages from a specific user.")
    parser.add_argument("--user", required=True, help="The WeChat username/nickname to monitor.")
    parser.add_argument("--webhook", required=True, help="The URL to send the message content to.")
    parser.add_argument("--interval", type=int, default=900, help="Polling interval in seconds (default: 900s / 15min).")
    
    # Pass-through arguments for the model connection
    parser.add_argument("--base-url", help="Model API Base URL")
    parser.add_argument("--apikey", help="Model API Key")
    parser.add_argument("--model", help="Model Name")
    
    args = parser.parse_args()

    # Collect model args to pass to main.py
    model_args = []
    if args.base_url:
        model_args.extend(["--base-url", args.base_url])
    if args.apikey:
        model_args.extend(["--apikey", args.apikey])
    if args.model:
        model_args.extend(["--model", args.model])

    print(f"[*] Starting Poller. Target: {args.user}, Interval: {args.interval}s")
    
    while True:
        run_auto_glm(args.user, args.webhook, model_args)
        
        print(f"[*] Sleeping for {args.interval} seconds...")
        time.sleep(args.interval)

if __name__ == "__main__":
    main()

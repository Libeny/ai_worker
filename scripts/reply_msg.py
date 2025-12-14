import subprocess
import argparse
import sys
import os

def main():
    parser = argparse.ArgumentParser(description="Send a reply to a WeChat user via Auto-GLM.")
    parser.add_argument("--user", required=True, help="The WeChat username/nickname to reply to.")
    parser.add_argument("--message", required=True, help="The content of the message to send.")
    
    # Pass-through arguments for the model connection
    parser.add_argument("--base-url", help="Model API Base URL")
    parser.add_argument("--apikey", help="Model API Key")
    parser.add_argument("--model", help="Model Name")
    
    args = parser.parse_args()

    # Construct the natural language prompt
    prompt = (
        f"打开微信，找到用户'{args.user}'，并发送以下消息：{args.message}。"
        f"发送完成后结束任务。"
    )

    # Collect model args
    model_args = []
    if args.base_url:
        model_args.extend(["--base-url", args.base_url])
    if args.apikey:
        model_args.extend(["--apikey", args.apikey])
    if args.model:
        model_args.extend(["--model", args.model])

    cmd = [sys.executable, "main.py", prompt] + model_args
    
    print(f"[*] Triggering Auto-GLM to reply to {args.user}...")
    try:
        # Run from project root
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            cwd=project_root
        )
        
        if result.returncode == 0:
            print("[*] Message sent successfully.")
        else:
            print(f"[!] Failed to send message. Code: {result.returncode}")
            print("Stderr:", result.stderr)
            
    except Exception as e:
        print(f"[!] Error executing reply: {e}")

if __name__ == "__main__":
    main()

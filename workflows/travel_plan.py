#!/usr/bin/env python3
"""
Travel planning workflow that drives Auto-GLM (phone agent) to collect guides and price-check
transport/hotels, then produce a report.

Usage examples:
  python workflows/travel_plan.py --to 三亚 --from 北京 --from 上海 --depart-date 2025-05-01 --return-date 2025-05-05
  python workflows/travel_plan.py --to 成都 --note "2大1小 预算有限 想吃美食" --from 深圳
  python workflows/travel_plan.py --base-url http://localhost:8000/v1 --model autoglm-phone-9b --apikey sk-xxx --to 厦门 --from 广州
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_prompt(args: argparse.Namespace) -> str:
    departures = ", ".join(args.from_city) if args.from_city else "未指定（请在应用内选择最近可行出发地）"
    destination = args.to or "未指定目的地（请在应用内选择一个热门目的地并标明假设）"
    depart_date = args.depart_date or "未指定（默认选择最近可行的周末/假期并标明假设）"
    return_date = args.return_date or "未指定（如未给出，按 3-5 天行程假设并说明）"
    nights = args.nights or "未指定（按 3-4 晚假设并说明）"
    travellers = args.travellers or "未指定（按 2 人假设并说明）"
    budget = args.budget or "未指定（以性价比优先，标出估算总价区间）"
    note = args.note or "无其他特别需求"

    prompt = f"""
你是 Auto-GLM 手机代理，需完成一次多出发地的旅行规划和比价，并在聊天中输出最终报告。

用户需求：
- 出发地（可多个）：{departures}
- 目的地：{destination}
- 去程日期：{depart_date}
- 返程日期：{return_date}
- 预计晚数：{nights}
- 人数/画像：{travellers}
- 预算：{budget}
- 其他说明：{note}

任务要求（按顺序执行）：
1) 如信息缺失，请在报告中声明假设（默认最近周末/3-4晚/2人），但仍继续完成搜索与推荐。
2) 打开小红书，搜索“{destination} 旅游 攻略 美食 必玩 必避”，阅读 2-3 篇高质量/近期笔记，提炼：
   - 必去景点/路线、必吃美食、交通方式、避坑提示/旺季排队建议/穿衣和天气提醒。
3) 交通比价：
   - 对每个出发地，分别查询 12306（高铁/火车）与携程（机票/火车），“去程” {depart_date}，“返程” {return_date}。
   - 记录时间、车次/航班号、出发/到达站、时长、价格、退改/行李规则。选出性价比/时间友好的 1-2 个备选。
4) 住宿推荐：
   - 在携程/美团按目的地搜索，筛选 2-3 个住宿（靠近主要景点/地铁），包含价格、位置、评分、可退改信息。
5) 组合方案：
   - 针对每个出发地，给出“交通+住宿”推荐组合，并估算总价；可提供 1) 性价比方案 2) 便捷方案。
6) 输出最终报告（文字即可，不用截图/表格也行，保证清晰）：
   - 总体建议与假设说明
   - 行程建议（分日或分时段）
   - 交通比价摘要（每个出发地的去/返程推荐及价格）
   - 住宿推荐列表（名称/大概价格/位置/退改）
   - 必玩/必吃/避坑提示/天气与穿衣建议
结束后直接在当前聊天回复结果并结束任务。
"""
    return prompt.strip()


def build_cmd(args: argparse.Namespace) -> List[str]:
    prompt = build_prompt(args)

    cmd: List[str] = [sys.executable, "main.py", prompt]

    if args.base_url:
        cmd.extend(["--base-url", args.base_url])
    if args.apikey:
        cmd.extend(["--apikey", args.apikey])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.device_id:
        cmd.extend(["--device-id", args.device_id])
    if args.lang:
        cmd.extend(["--lang", args.lang])
    return cmd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Travel plan workflow launcher")
    parser.add_argument("--to", dest="to", help="目的地")
    parser.add_argument("--from", dest="from_city", action="append", help="出发地，可多次指定")
    parser.add_argument("--depart-date", help="去程日期，如 2025-05-01")
    parser.add_argument("--return-date", help="返程日期，如 2025-05-05")
    parser.add_argument("--nights", help="晚数或行程天数说明")
    parser.add_argument("--travellers", help="人数/画像，如 2大1小")
    parser.add_argument("--budget", help="预算说明或区间")
    parser.add_argument("--note", help="其他说明，若无出发地/日期/目的地，可在此写明")

    parser.add_argument("--base-url", help="Model API base URL")
    parser.add_argument("--apikey", help="Model API key")
    parser.add_argument("--model", help="Model name (default autoglm-phone-9b)")
    parser.add_argument("--device-id", help="ADB device id")
    parser.add_argument("--lang", choices=["cn", "en"], default="cn", help="Prompt language (default cn)")

    return parser.parse_args()


def main():
    args = parse_args()
    cmd = build_cmd(args)

    print(f"[*] Running travel plan workflow with command:\n{' '.join(cmd)}\n")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.stdout:
            print(result.stdout.strip())
        if result.returncode != 0:
            if result.stderr:
                print(result.stderr.strip(), file=sys.stderr)
            sys.exit(result.returncode)
    except Exception as exc:
        print(f"[!] Failed to run travel plan workflow: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

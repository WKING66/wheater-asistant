"""一次性推送：跑一遍生成建议并发邮件。

本地手动测试、云上定时任务（GitHub Actions）都用它，不需要起 HTTP 服务。

用法：
    .venv\\Scripts\\python push_once.py

环境变量（云上跑时用；本地没设就读 settings.json）：
    PUSH_CITY       要查的城市，默认取 settings.json 的
    PUSH_EMAIL_TO   收件邮箱，默认取 settings.json 的
"""
import asyncio
import os

from main import build_briefing, load_settings, send_email


async def run() -> None:
    settings = load_settings()
    city = os.getenv("PUSH_CITY") or settings["city"]
    to = os.getenv("PUSH_EMAIL_TO") or settings["email_to"]
    if not to:
        raise SystemExit("没有收件邮箱：请设 PUSH_EMAIL_TO，或先在页面上填好")

    subject, body = await build_briefing(city)
    send_email(to, subject, body)
    print(f"已推送「{subject}」到 {to}", flush=True)


if __name__ == "__main__":
    asyncio.run(run())

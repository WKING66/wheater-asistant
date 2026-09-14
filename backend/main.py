"""天气穿搭助手 —— LangChain + LangGraph 学习 Demo 后端。

架构（对应 LangGraph 的核心概念，Java 同学可以类比成"状态机 + 依赖注入的工具"）：
    START → agent(LLM) ──有工具调用──▶ ToolNode(执行 get_weather/resolve_city)
             │                                │
             └──────── 没有工具调用 ──▶ END ◀──┘（工具结果再喂回 agent）

一句话：LLM 负责"决定调哪个工具/给出什么参数"，ToolNode 负责"真正去执行"，
两者通过 messages 状态来回传递，直到 LLM 不再需要工具为止（这就是 ReAct 循环）。
"""
import asyncio
import json
import os
import re
import smtplib
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, TypedDict

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel

load_dotenv()

# ---------------------------------------------------------------------------
# 1. 天气数据层：Open-Meteo 免费、免 key。和 LLM 完全解耦，纯 HTTP。
# ---------------------------------------------------------------------------

WMO = {  # 天气现象编码 → 中文
    0: "晴", 1: "基本晴朗", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨",
    65: "大雨", 66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def _get(url: str, params: dict) -> httpx.Response:
    """带重试的 GET。Open-Meteo 是国外源，国内访问偶尔超时，重试 3 次兜底。"""
    last = None
    for _ in range(3):
        try:
            r = httpx.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            last = e
    raise last


def _weather_payload(lat: float, lon: float) -> dict:
    """拉取当前 + 今明两天预报，转成 LLM 和前端都容易读的中文结构。"""
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
        "timezone": "auto", "forecast_days": 2,
    }
    r = _get("https://api.open-meteo.com/v1/forecast", params)
    d = r.json()
    cur = d["current"]
    daily = d["daily"]

    def day(i: int) -> dict:
        return {
            "天气": WMO.get(daily["weather_code"][i], "未知"),
            "最高温": daily["temperature_2m_max"][i],
            "最低温": daily["temperature_2m_min"][i],
            "降水概率": daily["precipitation_probability_max"][i],
            "紫外线指数": daily["uv_index_max"][i],
        }

    return {
        "时区": d["timezone"],
        "当前": {
            "温度": cur["temperature_2m"], "体感": cur["apparent_temperature"],
            "湿度": cur["relative_humidity_2m"], "风速": cur["wind_speed_10m"],
            "天气": WMO.get(cur["weather_code"], "未知"),
        },
        "今天": day(0),
        "明天": day(1),
    }

def _geocode(city: str) -> dict | None:
    """城市名 → 经纬度；找不到返回 None。工具和 HTTP 接口共用。"""
    r = _get(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": city, "count": 1, "language": "zh"},
    )
    results = r.json().get("results")
    if not results:
        return None
    res = results[0]
    return {"name": res["name"], "lat": res["latitude"], "lon": res["longitude"]}

# ---------------------------------------------------------------------------
# 2. 工具层：@tool 装饰器把一个普通函数变成 LLM 可以"调用"的工具。
#    函数签名 + docstring 会自动拼进提示词，告诉模型参数怎么填。
# ---------------------------------------------------------------------------

@tool
def resolve_city(city: str) -> str:
    """把城市名（如"北京"）解析成经纬度。当用户只说城市名、没给坐标时先调用它。"""
    found = _geocode(city)
    if not found:
        return f"没找到城市「{city}」，请换个写法或直接用坐标。"
    return f"{found['name']}：纬度 {found['lat']}，经度 {found['lon']}"


@tool
def get_weather(latitude: float, longitude: float) -> str:
    """获取指定经纬度当前 + 今明两天的天气预报（温度/湿度/风/降水/紫外线）。"""
    return json.dumps(_weather_payload(latitude, longitude), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 3. 图（StateGraph）层：把上面的工具和 LLM 串成一个可循环的"智能体"。
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个贴心的天气穿搭助理。规则：
1. 先拿到天气数据再回答，不要凭空编。用户给城市名就先调 resolve_city，给坐标就直接调 get_weather。
2. 拿到数据后，用简体中文给出：今日天气小结、上衣/下装/鞋/配饰的穿搭建议、出门必备（伞/防晒/外套等）。
3. 回答简洁实用，用 markdown 分点。"""

# DeepSeek 的 API 是 OpenAI 兼容协议，直接用 ChatOpenAI + base_url 即可；
# 换成任何 OpenAI 兼容服务（通义/智谱/Ollama）只改这三个环境变量。
llm = ChatOpenAI(
    model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    # 空 key 会直接抛异常，这里给占位符保证模块能 import（天气/地理接口不依赖 LLM）；
    # 真正调 LLM 的 /api/chat 入口另有显式校验。
    api_key=os.getenv("DEEPSEEK_API_KEY") or "sk-not-configured",
    temperature=0.7,
)


class State(TypedDict):
    # add_messages 是"归并器"：新消息 append 到旧列表，而不是覆盖。
    messages: Annotated[list[BaseMessage], add_messages]


async def agent(state: State, config: RunnableConfig) -> State:
    """agent 节点：让绑定了工具的 LLM 生成回复；它可能直接答，也可能发出工具调用。
    用 astream 逐 token 收。必须把 config 显式传进去，模型调用才会带上图的
    callbacks，astream_events / stream_mode 才能拿到 token 流。"""
    model = llm.bind_tools([get_weather, resolve_city])
    merged = None
    async for chunk in model.astream(
        [("system", SYSTEM_PROMPT), *state["messages"]], config=config
    ):
        merged = chunk if merged is None else merged + chunk
    return {"messages": [merged]}


def should_continue(state: State) -> str:
    """条件边：LLM 最后一条消息里还有待执行的工具调用 → 去 tools；否则 → END。"""
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


checkpointer = MemorySaver()  # 内存版记忆：thread_id 区分不同会话，重启即清空

builder = StateGraph(State)
builder.add_node("agent", agent)
builder.add_node("tools", ToolNode([get_weather, resolve_city]))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
builder.add_edge("tools", "agent")
graph = builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 4. 每日推送层：设置持久化 + 发邮件 + 每天定时任务。全用标准库，不引新依赖。
# ---------------------------------------------------------------------------

SETTINGS_FILE = Path(__file__).parent / "settings.json"
DEFAULT_SETTINGS = {"city": "珠海", "email_to": "2899964923@qq.com", "push_time": "07:00"}
# 云上文件系统往往是临时的（重新部署就重置），所以允许用环境变量覆盖页面上改的设置
ENV_KEYS = {"city": "PUSH_CITY", "email_to": "PUSH_EMAIL_TO", "push_time": "PUSH_TIME"}


def load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:
        saved = {}
    env = {k: v for k, key in ENV_KEYS.items() if (v := os.getenv(key))}
    return {**DEFAULT_SETTINGS, **saved, **env}


def save_settings(patch: dict) -> dict:
    merged = {**load_settings(), **patch}
    SETTINGS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
    return merged


def _md_to_html(text: str) -> str:
    """极简 markdown → 邮件 HTML：只处理标题 / 加粗 / 无序列表，够本项目的输出用。
    邮件客户端对 CSS 支持参差，所以样式一律写成内联 style，不用 <style> 块。"""
    out: list[str] = []
    in_ul = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        is_li = esc.lstrip().startswith("- ")
        if is_li and not in_ul:
            out.append('<ul style="margin:10px 0;padding-left:20px">')
            in_ul = True
        elif not is_li and in_ul:
            out.append("</ul>")
            in_ul = False
        if is_li:
            out.append(f'<li style="margin:6px 0;line-height:1.7">{esc.lstrip()[2:]}</li>')
        elif esc.startswith("### "):
            out.append(f'<h3 style="margin:16px 0 6px;font-size:15px;color:#111827">{esc[4:]}</h3>')
        elif esc.startswith("## "):
            out.append(f'<h2 style="margin:20px 0 8px;font-size:16px;color:#1d4ed8">{esc[3:]}</h2>')
        elif esc.startswith("# "):
            out.append(f'<h1 style="margin:0 0 10px;font-size:19px;color:#111827">{esc[2:]}</h1>')
        else:
            out.append(f'<p style="margin:8px 0;line-height:1.75">{esc}</p>')
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _email_html(subject: str, body_md: str) -> str:
    """套一层卡片式外壳，手机上看着舒服点。"""
    return f"""\
<div style="margin:0;padding:24px 12px;background:#eef2f7;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.06)">
    <div style="background:linear-gradient(135deg,#3b82f6,#6366f1);padding:22px 24px;color:#fff">
      <div style="font-size:19px;font-weight:700">🌤️ {subject}</div>
      <div style="margin-top:6px;font-size:13px;opacity:.9">{datetime.now():%Y-%m-%d}</div>
    </div>
    <div style="padding:20px 24px;color:#374151;font-size:14.5px">
      {_md_to_html(body_md)}
    </div>
    <div style="padding:14px 24px;background:#f9fafb;color:#9ca3af;font-size:12px;text-align:center">
      由「天气穿搭助手」自动发送 · 天气数据来自 Open-Meteo
    </div>
  </div>
</div>"""


def send_email(to: str, subject: str, body_md: str) -> None:
    """标准库 smtplib 发信。SMTP 账号密码放 .env（授权码属于敏感信息）。"""
    host, user, pwd = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    port = int(os.getenv("SMTP_PORT", "465"))
    if not (host and user and pwd):
        raise RuntimeError("未配置 SMTP_HOST / SMTP_USER / SMTP_PASS（见 backend/.env）")

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body_md)  # 纯文本兜底：极老的客户端看不到 HTML 时显示这个
    msg.add_alternative(_email_html(subject, body_md), subtype="html")  # 带样式的版本

    if port == 465:  # 465 走 SSL，587 走 STARTTLS
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)


async def build_briefing(city: str) -> tuple[str, str]:
    """跑一遍 LangGraph 智能体，生成某城市的今日穿搭建议。返回 (主题, 正文)。"""
    geo = await asyncio.to_thread(_geocode, city)  # 同步 HTTP 丢线程里，别堵事件循环
    if not geo:
        raise RuntimeError(f"找不到城市「{city}」")
    result = await graph.ainvoke(
        {"messages": [("user",
            f"[我的位置：纬度 {geo['lat']}，经度 {geo['lon']}]\n"
            "帮我看看今天天气，给出穿搭和出门建议")]},
        config={"configurable": {"thread_id": f"push-{date.today()}"}},
    )
    return f"今日穿搭建议 · {geo['name']}", result["messages"][-1].content


async def run_push() -> dict:
    """生成并发送当天推送。接口和定时任务共用，返回 {ok, msg}。"""
    settings = load_settings()
    if not settings.get("email_to"):
        return {"ok": False, "msg": "还没设置收件邮箱，请在页面上填写"}
    try:
        subject, content = await build_briefing(settings["city"])
        await asyncio.to_thread(send_email, settings["email_to"], subject, content)
        return {"ok": True, "msg": f"已推送「{subject}」到 {settings['email_to']}"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


async def daily_push_loop() -> None:
    """每天到 push_time 推送一次。用 asyncio.sleep 睡到下一个时间点，不需要 APScheduler。"""
    while True:
        now = datetime.now()
        try:
            hh, mm = (int(x) for x in load_settings()["push_time"].split(":"))
        except Exception:
            hh, mm = 7, 0
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # 今天已过点，排到明天
        await asyncio.sleep((target - now).total_seconds())
        print(f"[每日推送] {datetime.now():%Y-%m-%d %H:%M} ->", await run_push(), flush=True)
        await asyncio.sleep(61)  # 跨过这一分钟，避免同一分钟重复触发


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 云上若把推送交给 GitHub Actions，把 ENABLE_SCHEDULER 设成 0，免得同一时刻发两封
    task = asyncio.create_task(daily_push_loop()) if os.getenv("ENABLE_SCHEDULER", "1") != "0" else None
    yield
    if task:
        task.cancel()


# ---------------------------------------------------------------------------
# 5. HTTP 层：FastAPI 接口。CORS 放行给本地 Vite 前端。
# ---------------------------------------------------------------------------

app = FastAPI(title="天气穿搭助手", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/weather")
def weather(lat: float, lon: float):
    """前端天气卡片直接用（不走 LLM，秒回）。"""
    return _weather_payload(lat, lon)


@app.get("/api/geocode")
def geocode(city: str):
    """城市名 → {name, lat, lon}，前端切换城市用。"""
    found = _geocode(city)
    if not found:
        raise HTTPException(404, f"没找到城市「{city}」")
    return found


class SettingsIn(BaseModel):
    city: str | None = None
    email_to: str | None = None
    push_time: str | None = None


@app.get("/api/settings")
def get_settings():
    """读当前推送设置（默认城市珠海）。"""
    return load_settings()


@app.post("/api/settings")
def put_settings(body: SettingsIn):
    """只覆盖传进来的字段，其余保持不动。"""
    return save_settings({k: v for k, v in body.model_dump().items() if v is not None})


@app.post("/api/push")
async def push_now():
    """立即推送一封，用来验证邮箱配置是否正确。"""
    return await run_push()


class ChatIn(BaseModel):
    message: str
    thread_id: str = "default"
    lat: float | None = None
    lon: float | None = None


def _sse(obj: dict) -> str:
    """把一个事件编码成 SSE 的 data 行（JSON，避免 token 里的换行/引号破坏格式）。"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(body: ChatIn):
    """SSE 流式：token 一个接一个推给前端，而不是等全部生成完才返回。"""
    async def gen():
        if not os.getenv("DEEPSEEK_API_KEY"):
            yield _sse({"delta": "⚠️ 后端还没配 DEEPSEEK_API_KEY（见 backend/.env）。"})
            yield _sse({"done": True})
            return

        prefix = ""
        if body.lat is not None and body.lon is not None:
            prefix = f"[我的位置：纬度 {body.lat}，经度 {body.lon}]\n"

        # saw_tool：这次请求是否已经跑过工具。跑过之后，LLM 的下一段输出才是最终回答；
        # 之前的内容（如"我先查下天气"这类内心独白）先缓冲，一旦工具开始执行就丢弃。
        # ponytail: 多轮工具且每轮前都有独白时，第二轮独白会漏出；本 demo 单次查天气用不上。
        saw_tool = False
        pending: list[str] = []

        async for event in graph.astream_events(
            {"messages": [("user", prefix + body.message)]},
            config={"configurable": {"thread_id": body.thread_id}},
            version="v2",
        ):
            etype = event["event"]
            if etype == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if chunk.content:
                    if saw_tool:
                        if not chunk.tool_calls:
                            yield _sse({"delta": chunk.content})
                    else:
                        pending.append(chunk.content)
            elif etype == "on_tool_start":
                saw_tool = True
                pending.clear()  # 丢弃工具调用前的独白

        # 图结束仍没跑工具（比如纯闲聊"你好"）：pending 里就是最终回答，一次性 flush。
        for token in pending:
            yield _sse({"delta": token})
        yield _sse({"done": True})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 6. 前端静态文件：生产环境把构建产物挂到根路径，前后端同域，不用再配代理。
#    必须放在所有 /api 路由之后注册，否则连 API 请求也会被它吃掉。
# ---------------------------------------------------------------------------

WEB_DIST = Path(os.getenv("WEB_DIST") or (Path(__file__).parent.parent / "frontend" / "dist"))
if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    # 本地默认 127.0.0.1:8000；云上平台一般通过 PORT 环境变量指定端口
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))

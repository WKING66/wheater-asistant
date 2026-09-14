<script setup>
import { ref, onMounted } from 'vue'

const API = '/api'
const lat = ref(null)
const lon = ref(null)
const cityName = ref('')
const weather = ref(null)
const loading = ref(false)
const error = ref('')
const chat = ref([]) // {role, text}
const input = ref('')
const sending = ref(false)
const threadId = 'demo-' + Math.random().toString(36).slice(2)
const msgsEl = ref(null)
const TYPING_MS = 30 // 打字机节奏：每放一个 token 隔多少毫秒，越大越慢越明显

// ---- 推送设置 ----
const settings = ref({ city: '珠海', email_to: '2899964923@qq.com', push_time: '07:00' })
const showSettings = ref(false)
const saving = ref(false)
const pushing = ref(false)
const settingsMsg = ref('')
const pushMsg = ref('')

// 极简 markdown：先转义 HTML（安全），再处理 **加粗** 和换行。够用，不引第三方库。
function render(text) {
  const esc = text.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
  return esc.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>')
}

// 按天气文案挑个 emoji，纯装饰
function wxIcon(t) {
  if (!t) return '🌡️'
  if (t.includes('雷')) return '⛈️'
  if (t.includes('雪')) return '❄️'
  if (t.includes('雨')) return '🌧️'
  if (t.includes('雾')) return '🌫️'
  if (t.includes('晴')) return '☀️'
  if (t.includes('云') || t.includes('阴')) return '⛅'
  return '🌤️'
}

function scrollDown() {
  requestAnimationFrame(() => { if (msgsEl.value) msgsEl.value.scrollTop = msgsEl.value.scrollHeight })
}

async function loadWeather() {
  if (lat.value == null) return
  loading.value = true
  error.value = ''
  try {
    const r = await fetch(`${API}/weather?lat=${lat.value}&lon=${lon.value}`)
    if (!r.ok) throw new Error('天气接口失败')
    weather.value = await r.json()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

// 按城市名加载：解析坐标 → 拉天气 → 自动推送一条建议
async function loadCity(city) {
  if (!city) return
  error.value = ''
  try {
    const r = await fetch(`${API}/geocode?city=${encodeURIComponent(city)}`)
    if (!r.ok) throw new Error('没找到城市：' + city)
    const g = await r.json()
    lat.value = g.lat
    lon.value = g.lon
    await loadWeather()
    push()
  } catch (e) {
    error.value = e.message
  }
}

function locate() {
  error.value = ''
  if (!navigator.geolocation) return (error.value = '浏览器不支持定位，请手动输入城市')
  navigator.geolocation.getCurrentPosition(
    pos => { lat.value = pos.coords.latitude; lon.value = pos.coords.longitude; loadWeather(); push() },
    () => (error.value = '定位被拒绝，请手动输入城市或改用默认城市'),
    { timeout: 8000 },
  )
}

function switchCity() {
  const c = cityName.value.trim()
  if (!c) return
  cityName.value = ''
  loadCity(c)
}

async function push() {
  // 页面加载拿到坐标后，自动"推送"一条当日建议（相当于定时推送的预览）
  if (lat.value == null || chat.value.length) return
  send('帮我看看今天天气，给出穿搭和出门建议')
}

// ---- 设置相关 ----
async function openSettings() {
  settingsMsg.value = ''
  pushMsg.value = ''
  try {
    const r = await fetch(`${API}/settings`)
    settings.value = await r.json()
  } catch (_) { /* 用本地默认值兜底 */ }
  showSettings.value = true
}

async function saveSettings() {
  saving.value = true
  settingsMsg.value = ''
  try {
    const r = await fetch(`${API}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings.value),
    })
    settings.value = await r.json()
    settingsMsg.value = `✅ 已保存：每天 ${settings.value.push_time} 推送到 ${settings.value.email_to || '（还没填邮箱）'}`
  } catch (e) {
    settingsMsg.value = '❌ 保存失败：' + e.message
  } finally {
    saving.value = false
  }
}

async function testPush() {
  pushing.value = true
  pushMsg.value = ''
  try {
    // 先把表单里的值存下来，再触发推送，保证测的就是你填的内容
    await fetch(`${API}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings.value),
    })
    const r = await fetch(`${API}/push`, { method: 'POST' })
    const d = await r.json()
    pushMsg.value = (d.ok ? '✅ ' : '❌ ') + d.msg
  } catch (e) {
    pushMsg.value = '❌ ' + e.message
  } finally {
    pushing.value = false
  }
}

// ---- 对话（SSE 流式 + 打字机）----
async function send(text) {
  const msg = (text ?? input.value).trim()
  if (!msg || sending.value) return
  input.value = ''
  chat.value.push({ role: 'user', text: msg })
  chat.value.push({ role: 'assistant', text: '' }) // 先占位
  // 关键：从响应式数组里取引用再改，改原始对象不会触发 Vue 重渲染。
  const aiMsg = chat.value[chat.value.length - 1]
  sending.value = true

  const queue = [] // 收到的 token 先排队
  let eof = false  // SSE 流是否已结束

  // 打字机：以固定节奏把队列里的 token 逐个放进响应式 text，让"流"肉眼可见。
  // 上游（DeepSeek）可能把 token 一次性吐出来，这里负责把它摊开成一字一字。
  const reveal = (async () => {
    while (true) {
      if (queue.length) {
        aiMsg.text += queue.shift()
        scrollDown()
        await new Promise(r => setTimeout(r, TYPING_MS))
      } else if (eof) {
        break
      } else {
        await new Promise(r => setTimeout(r, 10))
      }
    }
  })()

  try {
    const r = await fetch(`${API}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg, thread_id: threadId, lat: lat.value, lon: lon.value }),
    })
    if (!r.ok || !r.body) throw new Error('HTTP ' + r.status)

    // 读 SSE 流，解析 data: {"delta":"..."}，token 只入队，由打字机负责显示
    const reader = r.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let i
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const evt = buf.slice(0, i)
        buf = buf.slice(i + 2)
        for (const line of evt.split('\n')) {
          if (!line.startsWith('data:')) continue
          try {
            const obj = JSON.parse(line.slice(5).trim())
            if (obj.delta) queue.push(obj.delta)
          } catch (_) { /* 忽略半截/坏行 */ }
        }
      }
    }
  } catch (e) {
    if (!aiMsg.text) aiMsg.text = '请求失败：' + e.message
  } finally {
    eof = true
    await reveal // 等队列全部放完再收尾
    if (!aiMsg.text) aiMsg.text = '(空回复)'
    sending.value = false
  }
}

onMounted(async () => {
  // 用设置里的默认城市（默认珠海）初始化，而不是强制要定位权限
  try {
    const r = await fetch(`${API}/settings`)
    settings.value = await r.json()
  } catch (_) { /* 后端没起就用本地默认值 */ }
  await loadCity(settings.value.city || '珠海')
})
</script>

<template>
  <div class="page">
    <div class="wrap">
      <header>
        <div class="brand">
          <span class="logo">🌤️</span>
          <div>
            <h1>天气穿搭助手</h1>
            <p class="tagline">每天一句话，告诉你穿什么、带什么</p>
          </div>
        </div>
        <button class="icon-btn" title="推送设置" @click="openSettings">⚙️</button>
      </header>

      <div class="toolbar">
        <input v-model="cityName" placeholder="切换城市，如：上海" @keyup.enter="switchCity" />
        <button class="ghost" @click="switchCity">切换</button>
        <button class="ghost" @click="locate">📍 定位</button>
      </div>

      <p v-if="error" class="err">{{ error }}</p>

      <section v-if="weather" class="card">
        <div class="card-head">
          <span class="cur-icon">{{ wxIcon(weather['当前']['天气']) }}</span>
          <div>
            <div class="cur-temp">{{ weather['当前']['温度'] }}<small>°C</small></div>
            <div class="cur-desc">{{ weather['当前']['天气'] }} · 体感 {{ weather['当前']['体感'] }}°C</div>
          </div>
          <div class="cur-meta">
            <span v-if="loading">刷新中…</span>
            <span v-else>湿度 {{ weather['当前']['湿度'] }}%<br />风速 {{ weather['当前']['风速'] }} km/h</span>
          </div>
        </div>
        <div class="days">
          <div v-for="k in ['今天', '明天']" :key="k" class="day">
            <div class="dname">{{ wxIcon(weather[k]['天气']) }} {{ k }}</div>
            <div class="dwx">{{ weather[k]['天气'] }}</div>
            <div class="dtemp">{{ weather[k]['最低温'] }} ~ {{ weather[k]['最高温'] }}°C</div>
            <div class="sub">降水 {{ weather[k]['降水概率'] }}% · 紫外线 {{ weather[k]['紫外线指数'] }}</div>
          </div>
        </div>
      </section>

      <section class="chat">
        <div class="msgs" ref="msgsEl">
          <div v-for="(m, i) in chat" :key="i" :class="['msg', m.role]">
            <div class="who">{{ m.role === 'user' ? '你' : '🤖 助理' }}</div>
            <div class="body">
              <template v-if="m.text"><span v-html="render(m.text)"></span></template>
              <span v-else class="dim">思考中…</span>
              <span v-if="m.role === 'assistant' && sending && i === chat.length - 1" class="cursor">▍</span>
            </div>
          </div>
        </div>
        <div class="send">
          <input v-model="input" placeholder="比如：明天降温吗？需要带伞吗？" @keyup.enter="send()" />
          <button :disabled="sending" @click="send()">发送</button>
        </div>
      </section>
    </div>

    <!-- 设置弹窗 -->
    <div v-if="showSettings" class="overlay" @click.self="showSettings = false">
      <div class="modal">
        <h3>⚙️ 每日推送设置</h3>
        <label>
          <span>默认城市</span>
          <input v-model="settings.city" placeholder="如：珠海" />
        </label>
        <label>
          <span>收件邮箱</span>
          <input v-model="settings.email_to" placeholder="you@example.com" />
        </label>
        <label>
          <span>推送时间</span>
          <input type="time" v-model="settings.push_time" />
        </label>
        <p class="hint">发信用的 SMTP 账号在 <code>backend/.env</code> 里配置（填授权码，不是登录密码）。</p>
        <p v-if="settingsMsg" class="ok">{{ settingsMsg }}</p>
        <p v-if="pushMsg" class="ok">{{ pushMsg }}</p>
        <div class="modal-actions">
          <button class="ghost" :disabled="pushing" @click="testPush">
            {{ pushing ? '发送中…' : '📨 立即推送测试' }}
          </button>
          <button :disabled="saving" @click="saveSettings">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif;
  color: #1f2937;
  background: linear-gradient(160deg, #eef4ff 0%, #e8eefb 40%, #f4f0ff 100%);
  min-height: 100vh;
}
.page { min-height: 100vh; padding: 28px 16px 64px; }
.wrap { max-width: 720px; margin: 0 auto; }

/* ---------- 头部 ---------- */
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
.brand { display: flex; align-items: center; gap: 12px; }
.logo {
  font-size: 28px; width: 52px; height: 52px; display: grid; place-items: center;
  background: #fff; border-radius: 16px; box-shadow: 0 6px 18px rgba(59, 130, 246, .18);
}
h1 { font-size: 21px; margin: 0; letter-spacing: .3px; }
.tagline { margin: 3px 0 0; font-size: 12.5px; color: #7b8794; }
.icon-btn {
  font-size: 18px; width: 44px; height: 44px; border-radius: 14px; padding: 0;
  background: #fff; color: #374151; box-shadow: 0 4px 14px rgba(31, 41, 55, .10);
}
.icon-btn:hover { transform: translateY(-1px); }

button {
  padding: 9px 16px; border: 0; border-radius: 11px; background: #2563eb; color: #fff;
  cursor: pointer; font-size: 14px; font-weight: 500; transition: .15s;
}
button:hover:not(:disabled) { background: #1d4ed8; transform: translateY(-1px); }
button:disabled { opacity: .5; cursor: default; }
button.ghost { background: #fff; color: #2563eb; box-shadow: inset 0 0 0 1px #dbe3f0; }
button.ghost:hover:not(:disabled) { background: #f2f6ff; }

/* ---------- 工具栏 ---------- */
.toolbar { display: flex; gap: 8px; margin-bottom: 16px; }
.toolbar input { flex: 1; }
input {
  padding: 10px 13px; border: 1px solid #dbe3f0; border-radius: 11px; font-size: 14px;
  background: #fff; outline: none; transition: .15s; font-family: inherit;
}
input:focus { border-color: #93b4f7; box-shadow: 0 0 0 3px rgba(37, 99, 235, .12); }
.err { color: #dc2626; font-size: 13.5px; }

/* ---------- 天气卡片 ---------- */
.card {
  background: linear-gradient(135deg, #3b82f6 0%, #6366f1 55%, #8b5cf6 100%);
  color: #fff; border-radius: 22px; padding: 22px 24px; margin-bottom: 18px;
  box-shadow: 0 14px 34px rgba(79, 70, 229, .28);
}
.card-head { display: flex; align-items: center; gap: 16px; }
.cur-icon { font-size: 44px; line-height: 1; filter: drop-shadow(0 3px 6px rgba(0,0,0,.18)); }
.cur-temp { font-size: 46px; font-weight: 700; line-height: 1; }
.cur-temp small { font-size: 20px; font-weight: 500; opacity: .85; }
.cur-desc { margin-top: 6px; font-size: 13.5px; opacity: .92; }
.cur-meta { margin-left: auto; text-align: right; font-size: 12.5px; opacity: .88; line-height: 1.7; }
.days { display: flex; gap: 12px; margin-top: 18px; }
.day {
  flex: 1; background: rgba(255, 255, 255, .16); border: 1px solid rgba(255, 255, 255, .22);
  border-radius: 15px; padding: 13px 15px; backdrop-filter: blur(6px);
}
.dname { font-weight: 600; font-size: 13.5px; margin-bottom: 6px; }
.dwx { font-size: 14px; }
.dtemp { font-size: 15px; font-weight: 600; margin-top: 2px; }
.sub { opacity: .82; font-size: 11.5px; margin-top: 5px; }

/* ---------- 对话 ---------- */
.chat { background: #fff; border-radius: 22px; padding: 18px; box-shadow: 0 8px 26px rgba(31, 41, 55, .08); }
.msgs { display: flex; flex-direction: column; gap: 14px; min-height: 130px; max-height: 430px; overflow-y: auto; padding-right: 4px; }
.msgs::-webkit-scrollbar { width: 6px; }
.msgs::-webkit-scrollbar-thumb { background: #dbe3f0; border-radius: 3px; }
.msg { max-width: 88%; }
.msg .who { font-size: 11.5px; color: #8b95a5; margin-bottom: 4px; }
.msg .body {
  background: #f4f6fb; padding: 11px 15px; border-radius: 14px 14px 14px 4px;
  line-height: 1.68; font-size: 14.5px; word-break: break-word;
}
.msg.user { align-self: flex-end; }
.msg.user .who { text-align: right; }
.msg.user .body { background: linear-gradient(135deg, #2563eb, #4f46e5); color: #fff; border-radius: 14px 14px 4px 14px; }
.dim { color: #9ca3af; }
.cursor { display: inline-block; margin-left: 2px; color: #2563eb; animation: blink 1s steps(1) infinite; }
.msg.user .cursor { color: #fff; }
@keyframes blink { 50% { opacity: 0; } }

.send { display: flex; gap: 9px; margin-top: 16px; }
.send input { flex: 1; }

/* ---------- 设置弹窗 ---------- */
.overlay {
  position: fixed; inset: 0; background: rgba(17, 24, 39, .45); backdrop-filter: blur(3px);
  display: grid; place-items: center; padding: 20px; z-index: 50;
}
.modal {
  background: #fff; border-radius: 20px; padding: 24px; width: 100%; max-width: 420px;
  box-shadow: 0 20px 60px rgba(17, 24, 39, .28);
}
.modal h3 { margin: 0 0 18px; font-size: 17px; }
.modal label { display: block; margin-bottom: 14px; }
.modal label span { display: block; font-size: 12.5px; color: #6b7280; margin-bottom: 5px; }
.modal label input { width: 100%; }
.hint { font-size: 12px; color: #8b95a5; line-height: 1.6; margin: 4px 0 14px; }
.hint code { background: #f1f5f9; padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }
.ok { font-size: 13px; color: #2563eb; margin: 0 0 12px; line-height: 1.6; }
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; }

@media (max-width: 520px) {
  .days { flex-direction: column; }
  .cur-meta { display: none; }
}
</style>

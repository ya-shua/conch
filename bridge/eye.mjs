/**
 * 眼睛的桥：把「留影」的看图能力借给海螺用。
 *
 * 为什么需要这个文件
 * -----------------
 * 留影自己是一个 Node 项目，海螺的插件是 Python。两边不是不能直接说话，
 * 是没必要为此让留影常驻一个 HTTP 服务 —— 这张图看完就完了。
 * 所以这里只做一件事：给一个图链，返回一句描述，然后退出。
 *
 * 用法： node eye.mjs <图片URL>
 * 输出： {"ok":true,"desc":"…","points":["…"]} 或 {"ok":false,"error":"…"}
 *
 * 复用留影自己的取图与描述代码，不复制它的逻辑 —— 它改模型、改提示词，这里跟着变。
 */
import { pathToFileURL } from 'node:url'

const ROOT = process.env.EYE_ROOT || ''
const url = process.argv[2]

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n')
}

if (!url) {
  out({ ok: false, error: '没给图链' })
  process.exit(0)
}

if (!ROOT) {
  out({ ok: false, error: '没配 EYE_ROOT（留影项目的绝对路径）' })
  process.exit(0)
}

try {
  const vision = await import(pathToFileURL(`${ROOT}/src/ring-vision.mjs`).href)
  const describe = await import(pathToFileURL(`${ROOT}/src/describe.mjs`).href)

  const cfg = vision.loadVisionConfig({})
  // 海螺可以自带视觉模型凭据（EYE_*），没给就用留影面板里配的那份
  const baseUrl = process.env.EYE_BASE_URL || cfg.baseUrl
  const model = process.env.EYE_MODEL || cfg.model
  const apiKey = process.env.EYE_API_KEY || cfg.apiKey

  const img = await vision.fetchImage(url, {
    maxBytes: cfg.maxBytes || 8 * 1024 * 1024,
    timeoutMs: cfg.timeoutMs || 45000,
    fetchImpl: fetch,
  })

  const res = await describe.describeImage({
    baseUrl,
    model,
    apiKey,
    imageBase64: img.base64,
    mime: img.mime,
    fetchImpl: fetch,
    timeoutMs: cfg.timeoutMs || 45000,
  })

  out({ ok: true, desc: res.desc || '', points: res.points || [], model: res.model || model })
} catch (err) {
  out({ ok: false, error: String(err && err.message ? err.message : err).slice(0, 300) })
}
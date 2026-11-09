/**
 * 眼睛的桥：聊天里有人发图时，给海螺一句描述。
 *
 * 两条路
 * ------
 * 1. plugin（默认，留影装了就用）：借留影的取图 + 描述代码，它改模型/提示词这里跟着变；
 * 2. direct（海螺自己接）：留影不在、或 EYE_ENGINE=direct 时，直接用海螺配的 key 调视觉模型。
 *
 * 用法： node eye.mjs <图片URL 或 本地文件路径>
 * 输出： {"ok":true,"desc":"…","points":["…"],"model":"…"} 或 {"ok":false,"error":"…"}
 */
import { pathToFileURL } from 'node:url'

const ROOT = process.env.EYE_ROOT || ''
const ENGINE = String(process.env.EYE_ENGINE || 'auto').toLowerCase()
const target = process.argv[2]

const SYSTEM =
  '看这张图，回答两件事：\n' +
  '1) 一句人话：图里是什么、发生了什么（别堆形容词）；\n' +
  '2) 2 到 4 个要点：值得注意的细节（文字、表情、动作）。\n' +
  '只输出 JSON：{"desc":"…","points":["…","…"]}'

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n')
}

if (!target) {
  out({ ok: false, error: '没给图链' })
  process.exit(0)
}

try {
  const usePlugin = ROOT && ENGINE !== 'direct'
  const vision = await import('./vision.mjs')

  if (usePlugin) {
    const ring = await import(pathToFileURL(`${ROOT}/src/ring-vision.mjs`).href)
    const describe = await import(pathToFileURL(`${ROOT}/src/describe.mjs`).href)
    const cfg0 = ring.loadVisionConfig({})
    const baseUrl = process.env.EYE_BASE_URL || cfg0.baseUrl
    const model = process.env.EYE_MODEL || cfg0.model
    const apiKey = process.env.EYE_API_KEY || cfg0.apiKey
    const maxBytes = cfg0.maxBytes || 8 * 1024 * 1024
    const img = /^https?:\/\//i.test(target)
      ? await ring.fetchImage(target, { maxBytes, timeoutMs: cfg0.timeoutMs || 45000, fetchImpl: fetch })
      : await vision.loadImage(target, { ...vision.config(), maxBytes })
    const res = await describe.describeImage({
      baseUrl,
      model,
      apiKey,
      imageBase64: img.base64,
      mime: img.mime,
      fetchImpl: fetch,
      timeoutMs: cfg0.timeoutMs || 45000,
    })
    out({ ok: true, desc: res.desc || '', points: res.points || [], model: res.model || model })
  } else {
    const cfg = vision.config()
    if (!cfg.apiKey) throw new Error('没配视觉模型的 key（面板「设置 → 聊天看图」里填）')
    const img = await vision.loadImage(target, cfg)
    const raw = await vision.ask(cfg, img, { system: SYSTEM, text: '看这张图，按要求输出 JSON。' })
    const parsed = vision.parseJson(raw) || {}
    const desc = String(parsed.desc || '').trim().slice(0, 200)
    const points = Array.isArray(parsed.points)
      ? parsed.points.map((x) => String(x).trim()).filter(Boolean).slice(0, 4)
      : []
    if (!desc) throw new Error(`模型没给出描述：${String(raw).slice(0, 80)}`)
    out({ ok: true, desc, points, model: cfg.model })
  }
} catch (err) {
  out({ ok: false, error: String(err && err.message ? err.message : err).slice(0, 300) })
}
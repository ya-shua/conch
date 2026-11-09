/**
 * 表情包的眼睛：让视觉模型按「要用的方式」看懂一张表情。
 *
 * 两条路（和 eye.mjs 一致）：
 *   1. plugin：借留影的取图 + 配置（默认，装了留影就走这条）；
 *   2. direct：EYE_ENGINE=direct 或没留影时，直接用海螺配的 key。
 *
 * 用法：node sticker-eye.mjs <本地文件 或 图片URL> [对这张图的说法]
 *   带了说法就按这个说法给标签（说法优先，模型只补标签）。
 * 输出：{"ok":true,"desc":"…","tags":["无语","摆烂"],"model":"…"}
 */
import { pathToFileURL } from 'node:url'

const ROOT = process.env.EYE_ROOT || ''
const ENGINE = String(process.env.EYE_ENGINE || 'auto').toLowerCase()
const target = process.argv[2]
const guidance = String(process.argv[3] || '').trim().slice(0, 200)

const SYSTEM = guidance
  ? '主人说：「' + guidance + '」\n' +
    '优先按它来；明显不符再按图补。给 3 到 5 个情绪/场合短标签（无语、摆烂、催…）。\n' +
    '只输出 JSON：{"tags":["…","…"]}'
  : '看这张表情，回答两件事：\n' +
    '1) 一句人话：这是什么表情（例：一只猫四脚朝天躺着）；\n' +
    '2) 3 到 5 个短标签：情绪或场合（无语、摆烂、开心、求夸、催、委屈、拒绝、敷衍）。\n' +
    '只输出 JSON：{"desc":"…","tags":["…","…"]}'

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n')
}

if (!target) {
  out({ ok: false, error: '没给图' })
  process.exit(0)
}

try {
  const usePlugin = ROOT && ENGINE !== 'direct'
  const vision = await import('./vision.mjs')
  const cfg0 = usePlugin
    ? await (async () => {
        const ring = await import(pathToFileURL(`${ROOT}/src/ring-vision.mjs`).href)
        const c = ring.loadVisionConfig({})
        return {
          baseUrl: (process.env.EYE_BASE_URL || c.baseUrl || '').replace(/\/+$/, ''),
          model: process.env.EYE_MODEL || c.model,
          apiKey: process.env.EYE_API_KEY || c.apiKey,
          maxBytes: c.maxBytes || 8 * 1024 * 1024,
          timeoutMs: c.timeoutMs || 45000,
        }
      })()
    : vision.config()

  if (!cfg0.apiKey) throw new Error('没配视觉模型的 key（面板「设置 → 聊天看图」里填）')
  const img = await vision.loadImage(target, cfg0)
  const raw = await vision.ask(cfg0, img, { system: SYSTEM, text: '看这张表情，按要求输出 JSON。', maxTokens: 200 })
  const parsed = vision.parseJson(raw) || {}
  const desc = String(parsed.desc ?? '').trim().slice(0, 120)
  const tags = Array.isArray(parsed.tags)
    ? parsed.tags.map((x) => String(x).trim()).filter(Boolean).slice(0, 5)
    : []
  if (!desc && !tags.length) throw new Error(`模型没给出能用的结果：${String(raw).slice(0, 80)}`)
  out({ ok: true, desc, tags, model: cfg0.model })
} catch (err) {
  out({ ok: false, error: String(err && err.message ? err.message : err).slice(0, 300) })
}
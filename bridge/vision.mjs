/**
 * 直接跟视觉模型说话（不经过留影）。
 *
 * 留影不在、或者不想让它再当中间人时用这一条：
 *   node vision.mjs 是给下面两个脚本共用的零件，不单独对外。
 *
 * 环境变量：EYE_BASE_URL / EYE_MODEL / EYE_API_KEY
 * 默认走智谱：https://open.bigmodel.cn/api/paas/v4 + glm-4v-flash
 */
import { readFileSync, statSync } from 'node:fs'
import { extname } from 'node:path'

export const MIME = {
  '.gif': 'image/gif',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
}

export function config() {
  return {
    baseUrl: (process.env.EYE_BASE_URL || 'https://open.bigmodel.cn/api/paas/v4').replace(/\/+$/, ''),
    model: process.env.EYE_MODEL || 'glm-4v-flash',
    apiKey: process.env.EYE_API_KEY || '',
    maxBytes: Number(process.env.EYE_MAX_BYTES || 0) || 8 * 1024 * 1024,
    timeoutMs: Number(process.env.EYE_TIMEOUT_MS || 0) || 45000,
  }
}

/** 图链就下载，本地文件就读；统一成 base64 + mime。 */
export async function loadImage(target, cfg) {
  if (/^https?:\/\//i.test(target)) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), cfg.timeoutMs)
    try {
      const res = await fetch(target, { signal: ctrl.signal, redirect: 'follow' })
      if (!res.ok) throw new Error(`取图失败 ${res.status}`)
      const buf = Buffer.from(await res.arrayBuffer())
      if (buf.length > cfg.maxBytes) throw new Error(`图片太大（${Math.round(buf.length / 1024)}KB）`)
      const head = buf.subarray(0, 12)
      const mime = sniffMime(head) || 'image/jpeg'
      return { base64: buf.toString('base64'), mime }
    } finally {
      clearTimeout(timer)
    }
  }
  const size = statSync(target).size
  if (size > cfg.maxBytes) throw new Error(`图片太大（${Math.round(size / 1024)}KB）`)
  const buf = readFileSync(target)
  return { base64: buf.toString('base64'), mime: MIME[extname(target).toLowerCase()] || 'image/png' }
}

function sniffMime(head) {
  if (head[0] === 0xff && head[1] === 0xd8) return 'image/jpeg'
  if (head[0] === 0x89 && head[1] === 0x50) return 'image/png'
  if (head.subarray(0, 3).toString() === 'GIF') return 'image/gif'
  if (head.subarray(0, 4).toString() === 'RIFF') return 'image/webp'
  if (head[0] === 0x42 && head[1] === 0x4d) return 'image/bmp'
  return ''
}

/** 问一次，拿模型原话（通常是 JSON）。 */
export async function ask(cfg, img, { system, text = '看图。', maxTokens = 300, temperature = 0.3 }) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), cfg.timeoutMs)
  let res
  try {
    res = await fetch(`${cfg.baseUrl}/chat/completions`, {
      method: 'POST',
      signal: ctrl.signal,
      headers: {
        'content-type': 'application/json',
        ...(cfg.apiKey ? { authorization: `Bearer ${cfg.apiKey}` } : {}),
      },
      body: JSON.stringify({
        model: cfg.model,
        messages: [
          { role: 'system', content: system },
          {
            role: 'user',
            content: [
              { type: 'text', text },
              { type: 'image_url', image_url: { url: `data:${img.mime};base64,${img.base64}` } },
            ],
          },
        ],
        temperature,
        max_tokens: maxTokens,
      }),
    })
  } finally {
    clearTimeout(timer)
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`视觉模型 ${res.status}：${body.slice(0, 160)}`)
  }
  const body = await res.json().catch(() => null)
  return String(body?.choices?.[0]?.message?.content ?? '')
}

export function parseJson(text) {
  const s = String(text || '').trim()
  const a = s.indexOf('{')
  const b = s.lastIndexOf('}')
  if (a === -1 || b <= a) return null
  try {
    return JSON.parse(s.slice(a, b + 1))
  } catch {
    return null
  }
}
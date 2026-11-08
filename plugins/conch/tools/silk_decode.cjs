/**
 * silk 解码小工具（腾讯 QQ 语音用的格式，ffmpeg 解不了）
 *
 * 用法： node silk_decode.cjs <输入.silk> <输出.pcm> [采样率]
 * 输出： 16 位单声道小端 PCM；采样率默认 16000（V3 常见值）privacy-ok
 *
 * 依赖： vendor/silk-wasm（npm silk-wasm，MIT，纯 wasm，不用编译）
 */

const { readFileSync, writeFileSync } = require('node:fs')
const { join } = require('node:path')

const vendorDir = join(__dirname, '..', 'vendor', 'silk-wasm', 'lib')
const { decode } = require(join(vendorDir, 'index.cjs'))

async function main() {
  const [, , src, dst, rateRaw] = process.argv
  if (!src || !dst) {
    console.error('用法：node silk_decode.cjs <输入.silk> <输出.pcm> [采样率]')
    process.exit(2)
  }
  const rate = Number(rateRaw || 16000) || 16000  // privacy-ok 采样率
  const silk = readFileSync(src)
  const out = await decode(silk, rate)
  const pcm = Buffer.from(out.data)
  writeFileSync(dst, pcm)
  console.error(`frames=${out.duration}ms pcm=${pcm.length}bytes rate=${rate}`)
}

main().catch((e) => {
  console.error('silk 解码失败：' + (e?.message || e))
  process.exit(1)
})
#!/usr/bin/env node
/**
 * rag_helper 部署入口（npm 只做转发，真正的逻辑在 deploy.sh / deploy.ps1）。
 *
 * 用法：
 *   npm run deploy                    启动 agent 单栈（等同 ./deploy.sh）
 *   npm run deploy -- joint           ItsMyGo + rag_helper 联合栈
 *   npm run deploy -- down            停止 agent 单栈（保留数据）
 *   npm run deploy -- joint-down      停止两套（保留数据）
 *   npm run deploy -- logs            跟踪 agent 日志
 *   npm run deploy -- status          查看容器状态
 *   npm run deploy -- reset           停止 agent 并删除其数据卷（需输入 yes）
 *
 * 也提供快捷脚本：deploy:up / deploy:joint / deploy:down / deploy:joint-down /
 * deploy:logs / deploy:status / deploy:reset / deploy:help
 *
 * 为什么要 node 而不是直接把命令写进 package.json：
 * Windows 下 npm 走 cmd/powershell、Linux/macOS 走 sh，直接写 `./deploy.sh` 两边不通用。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const COMMANDS = new Set([
  'up',
  'joint',
  'down',
  'joint-down',
  'logs',
  'status',
  'reset',
  'help',
])

const USAGE = `
rag_helper 部署入口（转发到 deploy.sh / deploy.ps1）

  npm run deploy                    启动 agent 单栈
  npm run deploy -- joint           ItsMyGo + rag_helper 联合栈
  npm run deploy -- down            停止 agent 单栈（保留数据）
  npm run deploy -- joint-down      停止两套（保留数据）
  npm run deploy -- logs            跟踪 agent 日志
  npm run deploy -- status          查看容器状态
  npm run deploy -- reset           停止 agent 并删除其数据卷（需输入 yes）

等价快捷脚本：npm run deploy:up / deploy:joint / deploy:down / deploy:joint-down /
deploy:logs / deploy:status / deploy:reset / deploy:help
`.trim()

const raw = process.argv.slice(2)
const wantsHelp = raw.includes('-h') || raw.includes('--help')

let command = 'up'
if (!wantsHelp && raw.length > 0 && !raw[0].startsWith('-')) {
  command = raw[0]
}
if (wantsHelp) {
  command = 'help'
}

if (!COMMANDS.has(command)) {
  console.error(`未知命令：${command}\n`)
  console.error(USAGE)
  process.exit(1)
}

const isWindows = process.platform === 'win32'
const script = join(ROOT, isWindows ? 'deploy.ps1' : 'deploy.sh')
if (!existsSync(script)) {
  console.error(`找不到部署脚本：${script}`)
  process.exit(1)
}

const child = isWindows
  ? spawn(
      'powershell.exe',
      ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, command],
      { cwd: ROOT, stdio: 'inherit' },
    )
  : spawn('bash', [script, command], { cwd: ROOT, stdio: 'inherit' })

child.on('error', (error) => {
  console.error('无法启动部署脚本：', error.message)
  process.exit(1)
})
child.on('exit', (code, signal) => {
  if (signal) {
    console.error(`部署脚本被信号终止：${signal}`)
    process.exit(1)
  }
  process.exit(code ?? 1)
})

#!/usr/bin/env node
/**
 * 开发环境联合启动入口（跨平台，从 rag_helper 仓库执行）
 *
 * 与 ItsMyGo 的 scripts/dev-joint.mjs 行为一致，只是把“本仓库”换成了 rag_helper：
 *   1. dev-infra 中间件（MySQL/Redis/MinIO/RabbitMQ/Mongo/Milvus/Neo4j…，已在跑则复用）
 *   2. ItsMyGo 后端（mvnw spring-boot:run，8080）
 *   3. ItsMyGo 前端（Vite，5173）
 *   4. rag_helper 三个本地进程（venv：8001 导入 / 8002 chat / 8003 gRPC）
 *
 * 用法：
 *   npm run dev:joint           启动（已启动会拒绝，避免端口冲突）
 *   npm run dev:joint:stop      停止应用进程与 dev-infra 容器（数据卷保留）
 *   npm run dev:joint:status    查看进程与健康状态
 *
 * ItsMyGo 目录：ITS_MYGO_DIR 环境变量 / 本仓 .itsmygo-dir 文件 / 默认 ../ItsMyGo。
 * rag_helper 固定为本脚本所在仓库。
 * 依赖：Docker、JAVA_HOME（或 PATH 里有 java）、Node、rag_helper 的 .venv。
 * 日志与 PID：rag_helper 根 .dev-logs/（已 gitignore）。
 */
import { spawn, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync, writeSync } from 'node:fs'
import net from 'node:net'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const LOG_DIR = join(ROOT, '.dev-logs')
const PID_FILE = join(LOG_DIR, 'processes.json')
const IS_WIN = process.platform === 'win32'

const SERVICES = {
  backend: { label: 'ItsMyGo 后端', port: 8080, health: 'http://127.0.0.1:8080/api/health' },
  frontend: { label: 'ItsMyGo 前端', port: 5173, health: 'http://127.0.0.1:5173/' },
  'rag-import': { label: 'rag_helper 导入', port: 8001, health: null },
  'rag-chat': { label: 'rag_helper chat', port: 8002, health: 'http://127.0.0.1:8002/health' },
  'rag-grpc': { label: 'rag_helper gRPC', port: 8003, health: null },
}

function fail(message) {
  console.error(`\n[dev:joint] ${message}\n`)
  process.exit(1)
}

function ensureLogDir() {
  if (!existsSync(LOG_DIR)) mkdirSync(LOG_DIR, { recursive: true })
}

function readEnvFile(file, key) {
  if (!existsSync(file)) return ''
  const text = readFileSync(file, 'utf8')
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(new RegExp('^\\s*' + key + '=(.*)$'))
    if (m) return m[1].trim()
  }
  return ''
}

function setEnvFile(file, key, value) {
  if (!existsSync(file)) fail(`缺少 ${file}，请先初始化`)
  const text = readFileSync(file, 'utf8')
  const pattern = new RegExp('^\\s*' + key + '=.*$', 'm')
  const next = pattern.test(text)
    ? text.replace(pattern, `${key}=${value}`)
    : text.replace(/\s*$/, '') + `\n${key}=${value}\n`
  writeFileSync(file, next, 'utf8')
}

function findItsMyGoOptional() {
  const candidates = []
  if (process.env.ITS_MYGO_DIR) candidates.push(process.env.ITS_MYGO_DIR)
  const pointer = join(ROOT, '.itsmygo-dir')
  if (existsSync(pointer)) candidates.push(readFileSync(pointer, 'utf8').trim())
  candidates.push(join(ROOT, '..', 'ItsMyGo'))

  for (const dir of candidates) {
    if (dir && existsSync(join(dir, 'dev-infra', 'docker-compose.yml'))) return dir
  }
  return null
}

function findItsMyGo() {
  const dir = findItsMyGoOptional()
  if (!dir) {
    fail('找不到 ItsMyGo 目录。请设置 ITS_MYGO_DIR，或在本仓创建 .itsmygo-dir 写入路径。')
  }
  return dir
}

function pythonPath(ragDir) {
  const win = join(ragDir, '.venv', 'Scripts', 'python.exe')
  const posix = join(ragDir, '.venv', 'bin', 'python')
  if (existsSync(win)) return win
  if (existsSync(posix)) return posix
  fail(`找不到 rag_helper 的虚拟环境（${win} / ${posix}）。请先在 rag_helper 里创建 .venv。`)
}

function ensureRagEnv(ragDir) {
  const envFile = join(ragDir, '.env')
  const example = join(ragDir, '.env.example')
  if (!existsSync(envFile)) {
    if (!existsSync(example)) fail(`缺少 ${envFile} 与 .env.example`)
    writeFileSync(envFile, readFileSync(example))
    console.log('已生成 rag_helper/.env。')
  }
  const missing = ['OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'EMBEDDING_API_KEY'].filter((key) => {
    const v = readEnvFile(envFile, key)
    return !v || v === 'sk-xxx'
  })
  if (missing.length > 0) fail(`rag_helper 还差模型密钥：${missing.join(', ')}，请编辑 ${envFile}`)

  let key = readEnvFile(envFile, 'AGENT_GRPC_KEY')
  if (!key) {
    key = randomBytes(32).toString('hex')
    setEnvFile(envFile, 'AGENT_GRPC_KEY', key)
    console.log('已为 rag_helper 生成 AGENT_GRPC_KEY。')
  }
  return key
}

function syncAgentKeys(itsDir, sharedKey) {
  const envFile = join(itsDir, '.env')
  if (!existsSync(envFile)) fail(`缺少 ${envFile}，请先在 ItsMyGo 运行一次 npm run deploy 或手工创建`)
  setEnvFile(envFile, 'AGENT_GRPC_KEY', sharedKey)
  setEnvFile(envFile, 'AGENT_IMPORT_KEY', sharedKey)
}

function dockerRunning() {
  const result = spawnSync('docker', ['info'], { stdio: 'ignore' })
  return result.status === 0
}

function startInfra(itsDir) {
  console.log('==> 启动 dev-infra 中间件')
  const result = spawnSync(
    'docker',
    [
      'compose',
      '-f',
      join(itsDir, 'dev-infra', 'docker-compose.yml'),
      '--env-file',
      join(itsDir, 'dev-infra', '.env'),
      'up',
      '-d',
      '--wait',
      '--wait-timeout',
      '180',
    ],
    { cwd: itsDir, stdio: 'inherit' },
  )
  if (result.status !== 0) fail('dev-infra 启动失败（检查 Docker Desktop 与 ItsMyGo/dev-infra/.env）')
}

function initAgentKey(ragDir, sharedKey) {
  // 与容器版 agent-key-init 等价：把 AGENT_GRPC_KEY 写进 rag_helper 的 Mongo 用户表
  console.log('==> 初始化 rag_helper 访问密钥（Mongo 用户表）')
  const result = spawnSync(pythonPath(ragDir), ['-m', 'app.clients.mongo_user_clients', 'ensure', 'itsmygo', 'admin'], {
    cwd: ragDir,
    env: { ...process.env, RAG_AGENT_KEY: sharedKey },
    stdio: 'inherit',
  })
  if (result.status !== 0) fail('rag_helper 访问密钥初始化失败（检查 dev-infra 的 mongo 是否就绪）')
}

function spawnLogged(name, command, args, options) {
  ensureLogDir()
  const logFile = join(LOG_DIR, `${name}.log`)
  const fd = openSync(logFile, 'a')
  writeSync(fd, `\n===== ${new Date().toISOString()} 启动 =====\n`)
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env ?? process.env,
    stdio: ['ignore', fd, fd],
    windowsHide: true,
    detached: true, // 父进程退出后服务继续运行（POSIX 下同时形成独立进程组，便于整组停止）
  })
  // 子进程已继承 fd，父进程可以关闭自己的句柄
  child.on('spawn', () => closeSync(fd))
  child.on('error', () => closeSync(fd))
  child.unref()
  return { pid: child.pid, logFile }
}

function windowsShell(commandLine) {
  return ['cmd.exe', ['/d', '/s', '/c', commandLine]]
}

function resolveJavaHome() {
  if (process.env.JAVA_HOME) return process.env.JAVA_HOME
  if (!IS_WIN) return ''
  const where = spawnSync('where', ['java'], { encoding: 'utf8' })
  if (where.status !== 0) return ''
  const first = where.stdout.split(/\r?\n/).find(Boolean)
  if (!first) return ''
  // ...\jdk\bin\java.exe -> ...\jdk
  return dirname(dirname(first.trim()))
}

function startApps(ragDir, itsDir) {
  const py = pythonPath(ragDir)
  const envWithKeys = { ...process.env }
  const javaHome = resolveJavaHome()
  if (javaHome) envWithKeys.JAVA_HOME = javaHome

  const started = {}

  console.log('==> 启动 rag_helper 三个进程（venv）')
  for (const [name, module] of [
    ['rag-import', 'app.entrypoints.import_http'],
    ['rag-chat', 'app.entrypoints.agent_http'],
    ['rag-grpc', 'app.entrypoints.agent_grpc'],
  ]) {
    const { pid, logFile } = spawnLogged(name, py, ['-m', module], { cwd: ragDir, env: envWithKeys })
    started[name] = { pid, logFile }
  }

  console.log('==> 启动 ItsMyGo 后端（mvnw spring-boot:run）')
  {
    const [cmd, args] = IS_WIN
      ? windowsShell('.\\mvnw.cmd -o spring-boot:run')
      : ['./mvnw', ['-o', 'spring-boot:run']]
    const { pid, logFile } = spawnLogged('backend', cmd, args, { cwd: itsDir, env: envWithKeys })
    started.backend = { pid, logFile }
  }

  console.log('==> 启动 ItsMyGo 前端（Vite）')
  {
    const [cmd, args] = IS_WIN
      ? windowsShell('npm --prefix frontend run dev')
      : ['npm', ['--prefix', 'frontend', 'run', 'dev']]
    const { pid, logFile } = spawnLogged('frontend', cmd, args, { cwd: itsDir })
    started.frontend = { pid, logFile }
  }

  return started
}

async function waitHttp(name, url, timeoutMs = 180000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(3000) })
      if (res.ok || res.status === 404) return true
    } catch {
      // 还没起来
    }
    await new Promise((r) => setTimeout(r, 1000))
  }
  return false
}

function waitTcp(name, port, timeoutMs = 120000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs
    const attempt = () => {
      const socket = net.connect({ host: '127.0.0.1', port })
      socket.once('connect', () => {
        socket.destroy()
        resolve(true)
      })
      socket.once('error', () => {
        socket.destroy()
        if (Date.now() > deadline) resolve(false)
        else setTimeout(attempt, 1000)
      })
    }
    attempt()
  })
}

function isPidAlive(pid) {
  if (!pid) return false
  if (IS_WIN) {
    const result = spawnSync('tasklist', ['/FI', `PID eq ${pid}`, '/NH'], { encoding: 'utf8' })
    return result.status === 0 && result.stdout.includes(String(pid))
  }
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function loadPids() {
  if (!existsSync(PID_FILE)) return null
  try {
    return JSON.parse(readFileSync(PID_FILE, 'utf8'))
  } catch {
    return null
  }
}

function savePids(data) {
  ensureLogDir()
  writeFileSync(PID_FILE, JSON.stringify(data, null, 2), 'utf8')
}

async function commandUp() {
  if (!dockerRunning()) fail('Docker Desktop 未启动')
  const existing = loadPids()
  if (existing && Object.values(existing.services ?? {}).some((s) => isPidAlive(s?.pid))) {
    fail('开发环境看起来已经在运行。先执行 npm run dev:joint:stop，或用 npm run dev:joint:status 查看。')
  }

  const ragDir = ROOT
  const itsDir = findItsMyGo()
  const sharedKey = ensureRagEnv(ragDir)
  syncAgentKeys(itsDir, sharedKey)
  startInfra(itsDir)
  initAgentKey(ragDir, sharedKey)

  const services = startApps(ragDir, itsDir)
  savePids({ startedAt: new Date().toISOString(), ragDir, itsDir, services })

  const checks = [
    ['backend', SERVICES.backend.health],
    ['frontend', SERVICES.frontend.health],
    ['rag-chat', SERVICES['rag-chat'].health],
  ]
  for (const [name, url] of checks) {
    const ok = await waitHttp(name, url)
    if (!ok) {
      console.error(`[dev:joint] ${SERVICES[name].label} 启动超时，查看 .dev-logs/${name}.log`)
      process.exit(1)
    }
  }
  for (const [name, port] of [
    ['rag-import', 8001],
    ['rag-grpc', 8003],
  ]) {
    const ok = await waitTcp(name, port)
    if (!ok) {
      console.error(`[dev:joint] ${SERVICES[name].label} 端口 ${port} 未就绪，查看 .dev-logs/${name}.log`)
      process.exit(1)
    }
  }

  console.log('\n开发环境（联合）已启动：')
  console.log('  ItsMyGo 前端   http://localhost:5173')
  console.log('  ItsMyGo 后端   http://localhost:8080/api/health')
  console.log('  rag_helper     8001 导入 / 8002 chat / 8003 gRPC')
  console.log('  日志          .dev-logs/*.log')
  console.log('停止应用进程与中间件容器：npm run dev:joint:stop')
}

function killTree(pid) {
  if (!pid) return
  if (IS_WIN) {
    spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
  } else {
    try {
      process.kill(-pid, 'SIGTERM')
    } catch {
      try {
        process.kill(pid, 'SIGTERM')
      } catch {
        // 已退出
      }
    }
  }
}

function commandStop() {
  const data = loadPids()
  if (data) {
    for (const [name, service] of Object.entries(data.services ?? {})) {
      if (isPidAlive(service.pid)) {
        killTree(service.pid)
        console.log(`已停止 ${name}（pid ${service.pid}）`)
      }
    }
    rmSync(PID_FILE, { force: true })
  } else {
    console.log('没有找到 .dev-logs/processes.json，跳过应用进程停止。')
  }

  const itsDir = data?.itsDir ?? findItsMyGoOptional()
  const infraCompose = itsDir ? join(itsDir, 'dev-infra', 'docker-compose.yml') : ''
  const infraEnv = itsDir ? join(itsDir, 'dev-infra', '.env') : ''
  if (!itsDir || !existsSync(infraCompose)) {
    console.log('未找到 ItsMyGo/dev-infra/docker-compose.yml，跳过中间件容器停止。')
    return
  }
  if (!dockerRunning()) {
    console.log('Docker 未运行，跳过 dev-infra 容器停止。')
    return
  }

  console.log('==> 停止 dev-infra 容器（数据卷保留）')
  const result = spawnSync(
    'docker',
    [
      'compose', '-f', infraCompose, '--env-file', infraEnv,
      // 带上可选 profile，确保手动启动过的 neo4j/legacy 容器也一起停掉
      '--profile', 'kg', '--profile', 'legacy', 'down',
    ],
    { cwd: itsDir, stdio: 'inherit' },
  )
  if (result.status !== 0) {
    console.warn('dev-infra 容器停止失败；可手动执行：cd ItsMyGo/dev-infra && docker compose --env-file .env down')
    return
  }
  console.log('dev-infra 容器已停止（external 数据卷保留）。')
}

async function commandStatus() {
  const data = loadPids()
  if (!data) {
    console.log('未通过 dev:joint 启动（无 .dev-logs/processes.json）。')
  } else {
    console.log(`启动时间：${data.startedAt}`)
    console.log(`rag_helper：${data.ragDir}`)
    console.log(`ItsMyGo：${data.itsDir}`)
    for (const [name, service] of Object.entries(data.services ?? {})) {
      const alive = isPidAlive(service.pid)
      console.log(`  ${alive ? '✔' : '✘'} ${name.padEnd(10)} pid=${service.pid}`)
    }
  }

  const probes = [
    ['ItsMyGo 后端', SERVICES.backend.health],
    ['ItsMyGo 前端', SERVICES.frontend.health],
    ['rag_helper chat', SERVICES['rag-chat'].health],
  ]
  for (const [label, url] of probes) {
    const ok = await waitHttp(label, url, 3000)
    console.log(`  ${ok ? '✔' : '✘'} ${label}：${url}`)
  }
  for (const [label, port] of [
    ['rag_helper 导入', 8001],
    ['rag_helper gRPC', 8003],
  ]) {
    const ok = await waitTcp(label, port, 2000)
    console.log(`  ${ok ? '✔' : '✘'} ${label}：127.0.0.1:${port}`)
  }
  const itsDir = data?.itsDir ?? findItsMyGo()
  const ps = spawnSync('docker', ['compose', '-f', join(itsDir, 'dev-infra', 'docker-compose.yml'), '--env-file', join(itsDir, 'dev-infra', '.env'), 'ps', '--format', 'table {{.Name}}\t{{.Status}}'], {
    cwd: itsDir,
    encoding: 'utf8',
  })
  if (ps.status === 0 && ps.stdout.trim()) {
    console.log('\ndev-infra 中间件：')
    console.log(ps.stdout.trim())
  }
}

const command = (process.argv[2] ?? 'up').toLowerCase()
switch (command) {
  case 'up':
  case 'start':
    await commandUp()
    break
  case 'stop':
    commandStop()
    break
  case 'status':
    await commandStatus()
    break
  case 'help':
  case '-h':
  case '--help':
    console.log(`开发环境联合启动

  npm run dev:joint           启动中间件 + ItsMyGo 前后端 + rag_helper 三个进程
  npm run dev:joint:stop      停止应用进程与 dev-infra 容器（数据卷保留）
  npm run dev:joint:status    查看进程与健康状态

ItsMyGo 目录：ITS_MYGO_DIR / 本仓 .itsmygo-dir / 默认 ../ItsMyGo
rag_helper：当前仓库
日志：.dev-logs/`)
    break
  default:
    fail(`未知命令：${command}（可用 up / stop / status）`)
}

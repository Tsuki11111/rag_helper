# Agent Galaxy · 项目逻辑星图

把 rag_helper 的运行时、RAG、工具权限、存储和部署边界画成一张可交互的「群星式」星图。

## 打开

直接双击 `agent-galaxy.html`，或：

```powershell
start visualization\agent-galaxy.html
```

单文件、Canvas 2D 伪 3D，不依赖 CDN / 构建 / 后端，`file://` 直接可用。

## 操作

| 操作 | 效果 |
|---|---|
| 拖拽 | 旋转星图 |
| 滚轮 | 缩放 |
| 点击节点 | 平滑聚焦到该节点，并展开说明、代码位置和关键实现 |
| 搜索框输入 + Enter | 过滤节点并聚焦第一个结果 |
| 点击左下系统图例 | 只看某个系统 |
| 「主线巡游」 | 按 `run_main_agent` 主调用链自动播放 |
| 空格 | 暂停 / 恢复自动旋转 |
| `/` | 聚焦搜索 |
| `Esc` | 关闭面板 / 停止巡游 |

## 内容

- 9 个系统：接入与契约、运行时、Agent 编排、RAG 检索、知识导入、工具与权限、状态与存储、可观测与评测、部署与边界。
- 62 个节点、92 条关系。
- 节点里的代码是真实模块的精简摘录，完整文件路径在面板「代码位置」中；不是完整源码，也不是自动生成的全量调用图。

## 修改

节点、边和代码片段在 `agent-galaxy.html` 顶部的 `CATS` / `NODES` / `EDGES` 数据段，改完刷新即可。

## 预览

- `preview-desktop.png`：1600×1000 桌面
- `preview-mobile.png`：390×844 移动端
- `preview-retrieval.png`：按 RAG 系统过滤

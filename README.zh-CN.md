# superdev

[English](README.md)

**别的工具管派活，superdev 管把活干成**——测试全过、合并请求开出、证据随附，不用人盯着循环。

superdev 是面向 AI 编码团队的执行可靠性层（开源控制塔）：把一队编码智能体（Claude Code、Codex、ZCode、Kimi……）变成能真正收工的工人。

## 十五秒看懂它（无需 API key）

失败生命周期就是本产品。一条命令看一个任务走红→回滚→强制研究→转绿→合并，另一个任务卡住→升级→拍板恢复：

```bash
python examples/see_failure_lifecycle.py
```

## 它解决什么问题

同时跑多个编码智能体的人都会撞这堵墙：

- 智能体把测试改红了还继续在坏代码上叠改，越试越远。
- 同一思路重试五次，token 在烧，进度为零。
- 几小时后人才发现，翻聊天记录猜"刚才试过什么"。

多数工具把这些当异常。superdev 把失败当**一等公民的生命周期**：失败预算 → 强制研究 → 结构化报告 → 一键拍板 → 自动恢复。红的不合入，卡的不空转，请人之前先给全证据。

## 六十秒上手

```bash
git clone https://github.com/yinrong/superdev
cd superdev
pip install -e ".[dev]"
python -m pytest -q          # 342 passed
```

自举彩蛋——仓库校验自己的开发台账：

```bash
python superdev_cli.py check agents/superdev-dev/派工计划.yml
# 校验通过：0 处问题
```

导入 spec-kit 清单当任务跑：`python superdev_cli.py import-spec <目录> plan.yml`

## 目录地图

`superdev/` 核心包，一个模块管一件事（17 个）：

| 层 | 模块 |
|---|---|
| 台账与规则 | ledger（git 版本化任务账本）· check（八项契约校验）· hooks（程序强制的边界） |
| 执行纪律 | redgreen（绿点分支、红即回滚、两败转研究）· research（预算分档+模块知识库）· engines（可插拔工人适配器） |
| 流转与状态 | state（状态机）· events（防篡改哈希链日志）· projection（SQLite 投影） |
| 安全网 | supervise（停滞判定、五要素报告、自动恢复）· guard（资源守护）· gate（质检管线）· reviewer（评审工人）· ciadapter（CI Watch） |
| 人与输入 | web（看板+一键拍板+token 认证）· clarify（需求澄清）· importspec（spec-kit 导入） |

- `commands/`：两份契约（[管理端](commands/superdev-manage.md)、[工人](commands/superdev-work.md)）
- `agents/superdev-dev/`：自举台账，superdev 的开发自己也走这套流程
- `design/`：[设计文档](design/README.md)

## 自举证明

本仓库由 superdev 自己建成：公开台账 21 个任务、14 名工人、8 个绿点（每次合入前全量测试必须创新高：59→143→223→239→310→342）；契约校验器开发期间两次拦下本仓库自己的台账（文件越界、超并发派发），都按规则修而不是绕过。

## 状态与路线图

v0.1 核心闭环完成，342 测试全绿，无已知问题。非本期：云同步看板、可插拔编辑引擎、托管服务。

参与贡献先读 [CONTRIBUTING.md](CONTRIBUTING.md)：任务先进台账再写码，PR 必须保持全绿。[Apache-2.0](LICENSE)

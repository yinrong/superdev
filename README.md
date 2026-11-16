# superdev

一套让 AI 团队自动交付软件的中台：AI 管想和写，程序管规则。AI 拆任务、写代码；越界的写入被 hook（拦截钩子）挡下，测试红了整体退回，卡住自动升级给人拍板。

## 十分钟上手

环境：Python 3.12+，装依赖 `pip install pytest pyyaml`。

1. 跑全量测试确认绿：`python -m pytest`
2. 校验一份真台账（任务账本）：`python superdev_cli.py check agents/superdev-dev/派工计划.yml`
3. 导入 spec-kit（GitHub 官方模板）清单：`python superdev_cli.py import-spec <spec目录> <输出台账路径>`
4. 跑一轮对账（核对台账和实际状态）：`python superdev_cli.py round agents/superdev-dev`

## 目录地图

`superdev/` 核心包，一个模块管一件事：

| 模块 | 一句话 |
|---|---|
| ledger | 台账读写：派工计划.yml 与 status.yml，出错整体不打散 |
| check | 派工计划校验器，管"校验八项" |
| state | 任务状态机：什么状态能迁到哪 |
| events | 事件日志：只增不改，哈希链（指纹逐条相连）防篡改 |
| projection | SQLite 投影（台账的数据库副本），供查询和事件派发 |
| redgreen | 红绿循环纪律：从绿点（全绿提交）开工、红即回滚、两败先研究 |
| research | 研究纪律：预算分档、引用校验、模块知识库 |
| supervise | 督办：停滞判定、五要素升级报告、自动恢复 |
| guard | 资源守护：进程快照、超限处置、确认后真杀 |
| hooks | 管理端 hook 裁决，四个纯函数 |
| engines | 工人引擎与回归协议，可插拔 |
| gate | 质检管线：lint/typecheck/test/build 多步门禁 |
| reviewer | 评审工人：门禁前的第二双眼 |
| ciadapter | CI Watch：盯合并请求后的远端流水线，失败回喂工人 |
| clarify | 需求澄清：拆任务前一次一问附选项 |
| web | 网页决策面：看板 + 一键拍板 + token 认证 |
| importspec | spec-kit 清单转派工计划 |

- `commands/`：两份契约，superdev-manage.md（管理端）和 superdev-work.md（工人）
- `agents/superdev-dev/`：自举台账，superdev 开发自己也走这套流程
- `design/`：设计文档，[01-卖点](design/01-卖点.md)、[02-架构](design/02-架构设计.md)、[03-详细设计](design/03-详细设计.md)、[04-bigdev问题与改进](design/04-bigdev问题与改进.md)、[05-竞品对比](design/05-竞品对比.md)

## 当前状态

v0.1 核心闭环已通：台账入库、红绿循环、督办升级、网页拍板，端到端有测试兜底（tests/test_e2e.py），全量 310+ 个测试全绿。

CI Watch 已加（ciadapter）：合并请求后盯远端流水线，失败走本地同一条回喂路；CI 与 bigdev 兼容细节见台账 T18/T19。

网页决策面支持 token 远程访问：`DecisionWeb(..., auth_token="随机串")` 后，请求须带 `Authorization: Bearer <token>` 头或 `?token=` 参数，否则 401；不设 token 仍是免认证的本机模式。

网页决策面这样起（先跑过一轮 round 刷新投影，用法同 tests/test_web.py）：

    python -c "from pathlib import Path; from superdev.projection import Projection; from superdev.events import EventLog; from superdev.web import DecisionWeb; d=Path('agents/superdev-dev'); DecisionWeb(Projection(d/'projection.db').__enter__(), EventLog(d/'events.jsonl'), d/'派工计划.yml').serve(port=8765, block=True)"

浏览器打开 http://127.0.0.1:8765 就是看板。

## 边界

云同步看板（多人协作）按设计排到非首期；编辑引擎与云托管运营挂账。详见 [design/05 吸收清单](design/05-竞品对比.md)。

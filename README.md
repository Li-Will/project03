# 企业客服多 Agent 工单系统（Ticket Agent）

> M0 骨架阶段。快速开始与架构详见 `docs/项目计划书.md`；README 将在 M4 工程化阶段完善。

## 一句话

用户一条消息进来，系统判定意图后要么 FAQ 直答、要么拆成工单走「分类→诊断→方案→用户确认/转人工」的多 Agent 流水线，
全程由**工单状态机**托管：合法迁移校验 + SLA 升级 + 人工接管（interrupt/resume）+ 满意度闭环。

## 状态

- 当前里程碑：M0（环境与数据）进行中
- 计划书：[docs/项目计划书.md](docs/项目计划书.md)

## 快速开始（骨架验证）

```bash
uv sync --python 3.13
uv run python -m pytest -q
```
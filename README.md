# Kibitz

> 坐在旁边陪你工作、娱乐、打游戏的家伙。记得前情，接得住话，偶尔嘴你一句。

**状态**：macOS demo 已接入 OpenClaw 完整持续会话与共享记忆（2026-10-28）；长期陪伴体验待试玩。
**位置**：`~/agent/kibitz`
**名称**：沿用“旁观并插嘴的人”的含义，描述这个伙伴的性格。

## 核心流程

**获取当前活动状态 → 综合上下文 → 发表评论。**

开启陪伴模式后，结合眼前屏幕、近期经历、对话、记忆和人格，说出贴合当下的话，也可以保持沉默。游戏、日常工作与娱乐都在范围内。

## 当前优先级

先验证主流程有没有陪伴价值，再优化 token、延迟和资源占用。注意力自学习是后续候选方案，不是实现前提。

由 OpenClaw skill 启动轻量后台循环：定时截图，送入同一 `main` agent 的独立普通会话 `agent:main:kibitz`。身份、人格、用户背景、工具和历史由 OpenClaw 管理，按需读写同一份自定义 `workspace/brain/`，使用 mac 系统通知输出评论。

## 使用 demo

当前已安装到本机 OpenClaw workspace，skill 名为 `kibitz`。在 OpenClaw 中说：

- “开启陪伴模式” / “陪我工作” / “陪我玩游戏”
- “陪伴模式现在什么状态？”
- “停止陪伴” / “别看了”

默认观察主屏，每 5 秒一轮，保留最近两帧。模型决定评论或沉默，通知不会按固定频率强制弹出。处理慢时降低实际观察频率，不积压请求。

评论以平均约每分钟一条为目标：普通评论至少间隔60秒；独立、有趣的新事件可短时多说，发言额度每分钟恢复一条、最多存两条。画面没实质变化或兴趣低时沉默，不补齐次数，也不积压过时评论。每轮都向模型提供实际发言统计，程序侧再执行频率限制。

源 skill 在 `skills/kibitz/`。在另一台同样配置了视觉模型的 Mac 安装：

```bash
openclaw skills install ./skills/kibitz --agent main
```

直接验证或控制：

```bash
python3 skills/kibitz/scripts/kibitz.py once
python3 skills/kibitz/scripts/kibitz.py init
python3 skills/kibitz/scripts/kibitz.py start
python3 skills/kibitz/scripts/kibitz.py status
python3 skills/kibitz/scripts/kibitz.py stop
```

默认模型为本机已有的 `deepseek/deepseek-v4-flash` 的完整 agent 运行，可用 `--model inherit` 沿用 agent/会话模型。复用现有 Gateway 和凭据，不修改 OpenClaw 源码或配置。`init` 只初始化并核验会话，不采屏。

## 上下文与记忆

AGENTS、SOUL、IDENTITY、USER、TOOLS 通过正常 agent 启动注入；本工作区的 BOOTSTRAP 与 brain/README 在首次接续时显式读取。启动报告缺少必要文件、读写工具、会话或工作区不匹配时会报错，不退回无工具的单次推理。

陪伴会话直接使用文件工具按你的记忆协议查询和更新 `brain/`。事实、事件、解释各归其位；编译产物仍通过 memory-curator 流程更新，Python 不搬运事实。主聊天尚未写入记忆的新对话不会自动复制过来，可显式交给陪伴会话：

```bash
python3 skills/kibitz/scripts/kibitz.py note --text '用户明确交给陪伴会话的新消息或记忆要求'
```

运行时下一轮处理，停止时排队到下一次 start/init；status 显示 pending_notes。偏好补充仍可通过 context 更新，但不替代正常启动上下文。

运行状态、会话诊断、排队消息和每轮用量在 `~/.kibitz/`；临时截图在 `workspace/state/kibitz/`，正常停止时删除。OpenClaw 持续会话可能保留图像与工具历史，停止不清空会话或长期记忆。没有登录自启。

截图和相关上下文按正常 agent 流程提交给所选 provider。通知实际可见性受 macOS 通知权限和专注模式影响。

## 产品边界

- 通用、零针对性配置：不逐个游戏或应用写适配，不要求用户填 ROI、选类型或安装游戏插件。
- 保留记忆、人格和对话上下文；长期目标包括语音输入与输出。
- 初版不做本地 OCR，不引入外周感知和策略生成系统。

## 下一步与验证

下一步进行约半小时的真实陪伴试玩，检查是否看错、是否接上前情、评论是否具体有趣、是否让人想接话或觉得被打扰。当前记录每轮耗时和 agent 实际返回的 token 用量；完整启动上下文和工具调用可能增加处理时间，观察仍串行、不积压。

本地检查：`python3 -m unittest discover -s . -p test_kibitz.py -v`。

真实接入验证：`python3 verify_integration.py`。它在独立测试会话中验证完整注入、临时 brain 记录读写与清理、会话续接和随机颜色图片输入；结果保存在 `.checks/verification-report.json`。

## 文档

- [当前设计与验证顺序](docs/INSPIRATION.md)
- [分项测量、估算与待复核结论](docs/MEASUREMENTS.md)
- [v1–v4 设计历史（已归档）](docs/DESIGN_HISTORY.md)

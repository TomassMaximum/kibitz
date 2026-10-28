---
name: kibitz
description: 开启、停止或调整 macOS 桌面陪伴模式；在 OpenClaw 主 agent 的独立持续会话中看屏、查询与更新同一份自定义记忆，适时通过系统通知评论。适用于“开启陪伴模式”“陪我工作/玩游戏/看视频”、陪伴反馈、停止或查询状态。
metadata:
  openclaw:
    os: [darwin]
    requires:
      bins: [python3, openclaw, screencapture, osascript]
---

# Kibitz

入口：`{baseDir}/scripts/kibitz.py`。使用同一个 `main` agent、同一个 workspace，固定普通会话 `agent:main:kibitz`；完整启动注入、工具和会话历史由 OpenClaw 管理。
模式协议见 [session.md](references/session.md)，发言偏好见 [context.md](references/context.md)。不把主会话启动文件或 brain 整份复制成另一份交接。

## 开启与验证

```bash
python3 "{baseDir}/scripts/kibitz.py" start
python3 "{baseDir}/scripts/kibitz.py" status
```

默认主屏、目标每5秒一轮、最近两帧；模型为 `deepseek/deepseek-v4-flash` 的完整 agent 运行，可用 `--model inherit` 沿用该 agent/会话模型。图片无法直接读入时由 agent 使用已有 image 工具。
`start` 只确认进程启动；用 status 确认首轮成功，`agent_session` 应有完整启动文件、记忆工具、workspace 和固定 session id。重复 start 不多开。

只初始化完整会话、不采屏：

```bash
python3 "{baseDir}/scripts/kibitz.py" init
```

身份、SOUL、USER、TOOLS、AGENTS 走正常启动注入；本机自定义 BOOTSTRAP 与 brain/README 由会话首次接续时显式读取。会话允许按记忆协议使用 read/edit/write 等工具；编译产物仍只由 memory-curator 流程更新，不写脚本搬运事实。

## 新消息、记忆与偏好

用户对陪伴的反馈、提问或记忆要求，用 note 传入这个持续会话：

```bash
python3 "{baseDir}/scripts/kibitz.py" note --file - <<'NOTE'
这里写用户刚刚明确交给陪伴会话的内容。
NOTE
```

运行时在下一轮处理；停止时排队到下一次 start 或 init。可从 status 的 pending_notes 查看是否仍待处理；处理结果在 summary，具体记忆写入应核对目标文件。共享磁盘记忆可直接查询，但主聊天未落记忆的新对话不会自动复制过来。

临时调整本次偏好可用 `context --file <UTF-8文件>`，每轮重新读取；它只补充偏好和相关前情，不替代启动人格/用户信息。不要默认注入待办或督促职责。长期偏好按记忆协议保存，并更新项目内偏好底稿，防止下次启动倒退。

## 发言与生命周期

观察间隔与发言频率分开。通常平均每分钟约一条，普通评论至少间隔60秒；额度每60秒补一条、最多存两条，有趣的新事件可用短时额度。画面无实质变化、低兴趣、首轮或重复时沉默；受限候选丢弃，不排队补发。模型收到实际额度、近期评论及时间。

```bash
python3 "{baseDir}/scripts/kibitz.py" stop
python3 "{baseDir}/scripts/kibitz.py" once
python3 "{baseDir}/scripts/kibitz.py" notify-test
```

“停止陪伴”“别看了”“闭嘴”执行 stop，确认 running=false；停止会取消进行中的 agent 请求。停止观察不删除 OpenClaw 会话或已写入的记忆，下次开启继续同一会话。当前 agent 身份的普通会话不能换成精简上下文的子代理。内部观察事件不触发每日任务询问、workbar 或 TTS。

## 运行位置与排错

- 程序/提示词在 Kibitz 项目；`~/.kibitz/` 保存状态、会话诊断、偏好补充、消息队列和耗时/用量记录。直接读写同一 workspace/brain，不建第二套长期记忆。
- 临时图片在 `workspace/state/kibitz/`，正常停止时删除；OpenClaw 持续会话可能保留图像、文字和工具历史，按既有会话管理规则处理，不能声称停止就清掉全部历史。
- 复用现有 Gateway 和凭据，不读取/复制密钥，不改 OpenClaw 源码或配置。图片与启动上下文按正常 agent 流程提交给所选 provider。
- 截图失败检查屏幕录制权限；通知提交不等于一定可见，受通知权限与专注模式影响。后台允许三次连续失败后停止。
- 完整上下文、工具调用和历史可能增加耗时；请求串行，慢时实际观察频率下降，不积压。状态中可见模型实际 token 用量。
- 若启动报告缺关键文件、记忆工具、session key/workspace 不匹配或出现嵌入式回退，视为失败，不把它当作完整陪伴已接入。

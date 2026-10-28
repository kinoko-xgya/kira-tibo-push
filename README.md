# kira-tibo-push

KiraAI 插件：轮询 [tibo.cc](https://tibo.cc) 的 Codex 重置公告，有变化时推送到指定的 QQ 会话。

它不抓网页、不解析 HTML，只读官方那个 JSON 接口，用里面的 `id` 做去重：只要 id 变了就认为有新公告。

---

## 功能

- 变化推送，没变化完全安静（不刷屏）
- 两种投递方式：
  - `direct`：机器格式的原文播报，一眼看出是机器发的
  - `notice`：把事实丢给目标会话里的 agent，由它自己组织语言处理
- 接口挂了不吵闹：连续拉不到数据超过阈值才提醒一次
- 每轮写 `/tmp/tibo_push.heartbeat`，方便外部看门狗判断存活
- 自带 `tibo_status` 工具，模型可以手动拉一次并立即推送

## 安装

1. 把整个目录放进 KiraAI 的 `data/plugins/` 下
2. 依赖只有 `httpx`：`pip install -r requirements.txt`
3. 重扫插件（或重启 KiraAI）
4. 在插件设置里选一个**目标会话**，否则它只会写日志

> 注意：目标会话留空时不会“随便找个人发”，而是只写日志并告警。这样别人装上不会把消息推给系统里的第一个私聊。

## 配置

| 键 | 说明 | 默认 |
|---|---|---|
| `target_session` | 推送到哪个会话 | 空（只记日志） |
| `mode` | `direct` / `notice` | `direct` |
| `section_advanced.poll_seconds` | 轮询间隔，最小 60 | 300 |
| `section_advanced.fail_alert_seconds` | 接口连续失败多久才提醒 | 600 |

改完配置会触发插件重载，不需要手动重启；改 `main.py` 的工具签名才需要。

## 为什么没有「让模型改写」模式

早期版本有个 `llm_auto` 模式：先让模型判断值不值得打扰，再让模型改写成一句人话。结果是发出去的东西半人半机 —— 主人以为是 bot 人格说的，其实是一段被模板约束的模型输出。这种「像谁都不像」的中间态是最差的选择，所以整条链路砍掉了，只剩纯机器播报和全交给 agent 两档。

## 状态

- 状态存在插件数据目录的 `tibo_push_state.json`，删掉即重新开始，首次会补推一次当前最新公告。
- 与独立的 `tibo_monitor.py` 脚本版状态隔离，互不干扰。

## 已知限制

- 只对接 tibo.cc 一个源，换源要改 `API` 常量
- 只在 KiraAI 上测试过；`notice` 模式依赖宿主的 `publish_notice`，宿主不支持时会自动降级为 `direct`
- 时间按北京时间（UTC+8）格式化

## License

MIT

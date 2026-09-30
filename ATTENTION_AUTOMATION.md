# B 站独立日报自动化

每天 09:00（Asia/Singapore）运行一次 Docker 任务，统计前一天的历史。任务完成即退出，数据保存在宿主机。微信发送 `/bili` 时，Hermes 只读取已发布简报，不启动 Fetcher、不调用模型。

## 配置和凭据

普通参数在 `config/attention.json`。相对输出路径始终以本仓库根目录为准，默认 `output/attention`，不与官方软件的数据目录共用。未知配置项和错误类型会被拒绝；缺省参数使用内置默认值。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `timezone` | `Asia/Singapore` | 日期划分使用的 IANA 时区 |
| `schedule.time` | `09:00` | 每日运行时间 |
| `schedule.enabled` | `true` | Windows 自动运行开关；不禁止显式手动执行 |
| `sync.refresh_days` | `3` | 重新抓取近期数据，更新播放进度和已发布的近期日报 |
| `sync.catch_up_days` | `7` | 自首次发布日起，补齐此窗口内缺失的日报 |
| `sync.request_timeout_seconds` | `30` | 单次 HTTP 请求超时 |
| `sync.max_attempts` | `3` | 瞬时网络错误的最大尝试次数；登录失效不重试 |
| `job_timeout_minutes` | `20` | 容器内整次任务时限 |
| `report.top_n` | `5` | 完整报告中主要视频和 UP 主数量 |
| `report.topic_rules` | AI 关键词示例 | 按数组顺序对标题做不区分大小写的关键词匹配，首个匹配生效 |

未匹配规则时使用 B 站分区，分区缺失时归入“未分类”。完整分类保留全部组别，简报展示前三主题。每个视频只属于一个主题。观看时长使用同日同一播放条目的最大有效进度；分 P、番剧单集分别计数。跨天续播、倍速和重播不等同于真实使用时长。

在未纳入版本控制的 `secrets/bilibili.env` 中保存一行 `SESSDATA=实际凭据`。凭据只读挂载到容器，不进入镜像，也不写入任务日志。纯日报不需要 SMTP；登录失效时更新此文件即可。

## 构建、运行和调度

### 开机或唤醒后，一键手动生成

双击桌面的 **B站日报-立即生成**，或者仓库根目录的 `Run-Daily-Report.cmd`。启动器会在 Docker 未就绪时按需启动 Docker Desktop 并等待约三分钟，然后同步历史、生成昨天的日报，并按配置补齐近期遗漏日期。完成后窗口显示最新简报和报告目录，按回车关闭。

手动入口不受 09:00 时间和“今天已经执行过”的限制；同一天重跑会更新报告，不重复累加记录。重复打开的一键窗口会提示任务正在执行。若执行失败，窗口保留错误提示和启动日志位置，原有成功报告仍然可读。

电脑关机或休眠期间不能运行此本地任务，需要先开机或唤醒。启动器不需要提前打开 Fetcher，执行后日报容器自动退出，Docker Desktop 保持运行。微信 `/bili` 仍然只读已有简报。

重新创建桌面快捷方式：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/Install-AttentionShortcut.ps1
```

### 定时任务

Windows 需安装 Docker Desktop，启用 Linux containers，并先运行 `uv sync` 准备本项目 Python 环境。以下命令均在仓库根目录执行：

```powershell
docker compose -f docker-compose.attention-job.yml build
powershell.exe -NoProfile -File scripts/Run-AttentionJob.ps1
powershell.exe -NoProfile -File scripts/Apply-AttentionConfig.ps1
```

任务名为 `BiliAttention-DailyReport`，默认 09:00 执行，启用错过执行后的补跑，并在登录后检查是否需要运行。Docker Desktop 需要处于可用状态；这套配置在当前用户登录会话下工作，关机期间不会执行。Docker 不可用时记录失败并保留旧报告。Docker 引擎和 Hermes 网关不随日报容器退出。

修改时间或启用开关后，重新执行 `Apply-AttentionConfig.ps1`；其他配置在下一次执行时读取。Windows 触发器保存应用配置时的 UTC 偏移；默认新加坡时区没有夏令时。若改用有夏令时的地区，应在时区偏移变化后重新应用配置。可用 `-Preview` 查看任务定义，用 `-Uninstall` 仅移除此日报计划任务。

指定日期重跑：

```powershell
powershell.exe -NoProfile -File scripts/Run-AttentionJob.ps1 -Date 2026-09-29
```

调试时也可运行 `python -m scripts.attention_job`，使用相同 JSON 和凭据文件。`--check-config` 只验证并显示非敏感配置。自动运行与手动容器运行共享系统文件锁；不要同时从 Windows 原生 Python 和 Docker 跨系统写同一个目录。

## 文件与失败处理

输出目录包括：

- `history_by_date/`：按本地日期保存的原始历史，重复记录更新播放进度。
- `history.db`：SQLite 历史数据库，记录更新保留备注与删除标记。
- `reports/YYYY-MM-DD/版本号/`：`report.json`、`report.md` 和 `brief.txt`。
- `reports/index.json`：唯一发布索引，同时保存各日期的当前版本与最新日报日期。
- `run-state.json`、`runs/`：最近任务状态和各次运行结果。

首次只发布昨天的日报。抓取可能包含今天的记录，但今天不会生成日报。后续更新近期已发布报告并补齐遗漏日期；指定旧日期重跑不会把最新日报倒退。

抓取必须完整走到请求日期边界或接口末尾，分页异常不会当作成功。数据库完成事务后生成报告；全部文件写完才原子更新发布索引。网络失败、导入失败、超时、发布失败均保留上次已发布版本。成功且无历史记录时，正常生成零值报告；这是“接口未返回记录”，不是对真实使用情况的额外推断。

## Hermes / 微信

运行以下命令把 `/bili` 注册到现有 Hermes 配置，保留其他设置并创建配置备份：

```powershell
python -m scripts.register_attention_hermes --hermes-home "$env:HERMES_HOME"
```

通过 Hermes 官方网关管理功能重新加载配置。网关在线且微信已连接后，在微信发送 `/bili`。命令使用固定的绝对路径，只读报告索引、对应 JSON 和运行状态，不读取数据库或凭据，不启动 Docker，不经过模型。

读取器可独立检查：

```powershell
python scripts/attention_reader.py --output-dir output/attention
```

昨天日报缺失或任务失败时，会说明状态并返回最近成功简报；没有任何已发布报告时返回“尚未生成”。不主动推送消息。

## 云端迁移

可选的独立云端邮件适配层见 [Cloud daily-report delivery adapter](CLOUD_DELIVERY.md)，包括通用配置、用户自行输入凭据、发送状态账本与离线测试。调度、邮件发送及持久化存储需由外部集成提供。

复用同一镜像与命令，由云端定时任务启动容器。挂载持久化数据目录、只读 JSON 和只读凭据；可通过 `ATTENTION_HOST_OUTPUT`、`ATTENTION_CONFIG_FILE`、`ATTENTION_CREDENTIALS_FILE` 为 Compose 指定宿主机路径。容器内固定写 `/data`。

首版假定 Hermes 与报告在同一宿主机。迁移后需要让 Hermes 只读访问同一份已发布报告；未实现 Dots 专属部署或跨主机报告同步。

## 验证

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

自动测试覆盖抓取重试与分页失败、日期边界、进度更新、分 P/番剧、备注和删除标记、主题汇总、发布失败、运行锁、超时，以及读取器无需数据库或网络。真实验收需连续运行两次容器，比较相同日期统计结果并确认无重复插入，同时使用 Hermes 自带命令执行器检查 `/bili`。微信客户端实际收取消息还需在已连接的微信对话中触发。

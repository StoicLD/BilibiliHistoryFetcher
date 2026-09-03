# B站注意力日报

此扩展在每日历史记录抓取链完成后，从现有 SQLite 数据库生成一封简洁日报。它不修改数据库结构，也不改变抓取和导入格式。

## 统计口径

- “估算内容消费”取同一天、同一播放条目的最大有效 `progress`；已完成记录按 `duration` 计算，避免续播记录重复累加。
- 普通视频以 `bvid + cid`（缺失时回退到 `page`）区分分 P；番剧以 `epid/cid/oid` 区分单集。
- 互动补充记录不进入日报。直播等非视频历史仍计入“历史记录数”，但不进入视频时长与排行。
- 该时长反映内容播放进度，不等于真实屏幕使用时间。

## 配置与调用

在 `config/config.yaml` 中填写 `email`。如需 Apprise，同时启用 `apprise`，并将调度任务的 `send_apprise_report` 改为 `true`。

```text
GET  /attention/daily-report?date=2026-09-03&timezone=Asia/Singapore
POST /attention/daily-report/send
```

新安装默认每天 22:45 执行 `fetch_history`，随后串行导入、分析、生成热力图并发送日报。若已有 `output/scheduler.db`，YAML 不会覆盖现有任务；请在“计划任务”界面把抓取时间改为 `22:45`，并把末尾任务改为 `POST /attention/daily-report/send`，参数参考 `config/scheduler_config.yaml`。

## 独立 Docker 部署

扩展版使用独立镜像、容器、网络和端口，不会与官方默认实例重名：

```bash
docker compose -f docker-compose.attention.yml up -d --build
```

前端为 `http://localhost:5174`，后端为 `http://localhost:8900`。首次打开前端后，将服务地址设置为 `http://localhost:8900`。停止服务使用 `docker compose -f docker-compose.attention.yml down`；`config/` 与 `output/` 保留在宿主机。

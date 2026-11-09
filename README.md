# TCP 连接数监控

独立 Linux 工具，不需要修改或重启 Java 应用。依赖 Python **3.8+**、`ss`（Debian/Ubuntu 的 `iproute2` 或 RHEL/CentOS 的 `iproute`），Python 不需要安装第三方包。HTML 使用原生 Canvas，无 CDN 依赖。

## 启动

推荐使用同目录下的 `tcp-monitor.sh` 管理后台进程。需要 Bash、`flock`（util-linux），以及前述 Python / ss。无需手动执行 `nohup`，也无需给脚本添加执行权限：

```bash
# 保留 30 天，监听多个本机端口，通过服务器 IP 的 8765 端口访问网页
bash tcp-monitor.sh start --days 30 --ports 40002,40000,11000 \
  --bind 0.0.0.0 --http-port 8765

# 查看状态、停止
bash tcp-monitor.sh status
bash tcp-monitor.sh stop

# 调整保留时间或监控端口后重启，沿用原来的数据目录
bash tcp-monitor.sh restart --days 7 --ports 40002,40000 \
  --bind 0.0.0.0 --http-port 8765

# 监听所有本机 TCP 连接；关闭 HTTP，仅写入文本
bash tcp-monitor.sh start --days 7 --ports all --http-port 0

# 自定义数据目录和日志位置
bash tcp-monitor.sh start --days 7 --ports 40002,40000 \
  --output-dir /var/lib/tcp-monitor --log-file /var/log/tcp-monitor.log
bash tcp-monitor.sh status --output-dir /var/lib/tcp-monitor
bash tcp-monitor.sh stop --output-dir /var/lib/tcp-monitor

bash tcp-monitor.sh --help
```

| 参数 | 用途 / 默认值 |
| --- | --- |
| `--days` | 保存天数，默认值取脚本顶部的 `DAYS` |
| `--ports` | 单个或多个本机端口，多个用逗号或空格分隔；默认值取 `PORTS`，`all` 表示整机全部 |
| `--bind` | HTTP 绑定地址，默认值取 `BIND`；`0.0.0.0` 可通过服务器 IP 访问 |
| `--http-port` | HTTP 端口，默认 `8765`；`0` 关闭网页服务 |
| `--state` | TCP 状态，默认 `established` |
| `--output-dir` | 数据及 PID 所在目录，默认脚本旁的 `data/` |
| `--log-file` | 启动及错误日志，默认脚本旁的 `monitor.log` |
| `--python` | Python 解释器路径，默认 `python3` |

也可修改启动脚本顶部的默认值，或设置 `TCP_MONITOR_DAYS`、`TCP_MONITOR_PORTS`、`TCP_MONITOR_BIND`、`TCP_MONITOR_HTTP_PORT`、`TCP_MONITOR_STATE`、`TCP_MONITOR_OUTPUT_DIR`、`TCP_MONITOR_LOG_FILE`、`TCP_MONITOR_PYTHON` 环境变量。命令行参数优先于环境变量。

启动脚本统一使用一个 `--ports` 参数，单端口示例为 `--ports 40002`，多端口示例为 `--ports 40002,40000,11000`。也可以直接在脚本顶部设置默认端口，然后执行 `bash tcp-monitor.sh start`：

```bash
PORTS="${TCP_MONITOR_PORTS:-40002,40000,11000}"
```

`start` 已经在运行时不会重复启动；`restart` 使用本次命令提供的参数和默认值，**不会自动记住上次命令行参数**。使用自定义 `--output-dir` 后，查看状态、停止和重启时应指定同一个目录。首次从手动 `nohup` 或 systemd 切换到本启动脚本时，先停止原进程；启动脚本只管理自己记录的进程。状态命令返回 `0` 表示运行中、`3` 表示未运行。每次启动覆盖启动日志，历史采样文本继续按保留天数续写和清理。

以下是直接运行 Python 的用法，仍然可用：

把 `tcp_monitor.py` 和 `index.html` 放到服务器同一目录，例如 `/opt/tcp-monitor/`。以下命令在该目录运行：

```bash
# 统计整机 ESTABLISHED，每 60 秒一次，只保留最近 7 天
python3 tcp_monitor.py --days 7

# 只统计机器人接入端口：40002 是示例，请替换成线上实际端口
python3 tcp_monitor.py --days 30 --port 40002

# 同时采集多个本机端口，页面可切换单个端口或查看合计
python3 tcp_monitor.py --days 7 --port 40002 --port 40000

# 包含 LISTEN、TIME-WAIT 等所有 TCP 状态
python3 tcp_monitor.py --days 7 --state all

# 采集一次以检查环境，输出本次记录并退出
python3 tcp_monitor.py --days 7 --port 40002 --once

# 不启动网页服务；仅输出文本
python3 tcp_monitor.py --days 7 --http-port 0
```

默认统计当前网络命名空间内的 IPv4/IPv6 TCP socket；指定 `--port` 时按**本机端口（sport）**过滤。需要宿主机连接数时就在宿主机运行，容器内运行则只能看到其所在网络命名空间。默认 `established` 是已建立连接数，不等于完成登录认证的机器人数量。

启动后立即采集一次，之后每 60 秒采集。没有补采历史数据。可用 `--state`：`established`、`all`、`time-wait`、`close-wait`、`syn-recv`、`syn-sent`。

## 查看 HTML

默认启用网页服务 `http://127.0.0.1:8765/`。从自己电脑通过 SSH 转发后，用浏览器打开这个地址：

```bash
# 在自己电脑执行；用户名、服务器地址替换成实际值
ssh -N -L 8765:127.0.0.1:8765 用户名@服务器地址
```

如需通过服务器 IP 直接访问：

```bash
python3 tcp_monitor.py --days 7 --port 40002 --bind 0.0.0.0 --http-port 8765
```

此时访问 `http://服务器IP:8765/`，需要网络允许访问该端口。内置 HTTP 服务不含登录认证，仅提供 HTML 和采样文本两个资源；默认只绑定本机地址。

页面每 60 秒自动读取一次 `tcp_connections.txt`，支持：

- 端口切换：选择“全部端口合计”或某个监控端口，趋势、统计卡片、悬浮数值及记录表同步更新。切换端口保留当前时间范围，自动刷新保留端口选择。
- 监控端口集合可以在重启时调整并续写同一个文本。下拉框包含保留期内曾监控的端口；单端口视图中未监控该端口的历史点显示为缺测，不按 0 处理。合计视图在相邻采样的端口集合变化处断开并提示，明细和悬浮提示显示每次实际监控的端口。
- 快捷时间范围：最近 1 小时、6 小时、24 小时、7 天、全部保留数据。
- 自定义开始、结束时间，点击“应用时间范围”；边界包含在查询内，时间以浏览器本地时区为准。
- 所选区间的末次连接数、峰值、均值、缺测数量及最近 60 条记录。均值排除缺测，末次连接数在末次采集失败时显示 `—`。
- 曲线鼠标悬浮查看时间和数值；缺测或间隔超过 90 秒时断开曲线。
- 将服务器的 `data/tcp_connections.txt` 下载到本机，双击打开 `index.html`，点击“导入文本”离线查看。离线文件不会自动追踪磁盘更新，更新后重新导入；离线快捷范围相对文件中的最新采样时间计算。

网页查看范围不会更改保留天数，已清理的数据无法通过选择更大的查看范围恢复。过长的曲线会按像素保留极值后绘制，统计值始终由完整的区间样本计算。

## 文本和磁盘保留

默认数据位置为脚本旁的 `data/tcp_connections.txt`，可以用 `--output-dir /var/lib/tcp-monitor` 修改。

文本使用 JSON Lines，每行是一分钟的快照，`timestamp` 为 Unix 秒时间戳：

```json
{"timestamp":1793923200,"state":"established","ports":[40000,40002],"count":28,"port_counts":{"40000":10,"40002":18}}
{"timestamp":1793923260,"state":"established","ports":[40000,40002],"count":null,"port_counts":{"40000":null,"40002":null},"error":"ss_timeout"}
```

`count` 是合计，`port_counts` 是按本机端口分别统计的数量。每分钟只执行一次 `ss`，从同一份快照提取各端口计数；没有连接的端口记录为 0。不指定 `--port` 时只统计整机总数，避免保存大量临时端口明细造成文件膨胀；要按端口切换，请显式传入多个 `--port`。

`count: null` 表示采集失败，不会误记为 0。`ss` 执行超时为 20 秒。失败原因包括 `ss_timeout`、`ss_unavailable`、`ss_exit_退出码`、`ss_output_unrecognized`（输出格式无法识别）。

升级时替换 `tcp_monitor.py` 和 `index.html`，停止旧采集进程后重新启动即可续写历史。增加或减少 `--port`、在指定端口与整机统计之间切换时，无需改变 `--output-dir`。每条历史记录保留它当时的端口集合，不会被重写为新配置。

旧版单端口记录可以直接按该端口展示；旧版多个端口只有合计，无法补出各端口历史，切换到单个端口时显示为缺测并提示原因，合计视图仍可正常查看。合计视图的峰值和均值使用所选区间内各次采样的实际合计；区间跨越端口集合变化时会提示这些统计值包含不同的监控范围。

- `--days N` 必须是正整数，默认 7 天，滚动保留最近 `N × 24` 小时。运行时每次采样清理一次，重启时也先清理。
- 样本数量最多 `N × 1440 + 1` 条，防止重复重启导致文件无限增长。每条记录通常约 100～200 字节，大小随指定端口数增长；只记录指定端口的明细。
- 每分钟使用临时文件写入并原子替换文本，网页不会读到半截记录；写入瞬间需要容纳新旧两份文件。正常运行不写持续增长的访问日志或采样日志。
- 同一输出目录只允许一个采集进程。端口集合和 `--days` 可在重启时调整，所有端口范围的历史统一按新保留天数清理；`--state` 改变时仍需指定不同的输出目录，防止将不同 TCP 状态的数量混用。
- 进程停止期间不会执行清理，也不会继续产生数据。文本内过期内容将在下次启动清理。

## 后台运行

简单后台运行（停止时使用 `kill PID`，不要反复启动多个实例）：

```bash
nohup python3 -u tcp_monitor.py --days 7 --port 40002 > monitor.log 2>&1 &
echo $!
```

采样失败记录在文本中，不会每分钟向 `monitor.log` 重复输出日志。长期使用建议配置 systemd，自动启动和异常退出后重启：

```ini
# /etc/systemd/system/tcp-monitor.service
[Unit]
Description=TCP connection count monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/tcp-monitor
ExecStart=/usr/bin/python3 -u /opt/tcp-monitor/tcp_monitor.py --days 7 --port 40002 --output-dir /var/lib/tcp-monitor
StateDirectory=tcp-monitor
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

确认 Python 路径和 TCP 端口后启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tcp-monitor
sudo systemctl status tcp-monitor
```

验证工具自身逻辑（跨平台可执行，不会连接真实服务器）：

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v

# 启动脚本回归，使用临时目录和模拟采集进程，不扫描真实 TCP；
# Git Bash 可检查语法、参数校验及参数传递，完整进程生命周期验证须在 Linux 执行。
bash test_launcher.sh
```

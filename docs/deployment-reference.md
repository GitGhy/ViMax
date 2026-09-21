# ViMax 部署附录

日常部署先看 [精简部署步骤](deployment.md)。本附录保留完整的环境安装、常驻服务、反向代理、备份迁移和排错说明，按需查阅。

本文对应 `codex/secondary-development` 分支的光合云版本，代码基线为 `b5b2c4b`，编写日期为 2026-09-20。按 **Ubuntu 24.04、单台服务器、单个 ViMax 实例** 部署，目录统一为 `/opt/vimax/app`，运行账户为 `vimax`。其他系统需要调整系统依赖和服务管理命令。

当前版本已在本地完成一轮生成，本文的服务器配置示例需要在目标服务器上按步骤验证。光合云接口细节见 [光合云 AI 接入说明](ghyai.md)。

## 1. 部署结构与适用范围

```text
浏览器
  └─ SSH 隧道，或 Nginx HTTPS + 访问认证
       └─ Node.js Web 服务：127.0.0.1:4173
            ├─ 提供构建后的网页和 /api/* 接口
            ├─ 通过 /api/events 推送 SSE 进度
            └─ 启动 Python 智能体子进程
                 ├─ 调用光合云 API
                 └─ 读写项目目录中的配置、日志和生成产物
```

- Python 智能体通过标准输入/输出与 Node.js 通信，不需要另起 Uvicorn、Gunicorn 或一个 Python HTTP 端口。
- 模型推理由光合云完成；服务器负责接口调用、图片处理、视频下载和拼接，这条部署路径不要求本地 GPU。
- 业务数据存放在本地文件中，不需要配置 MySQL、PostgreSQL 或 Redis。
- 一个 Web 进程只有一个活动智能体；切换项目会停止原智能体。当前适合个人或共享工作区使用，不提供多用户项目隔离，也不要用多个进程同时操作同一套数据目录。
- 当前 Web 接口没有内置登录认证，包括生成、配置修改和项目删除接口。默认保留回环地址，通过 SSH 隧道访问；需要域名访问时，使用第 7 节的 HTTPS 和访问认证配置。

## 2. 准备服务器环境

以下命令在服务器的管理员账户中执行，需要 `sudo` 权限。

安装系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates tar nano ffmpeg libgl1 libglib2.0-0t64
```

`ffmpeg` 用于视频处理；`libgl1` 和 GLib 是当前 OpenCV 包在 Linux 上导入时需要的运行库。这里的 GLib 包名适用于 [Ubuntu 24.04](https://packages.ubuntu.com/en/noble/libs/libglib2.0-0t64)。

使用 Node.js 24。若服务器已经安装该版本，可跳过安装命令。下面采用 [NodeSource 的 24.x 安装脚本](https://github.com/nodesource/distributions/blob/master/scripts/deb/setup_24.x)，安装后的可执行文件应为 `/usr/bin/node`：

```bash
curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/vimax-nodesource-setup.sh
sudo bash /tmp/vimax-nodesource-setup.sh
sudo apt-get install -y nodejs
node --version
npm --version
command -v node
ffmpeg -version
```

如果使用 NVM 或其他路径下的 Node.js，后续 systemd 的 `ExecStart` 必须改成 `command -v node` 输出的绝对路径；systemd 不会自动加载交互终端中的 NVM 配置。

创建专用运行账户和目录。下面两条命令用于首次部署；账户已经存在时不用重复创建：

```bash
sudo useradd --system --create-home --home-dir /opt/vimax --shell /bin/bash vimax
sudo install -d -o vimax -g vimax -m 0750 /opt/vimax/app
```

服务器需要能访问 Python/npm 软件源和 `https://ghy-ai.com`。无需对公网开放 4173 端口。

## 3. 上传当前版本并安装依赖

### 3.1 从当前开发电脑打包

当前二次开发代码已在本地提交；不假设该分支已经推送到远程仓库。下面从已提交的 `HEAD` 打包，不包含本地 Key、历史项目、虚拟环境或生成产物。

在开发电脑执行，将 `deploy@YOUR_SERVER` 替换为实际服务器登录账户和地址：

```bash
cd /home/ghy/PycharmProjects/ViMax
git status --short
git log -1 --oneline
git archive --format=tar.gz --output=/tmp/vimax-release.tar.gz HEAD
scp /tmp/vimax-release.tar.gz deploy@YOUR_SERVER:/tmp/vimax-release.tar.gz
```

`git archive HEAD` 只包含已提交文件。后续修改要先提交，再重新打包。迁移现有项目数据另见第 9 节。

在服务器管理员账户中解压到首次创建的空目录：

```bash
sudo tar -xzf /tmp/vimax-release.tar.gz -C /opt/vimax/app
sudo chown -R vimax:vimax /opt/vimax/app
```

### 3.2 安装 Python 和项目依赖

切换到运行账户，后续安装、配置和手动启动命令均在该账户中执行：

```bash
sudo -iu vimax
```

使用 [uv 官方安装器](https://docs.astral.sh/uv/getting-started/installation/) 安装 uv，再安装 Python 3.12：

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /opt/vimax/uv-install.sh
sh /opt/vimax/uv-install.sh
/opt/vimax/.local/bin/uv --version
/opt/vimax/.local/bin/uv python install 3.12
cd /opt/vimax/app
/opt/vimax/.local/bin/uv sync --locked --python 3.12 --no-dev
```

`--locked` 使用仓库锁定的依赖，锁文件与项目不一致时直接报错；`--no-dev` 跳过 Python 测试依赖。参数含义见 [uv 同步说明](https://docs.astral.sh/uv/concepts/projects/sync/)。不要将开发电脑的虚拟环境复制到服务器，按上述命令重新创建。

安装 Web 依赖并构建网页：

```bash
npm --prefix /opt/vimax/app/web ci --include=dev
npm --prefix /opt/vimax/app/web run build
mkdir -p /opt/vimax/app/.vimax /opt/vimax/app/.working_dir
```

`npm ci` 使用 `package-lock.json`。构建需要 Vite 等开发依赖，因此这里使用 `--include=dev`。构建产物位于 `/opt/vimax/app/web/dist`。

检查 Python 运行库：

```bash
/opt/vimax/app/.venv/bin/python --version
FFMPEG_BINARY=/usr/bin/ffmpeg /opt/vimax/app/.venv/bin/python -c 'import cv2, moviepy, httpx, yaml; print("Python 运行依赖正常")'
```

## 4. 配置光合云

仍使用 `vimax` 账户。首次创建配置时复制示例；已经存在的配置不会被以下命令覆盖：

```bash
test -e /opt/vimax/app/configs/agent.local.yaml || cp /opt/vimax/app/configs/agent.ghyai.example.yaml /opt/vimax/app/configs/agent.local.yaml
chmod 600 /opt/vimax/app/configs/agent.local.yaml
nano /opt/vimax/app/configs/agent.local.yaml
```

主要配置如下，将占位文字替换为服务器要使用的 API Key：

```yaml
llm:
  model_provider: openai
  model: qwen3.8-flash
  tool_mode: json
  base_url: https://ghy-ai.com/v1
  api_key: '填入你的 API Key'

image:
  model: doubao-seedream-5.0-lite
  base_url: https://ghy-ai.com/v1
  api_key: ''

video:
  model: doubao-seedance-2.0
  base_url: https://ghy-ai.com/v1
  api_key: ''
```

图片和视频的 Key 留空时使用语言模型的 Key。模型名称以该 Key 实际开放的能力为准；默认 JSON 工具模式与当前适配器配套。小说检索流程还需要另外配置 `embedding` 和 `reranker`，创意/剧本生成路径不要求它们。

在项目根目录检查模型和能力；该命令只查询目录，不提交生成任务：

```bash
cd /opt/vimax/app
/opt/vimax/app/.venv/bin/python -m scripts.check_ghyai
```

如需测试真实调用，可加 `--chat`、`--image` 或 `--video`，这些参数会产生 API 用量，具体用法见光合云接入说明。

环境变量优先于 YAML。Node.js Web 进程不会自动读取仓库根目录的 `.env`；通过 systemd 的 `Environment` 或明确配置的 `EnvironmentFile` 注入环境变量，使 Web 进程及其 Python 子进程均能获得配置。常用选项如下：

| 变量 | 默认值或本文设置 | 用途 |
| --- | --- | --- |
| `VIMAX_WEB_HOST` | `127.0.0.1` | Web 监听地址 |
| `VIMAX_WEB_PORT` | `4173` | Web 端口 |
| `VIMAX_PYTHON_CMD` | 本文指定虚拟环境 Python 的绝对路径 | 确保子进程使用正确依赖 |
| `VIMAX_WEB_UPLOAD_MAX_BYTES` | `104857600` | 单次上传上限，100 MiB |
| `VIMAX_LLM_API_KEY` | 未设置时读取本地配置 | 覆盖模型 Key，也可作为图片/视频 Key 的回退值 |
| `VIMAX_GHYAI_TOOL_MODE` | `json` | 智能体工具协议 |
| `VIMAX_NARRATIVE_MAX_TOKENS` | 光合云为 `16384` | 规划模型单次输出上限 |
| `VIMAX_LLM_REQUEST_TIMEOUT_SECONDS` | `300` | 规划模型单次请求超时 |
| `VIMAX_NARRATIVE_STEP_TIMEOUT_SECONDS` | `900` | 单个叙事规划步骤超时 |
| `VIMAX_VIDEO_QUERY_TIMEOUT_SECONDS` | `600` | 单次视频轮询总超时 |
| `VIMAX_VIDEO_POLL_INTERVAL_SECONDS` | `5` | 视频状态查询间隔 |
| `VIMAX_VIDEO_REQUEST_TIMEOUT_SECONDS` | `60` | 视频接口单次请求超时 |

如果设置页保存的配置似乎没有生效，先检查服务环境中是否存在同名覆盖值。手动修改 YAML 后要重新启动智能体；使用第 6 节服务时可在空闲时重启服务。

## 5. 首次手动启动

使用 `vimax` 账户，从项目根目录运行生产服务：

```bash
cd /opt/vimax/app
VIMAX_WEB_HOST=127.0.0.1 \
VIMAX_WEB_PORT=4173 \
VIMAX_PYTHON_CMD=/opt/vimax/app/.venv/bin/python \
FFMPEG_BINARY=/usr/bin/ffmpeg \
IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg \
/usr/bin/node /opt/vimax/app/web/server.mjs
```

该命令提供已构建的生产网页。仓库的 `./vimax web start` 也是生产入口；`./vimax web` 默认运行开发模式。

另开一个服务器终端检查：

```bash
curl -fsS http://127.0.0.1:4173/api/health
curl -N --max-time 20 http://127.0.0.1:4173/api/events
```

首次健康检查通常返回：

```json
{"ok":true,"agentRunning":false,"activeSessionId":""}
```

`agentRunning: false` 表示还没有从网页打开工作区，不代表 Web 服务异常。SSE 检查应先收到 `data:` 事件，随后约每 15 秒收到 `: keepalive`；20 秒后的 curl 超时是此检查主动设置的结束条件。

没有域名时，可在开发电脑建立 SSH 隧道：

```bash
ssh -N -L 127.0.0.1:14173:127.0.0.1:4173 deploy@YOUR_SERVER
```

浏览器打开 `http://127.0.0.1:14173`。这里使用 14173 避免与开发电脑原有的 4173 服务冲突。服务器上的 4173 仍只监听回环地址。

检查完成后，在运行 Node.js 的终端按 `Ctrl+C`，再输入 `exit` 回到管理员账户，配置常驻服务。

## 6. 配置 systemd 常驻运行

以管理员身份创建 `/etc/systemd/system/vimax.service`：

```bash
sudo tee /etc/systemd/system/vimax.service > /dev/null <<'EOF'
[Unit]
Description=ViMax Web 与智能体服务
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=vimax
Group=vimax
WorkingDirectory=/opt/vimax/app
Environment=VIMAX_WEB_HOST=127.0.0.1
Environment=VIMAX_WEB_PORT=4173
Environment=VIMAX_PYTHON_CMD=/opt/vimax/app/.venv/bin/python
Environment=PYTHONUNBUFFERED=1
Environment=FFMPEG_BINARY=/usr/bin/ffmpeg
Environment=IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg
ExecStart=/usr/bin/node /opt/vimax/app/web/server.mjs
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

sudo systemd-analyze verify /etc/systemd/system/vimax.service
sudo systemctl daemon-reload
sudo systemctl enable --now vimax
sudo systemctl status vimax --no-pager
curl -fsS http://127.0.0.1:4173/api/health
```

Node.js 的子进程继承上面的 Python 路径和 FFmpeg 配置。服务停止时，systemd 同时清理该服务的 Python/FFmpeg 子进程。

常用管理命令：

```bash
sudo journalctl -u vimax -n 100 --no-pager
sudo journalctl -u vimax -f
sudo systemctl restart vimax
sudo systemctl stop vimax
sudo systemctl start vimax
```

systemd 会在 Web 主进程异常退出后重启服务，不会自动续跑被中断的生成流程。升级或重启前先等待当前任务完成；远端视频任务可能仍在运行，应保留任务记录，恢复查询而不是盲目重新创建。

## 7. 可选：Nginx、HTTPS 与访问认证

只通过 SSH 隧道使用时可以跳过本节。需要域名访问时，先将域名解析到服务器，并准备该域名的有效证书和私钥；示例统一使用 `vimax.example.com`，部署时全部替换。

安装 Nginx 和密码文件工具，创建访问账户。这里的账户与光合云 API Key 无关，密码由命令交互输入：

```bash
sudo apt-get install -y nginx apache2-utils
sudo htpasswd -c /etc/nginx/vimax.htpasswd vimaxviewer
sudo chown root:www-data /etc/nginx/vimax.htpasswd
sudo chmod 640 /etc/nginx/vimax.htpasswd
sudo install -d -m 0750 /etc/nginx/ssl
```

`htpasswd -c` 仅在首次创建密码文件时使用；以后新增或修改账户去掉 `-c`。

将证书链放在 `/etc/nginx/ssl/vimax.fullchain.pem`，私钥放在 `/etc/nginx/ssl/vimax.privkey.pem`，私钥由 root 持有并设置为 `600`。也可以在下面配置中使用已有证书管理工具提供的路径；证书续期后要 reload Nginx。证书配置见 [Nginx HTTPS 文档](https://nginx.org/en/docs/http/configuring_https_servers.html)。

创建 `/etc/nginx/sites-available/vimax`，写入以下内容：

```nginx
server {
    listen 80;
    server_name vimax.example.com;
    return 301 https://vimax.example.com$request_uri;
}

server {
    listen 443 ssl;
    server_name vimax.example.com;

    ssl_certificate /etc/nginx/ssl/vimax.fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/vimax.privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    auth_basic "ViMax";
    auth_basic_user_file /etc/nginx/vimax.htpasswd;
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:4173;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

认证覆盖整个站点及全部 API。`proxy_buffering off` 用于及时转发 SSE 进度，较长的读取超时用于保留事件连接；这些配置不改变光合云接口自身的超时。参考 [Nginx 代理说明](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) 和 [访问认证说明](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)。

在证书和域名均已配置后启用站点：

```bash
sudo ln -s /etc/nginx/sites-available/vimax /etc/nginx/sites-enabled/vimax
sudo nginx -t
sudo systemctl reload nginx
curl -u vimaxviewer -fsS https://vimax.example.com/api/health
curl -u vimaxviewer -N --max-time 20 https://vimax.example.com/api/events
```

软链接已经存在时跳过创建命令。防火墙/云安全组按访问方式放行 SSH，以及本节需要的 80/443；保持 4173 不对公网开放。浏览器登录后可共享同一工作区，认证本身不增加 ViMax 多用户隔离能力。

## 8. 验收部署

完成以下一次实际操作，确认 Web、Python 和光合云都能协同工作：

1. 健康接口返回 `ok: true`，网页能打开，SSE 持续收到事件。
2. 在网页新建一个项目；此时健康接口的 `agentRunning` 应变为 `true`。
3. 先选择 `idea2video`，生成一个场景、3～5 个镜头的短视频规划。
4. 确认规划完成后进入渲染，检查进度、参考图和镜头视频。
5. 检查最终成片能预览，并确认对应项目目录中存在 `final_video.mp4`。
6. 在任务结束后重启服务，确认项目、历史展示和产物仍存在。

生成验收会产生 API 用量。健康接口只检查 Web 进程状态，不能替代模型能力检查或完整生成验收。

## 9. 数据保存、备份与迁移

必须保留以下内容：

| 服务器路径 | 数据 |
| --- | --- |
| `/opt/vimax/app/.vimax` | 会话索引、聊天/工具日志、偏好、待办和摘要 |
| `/opt/vimax/app/.working_dir` | 项目文档、上传文件、图片、视频、渲染状态及光合云任务记录 |
| `/opt/vimax/app/configs/agent.local.yaml` | 模型地址和 API Key |

它们默认被 Git 忽略，代码包不会自动携带这些数据。备份包含明文 Key，应保存到受控目录；持久化目录不要改为临时目录。

在任务结束后，以管理员身份执行一致性备份：

```bash
sudo systemctl stop vimax
vimax_backup_dir="/var/backups/vimax/$(date +%Y%m%d-%H%M%S)"
sudo install -d -m 0700 "$vimax_backup_dir"
sudo tar -czf "$vimax_backup_dir/data.tar.gz" -C /opt/vimax/app .vimax .working_dir configs/agent.local.yaml
sudo chmod 600 "$vimax_backup_dir/data.tar.gz"
sudo cp /etc/systemd/system/vimax.service "$vimax_backup_dir/vimax.service"
sudo systemctl start vimax
```

如果使用 Nginx，同时保留自己的站点配置、认证文件和证书管理配置。检查 tar 命令成功后再使用该备份恢复。

迁移现有开发电脑数据时，先停止开发电脑中的 ViMax，再从 `/home/ghy/PycharmProjects/ViMax` 打包同样的 `.vimax`、`.working_dir` 和本地配置，通过 SSH 传到服务器。不需要迁移 `.venv`、`node_modules` 或 `.idea`。

在**新部署、尚无项目数据**的目标工作区恢复，下面假设备份已上传到 `/tmp/vimax-data.tar.gz`：

```bash
sudo systemctl stop vimax
sudo tar -xzf /tmp/vimax-data.tar.gz -C /opt/vimax/app
sudo chown -R vimax:vimax /opt/vimax/app/.vimax /opt/vimax/app/.working_dir
sudo chown vimax:vimax /opt/vimax/app/configs/agent.local.yaml
sudo chmod 600 /opt/vimax/app/configs/agent.local.yaml
sudo systemctl start vimax
```

不要把两套会话索引直接覆盖混合。光合云任务记录必须与生成产物一起迁移，并保留创建任务时使用的 Key，才能继续恢复查询。历史 JSON 文件中可能记录开发电脑的绝对素材路径；迁移到不同根目录后，继续编辑或渲染旧项目时若出现路径不存在，需要重新选择素材或修正对应缓存中的路径，不能只凭网页能打开就认定断点恢复已通过。

重启后网页可从日志恢复聊天展示，智能体会重新读取项目文件和已保存摘要；它不会自动将全部历史聊天重新装入模型上下文。

## 10. 更新与回退

升级前先完成第 9 节备份，并保留旧代码包或提交号。当前上传方式是 `git archive`，服务器目录里没有 `.git`，不能直接在该目录运行 `git pull`。

升级时停止服务，将旧应用目录整体保留到另一个位置，在 `/opt/vimax/app` 建立新的空目录并解压新代码包，恢复备份数据，再以 `vimax` 账户重新执行 Python 依赖安装、Web 依赖安装和构建。检查文件所有者正确后启动服务，执行健康检查和一次项目读取。

若新版本启动失败，停止服务，把旧目录恢复到 `/opt/vimax/app`，再启动服务。回退代码不等于自动回退期间产生的数据变更；需要恢复数据时使用对应版本备份，先保留当前数据副本。

## 11. 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 网页返回 502 | 先访问服务器本地 `/api/health`，再检查 `systemctl status vimax`、服务日志及代理端口 |
| `agentRunning: false` | 在网页打开/新建项目；若仍失败，检查 Python 路径、Key 和 `journalctl` 中的子进程错误 |
| `uv: command not found` 或 Python 缺包 | systemd 使用明确的 `VIMAX_PYTHON_CMD=/opt/vimax/app/.venv/bin/python`，不要依赖交互终端 PATH |
| 缺少 `libGL.so.1` 或 `libgthread-2.0.so.0` | 检查第 2 节的 OpenCV 系统运行库是否安装 |
| FFmpeg 找不到或视频拼接失败 | 检查 `/usr/bin/ffmpeg`、服务环境、磁盘空间，并用运行账户导入 MoviePy |
| 网页提示找不到构建文件 | 以 `vimax` 账户运行 Web 构建，确认 `/opt/vimax/app/web/dist/index.html` 存在 |
| 看不到实时进度 | 检查 `/api/events` 是否持续返回事件、反向代理是否缓冲 SSE、认证是否覆盖该接口 |
| 上传返回 413 | 同时检查 Nginx 的 `client_max_body_size` 和 `VIMAX_WEB_UPLOAD_MAX_BYTES` |
| API 返回 401/403 | 检查 Key 和授权能力；使用运行账户在项目根目录执行模型检查脚本 |
| 视频轮询超时 | 保留镜头旁的 `.ghyai.json`，按已有视频 ID 恢复查询；不要为重试直接删除任务记录 |
| 输出被截断 | 检查 `output_length_exceeded`，调整规划输出额度或缩短任务；不要无限重试 |
| 文件写入权限错误 | 检查数据目录、本地配置及其父目录是否允许 `vimax` 账户写入；配置保存需要创建临时文件 |
| 配置修改未生效 | 检查环境变量覆盖，并在空闲时重新打开工作区或重启服务 |

磁盘使用情况可直接检查：

```bash
sudo du -sh /opt/vimax/app/.working_dir /opt/vimax/app/.vimax
df -h /opt/vimax/app
```

应用日志位于 `/opt/vimax/app/.vimax/logs`，服务进程日志通过 `journalctl -u vimax` 查看。光合云错误里的 `X-Request-ID` 与平台聊天调用日志的数字业务 ID 可能不同，排查时同时记录时间、模型和错误信息。

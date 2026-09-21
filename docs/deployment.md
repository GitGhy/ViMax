# ViMax 快速部署

适用于 Ubuntu 24.04，直接使用当前 **`ubuntu` 账户**，项目放在 `/home/ubuntu/ghy/ViMax`。先按 [部署附录](deployment-reference.md) 安装 Git、Node.js 24、uv、FFmpeg 和系统依赖。

## 1. 拉取代码并构建

首次部署执行以下命令；已经克隆过则跳过 `git clone`：

```bash
mkdir -p /home/ubuntu/ghy
git clone -b codex/secondary-development https://github.com/GitGhy/ViMax.git /home/ubuntu/ghy/ViMax
cd /home/ubuntu/ghy/ViMax
/home/ubuntu/.local/bin/uv sync --locked --python 3.12 --no-dev
npm --prefix /home/ubuntu/ghy/ViMax/web ci --include=dev
npm --prefix /home/ubuntu/ghy/ViMax/web run build
```

## 2. 填写光合云 Key

```bash
cd /home/ubuntu/ghy/ViMax
test -e configs/agent.local.yaml || \
  cp configs/agent.ghyai.example.yaml configs/agent.local.yaml
chmod 600 configs/agent.local.yaml
nano configs/agent.local.yaml
```

填写 `llm.api_key`，按 `Ctrl+O`、回车保存，再按 `Ctrl+X` 退出。其他模型和接口地址保留示例值；图片、视频的 Key 留空会复用这个 Key。

检查连接和模型权限：

```bash
cd /home/ubuntu/ghy/ViMax
/home/ubuntu/ghy/ViMax/.venv/bin/python -m scripts.check_ghyai
```

## 3. 启动生产服务

```bash
cd /home/ubuntu/ghy/ViMax
VIMAX_WEB_HOST=127.0.0.1 \
VIMAX_PYTHON_CMD=/home/ubuntu/ghy/ViMax/.venv/bin/python \
FFMPEG_BINARY=/usr/bin/ffmpeg \
IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg \
node /home/ubuntu/ghy/ViMax/web/server.mjs
```

保持终端运行。需要后台常驻和开机启动时，使用附录中的 systemd 配置。

## 4. 打开网页

**公网 IP 直接访问：** 将上面的 `VIMAX_WEB_HOST` 改为 `0.0.0.0` 并重新启动，安全组和防火墙允许你电脑的公网 IP 访问 TCP 4173，再打开 `http://服务器公网IP:4173`。应用没有登录验证，请限制访问来源。systemd 对应设置见 [常驻服务](deployment-reference.md#6-配置-systemd-常驻运行)。

**SSH 隧道访问：** 保留 `127.0.0.1`，在自己的电脑执行，将 `YOUR_SERVER` 换成服务器地址：

```bash
ssh -N -L 127.0.0.1:14173:127.0.0.1:4173 ubuntu@YOUR_SERVER
```

保持 SSH 终端运行，浏览器打开 **http://127.0.0.1:14173**，新建项目即可使用。域名和 HTTPS 配置见附录。

服务器健康检查：

```bash
curl -fsS http://127.0.0.1:4173/api/health
```

返回 `ok: true` 表示 Web 服务已启动；打开项目之前 `agentRunning: false` 是正常的。

项目数据位于 `/home/ubuntu/ghy/ViMax/.vimax` 和 `/home/ubuntu/ghy/ViMax/.working_dir`，连同本地配置一起备份。常驻服务、Nginx、迁移和故障排查按需查看 [部署附录](deployment-reference.md)。

后续更新使用 `git pull --ff-only`，然后安装依赖、重新构建并重启服务，完整命令见 [更新与回退](deployment-reference.md#10-更新与回退)。

# ViMax 快速部署

适用于 Ubuntu 24.04 单机部署。先安装 **Node.js 24、uv、FFmpeg**，并将当前项目代码上传到 `/opt/vimax/app`。以下服务器命令使用有该目录写权限的普通账户执行；环境安装和上传命令见 [部署附录](deployment-reference.md)。

## 1. 安装依赖并构建

```bash
cd /opt/vimax/app
uv sync --locked --python 3.12 --no-dev
npm --prefix /opt/vimax/app/web ci --include=dev
npm --prefix /opt/vimax/app/web run build
```

## 2. 填写光合云 Key

```bash
test -e /opt/vimax/app/configs/agent.local.yaml || cp /opt/vimax/app/configs/agent.ghyai.example.yaml /opt/vimax/app/configs/agent.local.yaml
chmod 600 /opt/vimax/app/configs/agent.local.yaml
```

编辑 `/opt/vimax/app/configs/agent.local.yaml`，填写 `llm.api_key`。其他模型和接口地址保留示例值；图片、视频的 Key 留空会复用这个 Key。

检查连接和模型权限：

```bash
cd /opt/vimax/app
/opt/vimax/app/.venv/bin/python -m scripts.check_ghyai
```

## 3. 启动生产服务

```bash
cd /opt/vimax/app
VIMAX_WEB_HOST=127.0.0.1 \
VIMAX_PYTHON_CMD=/opt/vimax/app/.venv/bin/python \
FFMPEG_BINARY=/usr/bin/ffmpeg \
IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg \
node /opt/vimax/app/web/server.mjs
```

保持终端运行。需要后台常驻和开机启动时，使用附录中的 systemd 配置。

## 4. 打开网页

在自己的电脑执行，将 `deploy@YOUR_SERVER` 换成服务器账户和地址：

```bash
ssh -N -L 127.0.0.1:14173:127.0.0.1:4173 deploy@YOUR_SERVER
```

浏览器打开 **http://127.0.0.1:14173**，新建项目即可使用。程序没有内置登录，因此这里通过 SSH 隧道访问；域名和 HTTPS 配置见附录。

服务器健康检查：

```bash
curl -fsS http://127.0.0.1:4173/api/health
```

返回 `ok: true` 表示 Web 服务已启动；打开项目之前 `agentRunning: false` 是正常的。

项目数据位于 `/opt/vimax/app/.vimax` 和 `/opt/vimax/app/.working_dir`，连同本地配置一起备份。常驻服务、Nginx、迁移和故障排查按需查看 [部署附录](deployment-reference.md)。

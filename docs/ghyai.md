# 光合云 AI 接入

本项目按仓库根目录的 `openapi-public.json` 接入光合云。服务根地址为 `https://ghy-ai.com`，配置中的 `base_url` 统一填写 `https://ghy-ai.com/v1`，不填写完整的生成接口路径。智能体对话、创意/剧本规划、图片和视频均由专用适配器处理；其他服务商仍走原来的适配器。

## 配置与启动

新环境可以复制示例，然后在本地文件填写 Key；已有配置请直接修改，避免覆盖密钥：

```bash
cp configs/agent.ghyai.example.yaml configs/agent.local.yaml
uv sync --locked
npm --prefix web install
./vimax web
```

打开 `http://localhost:4173`。API Key 只保存在被 Git 忽略的 `configs/agent.local.yaml`；图片和视频的 Key 留空时会使用语言模型的 Key。`VIMAX_LLM_*`、`VIMAX_IMAGE_*`、`VIMAX_VIDEO_*` 环境变量仍然优先于 YAML。修改设置后开始新的对话或生成操作即可使用新配置。

`llm.model_provider` 使用 `openai` 作为协议标识，实际适配器根据 `ghy-ai.com` 主机名识别。无需设置一个 LangChain 不认识的中文服务商名称。

光合云叙事规划的默认输出上限为 **16384 token**，实际请求还会按模型能力中的 `max_output_tokens` 限制。长故事可在启动前设置 `VIMAX_NARRATIVE_MAX_TOKENS` 调整，例如 `VIMAX_NARRATIVE_MAX_TOKENS=24000 ./vimax web`；这是单次输出上限，并非固定消耗。其他服务商仍沿用原默认值 4096。

## 已确认的模型能力

2026-09-20 使用当前测试 Key 查询了 `GET /v1/models` 与 `GET /v1/platform/models/capabilities`，两者均返回 HTTP 200。共开放 11 个模型。下列限制是该次查询结果，运行时也会用当前 Key 查询、校验并在当前适配器实例内缓存能力；不会把文档示例当作模型实际能力。

| 用途 | 当前模型 | 确认的限制 |
| --- | --- | --- |
| 对话与规划 | `qwen3.8-flash` | 文字/图片输入，最多 10 个工具，最多 131072 个输出 token，支持 `text`、`json_object`，未声明 `json_schema` |
| 图片生成/编辑 | `doubao-seedream-5.0-lite` | 提示词最多 4000 字符；输出 PNG/JPEG；编辑最多 14 张参考图、30,000,000 字节；不支持蒙版 |
| 文生/图生视频 | `doubao-seedance-2.0` | 文字或单张首帧图片输入；JPEG/PNG/WebP，图片最大 31,457,280 字节（30 MiB）；提示词最多 4000 字符，最长 15 秒；输出 MP4 |

标准图片 schema 不接受 `2K`、`3K` 字符串，因此适配器只使用能力目录中同时符合协议的 `2048x2048`、`3072x3072`。原流程的 `1600x900`、`512x512` 等目标尺寸会在生成后本地中心裁剪/缩放得到，裁剪可能移除边缘内容。编辑时将参考图转换为同格式 PNG、统一画布尺寸；缩放和留白保留参考内容。超过参考图数量、文件大小或提示词限制时会在提交前报错，不截断提示词或丢弃参考图。

视频支持 `864x496`、`752x560`、`640x640`、`560x752`、`496x864`、`992x432`。默认 4 秒，按目标宽高比选最接近的已声明尺寸，16:9 对应 `864x496`。可通过 `VIMAX_GHYAI_VIDEO_SECONDS`、`VIMAX_GHYAI_VIDEO_SIZE` 指定；超出当前模型能力时不会提交。

更新后的文档和实时能力已开放单张首帧图生视频。传入参考图时，适配器通过 multipart 的 `input_reference` 文件字段上传第一张本地图片，并按实际文件内容确定 MIME；没有参考图时继续使用文生视频。提交前检查图片格式、完整性、单帧和模型大小限制，不接受图片 URL 字符串。

**标准接口只支持首帧，尾帧不参与生成。** ViMax 原流程有时会传入首尾两张图，此时仅上传第一张，并在进度和日志中明确提示尾帧不会参与。设置页也说明这一限制；不会添加文档未声明的尾帧字段。

模型目录没有开放 embedding/reranker 能力，因此小说检索流程仍需要单独配置向量模型和重排服务。

## 请求、错误与任务恢复

- 聊天使用 `/v1/chat/completions` 非流式 JSON。当前原生工具调用实测返回 `response_projection_failed`，默认 `llm.tool_mode: json` 通过已声明的 `response_format: {type: json_object}` 返回工具指令，校验名称和参数后交给原工具执行器；所有工具仍可使用。不会在失败后自动换协议重发。上游修复原生工具后可显式设置 `VIMAX_GHYAI_TOOL_MODE=native`；该模式在工具数量超过模型上限时使用统一分发入口。两种模式都先检查 `finish_reason` 和拒绝信息。
- 图片生成使用 `/v1/images/generations` JSON；编辑使用 `/v1/images/edits` multipart，重复 `image` 字段上传图片。只接收并校验 `data[0].b64_json` 的 PNG 内容。默认不发送可选的 `Idempotency-Key`，因为当前服务在携带该头时返回 503，完全相同的正文去掉该头可成功生成。图片只提交一次，网络中断或服务错误不自动重发。
- 参考图筛选等视觉聊天按整份 JSON 的 UTF-8 编码检查 16 MiB 上限。超限时按剩余空间压缩内嵌图片副本，生成保持宽高比、最长边不超过 1536 像素的 JPEG；必要时进一步降低质量和尺寸。保留图片顺序、数量和全部文本，不修改磁盘原图，也不影响后续图片/视频生成使用的素材。正常体积的请求直接发送；文本过大、图片无效或无法在保留全部内容的前提下压缩时仍在本地报错。
- 视频使用 `/v1/videos` multipart，图生视频额外上传一个 `input_reference` 文件。创建前保存幂等键，拿到标准 `id` 后立即落盘；只识别 `queued/in_progress/completed/failed`。完成后携带同一 Key 下载 `/v1/videos/{id}/content`，不混用平台 `task_no` 或大写任务状态。
- 标准错误读取 `error.message/code/param`，平台能力查询读取 `code/success/msg/tip/data`；报错保留 `X-Request-ID`，隐藏密钥，不输出完整请求或响应正文。
- JSON 工具回复兼容单个完整对象被一层数组包裹的情况，解包后仍校验 `content`、`tool_calls`、工具名称和参数对象；空数组、多项数组、嵌套数组和不完整对象仍拒绝。提示词明确要求最外层为对象。HTTP 成功后的对话解析错误显示“响应校验失败”和实际 HTTP 状态，不再误标为提交前的“本地校验”，也不自动重新请求。
- `finish_reason=length` 表示输出被截断，单独报告 `output_length_exceeded`，显示本次实际输出上限及服务返回的 token 用量。不会解析或保存不完整的脚本，也不会自动加额度重发。缩短内容或提高 `VIMAX_NARRATIVE_MAX_TOKENS` 后，可继续当前项目，复用已保存的故事、角色和场景文件。
- 仅 GET 和文档允许的、带稳定 `Idempotency-Key` 的图片/视频请求最多尝试 3 次。额度不足、认证失败、参数错误和幂等冲突不重试。临时错误遵守 `Retry-After`；等待超过单次 60 秒预算时直接报告错误，不提前重试。光合云聊天、上层规划链不会因网络、供应商或输出解析错误自动重复提交付费请求。
- 图片默认不启用上述幂等重试；调用方显式传入 `idempotency_key` 才会启用。工具返回 `retryable: false` 的失败时，智能体立即结束当前轮次，连同一批中尚未执行的工具一起停止，避免模型自动发起第二次渲染。用户新的操作仍可再次执行。
- 默认视频查询总超时 600 秒、查询间隔 5 秒、单请求超时 60 秒。分别可用 `VIMAX_VIDEO_QUERY_TIMEOUT_SECONDS`、`VIMAX_VIDEO_POLL_INTERVAL_SECONDS`、`VIMAX_VIDEO_REQUEST_TIMEOUT_SECONDS` 设置。中断本地查询不等于取消远端任务。

每个镜头/转场视频旁保存 `.ghyai.json` 任务记录，记录幂等键、视频 ID 和请求指纹，不包含密钥。图生视频指纹包含实际上传首帧的 SHA-256 和 MIME；单次请求重试使用相同的图片字节快照。重试同一镜头时复用任务；参数、首帧内容或 Key 发生变化时要求先处理旧记录，避免把旧任务误当成新图的结果。无参考图时保留原指纹格式，仍能恢复升级前的文生视频任务。不要为了解决超时直接删除记录、反复新建任务。

## 检查命令

只查询当前 Key 的模型及能力，不执行生成：

```bash
uv run python -m scripts.check_ghyai
```

以下测试会产生 API 用量，按需要单独执行：

```bash
uv run python -m scripts.check_ghyai --chat
uv run python -m scripts.check_ghyai --image
uv run python -m scripts.check_ghyai --video
uv run python -m scripts.check_ghyai --video --video-reference /absolute/path/first-frame.png
```

`--chat` 检查完整工具目录和规划模型；`--image` 生成一张图片并编辑一次；`--video` 创建或恢复一个 4 秒视频。搭配 `--video-reference` 验证首帧图生视频，单独保存为 `image-video.mp4` 和 `image-video.ghyai.json`，不覆盖文生视频记录。默认产物及任务记录在 `.working_dir/ghyai-check/`，也可指定 `--output-dir`。

已取得视频 ID 时，可以仅恢复查询和下载：

```bash
uv run python -m scripts.check_ghyai --resume-video video_0123456789abcdef0123456789abcdef
```

此处 ID 为格式示例，请替换成实际任务 ID，并保留创建时的 Key。

## 本次联调结果（2026-09-20）

用户在本轮修复后已确认完整跑通一轮生成。提交前回归通过 248 项 Python 测试、13 项子测试、32 项 Web 测试及 Web 生产构建。下文保留各阶段的联调和错误排查记录，其中早期服务错误不代表当前状态。

普通聊天、带输出长度限制的聊天、JSON 模式的完整工具目录选择，以及 LangChain 规划模型调用均已真实通过。另在隔离的临时工作区跑通了完整智能体循环：选择并执行 `todo_read`，读取结果后向用户回复，未产生错误。原生单工具调用返回 `HTTP 500 / response_projection_failed`，因此默认采用上述 JSON 工具模式。

长脚本回归中，复用约 4700 字的已有故事，仅将输出上限从 4096 提高到 16384，服务返回 `finish_reason=stop`，报告使用 12608 个输出 token，成功解析并保存 8 个场景脚本。由此确认旧默认额度会截断这类规划输出；新默认额度已覆盖该实际案例。

图片 503 的进一步对照已经定位到可选请求头：同一 Key、`doubao-seedream-5.0-lite`、同一 JSON 正文（`2048x2048`、`n: 1`、PNG、`b64_json`），不带 `Idempotency-Key` 时用时约 17 秒返回 HTTP 200，已解码验证 2048×2048 PNG；加上该头约 0.2 秒返回 `503 service_unavailable`。对应请求 ID 分别为 `req_2a272943c9c54416a830e3a285e3285e` 与 `req_86ef4d8afda64d178238d96e1bafb736`。这不是模型或 Key 整体不可用，具体服务端幂等处理异常仍需结合服务端日志定位。

课程项目采用的 `/v1/platform/images/generations` 异步协议也已使用同一 Key 验证成功，任务 `7507425353139544064` 完成并下载了 PNG。它与 ViMax 的同步协议不同，不能直接混用请求字段或响应解析。当前修复保留标准同步接口及本地参考图上传能力，只调整默认幂等头和自动重试边界。

本轮经过脱敏的请求、响应及生成图片保存在 `.working_dir/ghyai-request-diagnosis/`。不要只凭调用/用量列表没有记录就判断 HTTP 请求未到达服务；已验证 HTTP 响应关联 ID 与历史业务调用记录 ID 并不总是相同，失败发生在调用记录创建前时也可能没有对应业务行。

修复后通过实际 `_build_image_generator()` 调用完成文生图和本地参考图编辑，两张产物均已解码验证为 1600×900 PNG，保存在上述目录的 `adapter-check/`。全部 Python 回归通过 234 项测试、13 项子测试；本次图片验证不代表整条视频渲染流程已经通过。

随后在实际渲染中发现第一帧参考图筛选超限：黑猫与霓虹灯各有正面、侧面、背面，共 6 张 2048×2048 PNG，原始文件合计 19,580,213 字节，仅 Base64 就有 24.90 MiB。调用链为 `ReferenceImageSelector → GhyAIChatModel → /v1/chat/completions`，在本地大小校验处失败，尚未发出该次聊天请求；此前的 9 张角色图已经成功生成。

加入视觉请求压缩后，使用完全相同的 6 张原图和第一帧描述完成真实筛选，完整请求为 1,170,535 字节（约 1.12 MiB），模型选出黑猫和霓虹灯的正面参考图，请求 ID 为 `req_f8186868413841648a5764a21696a233`。离线复现同时核对全部原图 SHA-256 不变；全部 Python 回归通过 239 项测试、13 项子测试。验证记录为 `.working_dir/ghyai-request-diagnosis/vision-size-check.json` 和 `vision-selection-live.json`；本次没有运行完整视频渲染。

22:09:12 的智能体错误包含响应头 `X-Request-ID=req_5a5a0951bd884a66ba4c992f7289711f`。按时间、模型、会话 ID 和完整用户请求，在光合云调用日志匹配到业务请求 `7507440712009945088`（`qwen3.8-flash`，`SUCCESS`，输入 4153 / 输出 147 token）。日志中的实际回复为 `[{"content":"正在重新进入渲染阶段，生成最终视频。","tool_calls":[{"name":"vimax_render_video","arguments":{"session_id":"20260920-185927-vimax","force":true}}]}]`：完整工具对象被数组包裹，旧解析器只接受对象，因此在执行任何工具前失败。

接口响应头的 `req_...` 和该聊天调用日志的数字业务 ID 由服务不同层分别生成，不能直接用前者查询后者；该条记录可通过 [Trace 页面](https://ghy-ai.com/admin/system/traces?trace_id=7507440712009945088) 查看。后台 `SUCCESS` 表示模型调用成功，不代表 ViMax 后续解析或视频渲染成功。

修复后增加 9 项正常/无效回复的回归案例，全部 Python 测试通过 248 项、13 项子测试。22:26 使用同一项目、完整工具目录和相同用户指令真实验证，返回 HTTP 200、`finish_reason=stop`，成功解析 `vimax_render_video` 调用；本次模型按要求返回顶层对象，请求头 ID 为 `req_c9fcf52426c743219a810ea61c9ef4ff`。记录保存在 `.working_dir/ghyai-request-diagnosis/json-tool-envelope-live.json`；只验证智能体选择和解析，没有执行渲染工具。

首次接入时图片和视频适配器已通过本地协议回归测试，但未获得真实媒体产物，记录到以下服务端错误（保留历史排查信息，不代表服务更新后的状态）：

| 请求 | 返回错误 | 请求关联 ID |
| --- | --- | --- |
| 最小原生工具聊天 | `500 response_projection_failed` | `req_59917d46c39e49ec8aaf3ddfb233530b` |
| 图片生成 | `503 service_unavailable` | `req_e7cf0ec371e94ffa9b41993b3d9bbfeb` |
| 标准视频创建 | `500 internal_error` | `req_d2f7597b8054421792f61b6b40a4ae24` |

视频创建未返回任务 ID，本地保留了原幂等键。服务恢复后使用同一输出目录运行 `--video`，会复用该键，不另建一份不确定的请求。

更新文档后的图生视频联调已重新查询并确认模型开放 `image` 输入。使用一张 864×496、3202 字节的 PNG 测试首帧，提交 4 秒、`864x496` 的标准 multipart 请求后，创建接口仍返回 `HTTP 500 / internal_error`，请求关联 ID 为 `req_0df1a0cf34cb4508b001622faac02f9c`；未返回视频 ID，因此还没有真实生成成功的产物。上传格式、首帧大小校验、尾帧提示、幂等重试、换图后的任务指纹检测及轮询下载均已通过离线测试。

本次图生测试的首帧和幂等记录保存在 `.working_dir/ghyai-image-video-check/`。服务端修复后，用同一首帧和目录恢复请求：

```bash
uv run python -m scripts.check_ghyai --video \
  --video-reference .working_dir/ghyai-image-video-check/first-frame.png \
  --output-dir .working_dir/ghyai-image-video-check
```

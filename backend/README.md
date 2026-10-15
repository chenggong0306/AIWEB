# AIWEB 本机版

现有网页 → Python 接口 → 官方 DSH SDK → DeepSeek 官方模型 → Unity CLI / Editor → Unity Web 构建 → 原网页预览。

Python 只处理网页接口、任务排队、记录和文件。规划、调用模型与工具都由 DSH 完成，没有另写智能体框架，也没有改 DSH 源码。

## 启动

首次拉取请先阅读仓库根目录的 [安装说明](../README.md)，准备 uv、PowerShell 7 和 Unity 环境。然后在仓库根目录打开终端：

```powershell
uv sync --locked --no-dev --project backend
uv run --locked --no-dev --project backend python backend/run.py
```

打开 <http://127.0.0.1:8000/>。不要双击 HTML，也不用 npm、npx 或另外运行 `dsh web`。

端口被占用时：

```powershell
uv run --locked --no-dev --project backend python backend/run.py --port 8001
```

停止服务按 `Ctrl+C`。

## 第一次使用

1. 在网页点击“模型与环境设置”，填写 DeepSeek 官方 API Key。也能继承启动服务时的 `DEEPSEEK_API_KEY` 环境变量。
2. 默认模型沿用已安装 SDK 的 `deepseek-v4-flash`，可填写官方账户实际可用的模型名。保存配置不代表已经向模型验证成功。
3. 查看同一弹窗中的 DSH、Unity、许可证、Web 模块和技能状态。
4. 确认 Unity Hub 中的许可证已激活。每位开发者需要在自己的电脑上安装 Editor 和 Web 模块，并激活自己的许可证；这些不随 Python 依赖分发。项目曾使用 Editor 6000.5.6f1 完成真实 Web 构建。
5. 描述一个小型游戏，例如“做一个第三人称收集金币游戏，先用简单几何体做场景”。DSH 按需读取 Unity Skill，通过 CLI 操作工程。
6. 只有发现完整 Unity Web 导出，网页才会加载游戏。对话完成不等于游戏构建成功。

本机初版仅适配单线程 Unity Web，要求关闭构建压缩或使用 Unity 解压回退。游戏运行在沙箱中，浏览器持久存储、多线程及原生移动 App 打包不在本轮验证范围。APP 标签是手机尺寸预览，不是 Android/iOS 安装包。

## 文件怎么放

| 位置 | 用途 |
| --- | --- |
| 根目录 `ai.html / ai.css / ai.js / client-api.js` | 现有界面与后台连接 |
| `backend/app/main.py` | 网页 API、文件查看、构建静态服务 |
| `backend/app/dsh_service.py` | 单任务队列、DSH 官方 SDK 适配、步骤事件 |
| `backend/app/database.py` | SQLite 记录 |
| `backend/app/unity.py` | Unity 环境检测及 Web 构建检查 |
| `backend/app/skill_setup.py` | 将打包的 Unity Skills 安装给 DSH |
| `backend/resources/skills/` | 8 套 Unity Skills 原文与引用，随项目交付 |
| `backend/.venv/` | uv 安装的 Python 与 DSH 依赖 |
| `backend/.dsh/` | DSH 配置、技能、原生会话记录 |
| `backend/data/aiweb.sqlite3` | 作品、网页聊天记录、任务与构建索引 |
| `backend/data/settings.json` | 本机模型设置；Windows 使用当前账户 DPAPI 加密密钥 |
| `backend/data/projects/<id>/workspace/` | 这个作品的 Unity 工程、素材和构建输出 |
| `backend/data/projects/<id>/builds/<build-id>/` | 校验后保存的不可覆盖预览版本 |

不要提交 `.venv`、`.dsh` 或 `data` 给其他开发者；根目录 `.gitignore` 已排除。交付源码、`pyproject.toml`、`uv.lock` 和 `resources/skills`，对方运行 `uv sync --project backend` 即可安装 Python/DSH 依赖。Unity Editor、Web 模块和许可证仍需各自安装和配置。

## 当前行为与边界

- 任务按顺序执行，避免两个智能体同时操作 Unity；同一作品执行期间不能重复提交。
- 页面展示 DSH 完整消息与真实工具步骤，不伪造逐字输出和制作进度。
- `sdk-minimal` 加官方 `skill` 插件补丁；诊断日志上传插件在 AIWEB 实例内禁用。模型请求仍按你的指令发送到 DeepSeek 官方 API。
- 同一服务进程内保留各作品 DSH 实例，多轮沿用同一个会话。已安装的 DSH 0.1.5rc1 没有跨进程会话恢复接口；重启或取消后保留源码、网页记录和 DSH 日志，下次创建新会话并附最近 10 条对话的有限摘录，不声称恢复完整模型上下文。
- 单个任务最长 30 分钟，单次 DSH shell 命令最多 20 分钟，以容纳首次 Unity Web 编译；停止任务通过关闭它持有的 DSH runtime。已发生的源码修改不会自动回滚。
- 完整导出通过结构、资源和 WASM 检查后才保存版本；仍需实际打开试玩验证玩法。版本菜单切换构建预览，不回滚工程源码。
- “发布”生成本机服务里的可访问链接，没有自动上传公网。外网部署、账号、多用户权限和每用户执行容器需要后续设计。
- 删除/清空只移除网页作品和聊天索引，本地工程文件及 DSH 日志保留，避免误删制作成果。
- 游戏文档用 CSP 与 iframe 沙箱隔离控制 API，且 API 拒绝跨站请求。DSH 执行工具仍是当前 Windows 用户权限，**不是操作系统级沙箱**，本版只供本机可信用户使用。
- 附件最多 10 个、总计 25 MB；代码查看限 2 MB 的文本文件，禁止越出当前作品目录。

## 验证

```powershell
cd backend
uv sync --locked
uv run --locked pytest -q
uv run --locked python tests/runtime_probe.py --patch .dsh/aiweb.patch.yml --patch .dsh/unity-skills.patch.yml --expect-skill unity-cli
```

`runtime_probe.py` 使用本地模拟 API，真实启动 DSH、加载插件、运行 PowerShell 并检查多轮上下文与技能正文保留，不调用付费模型。这组 fixture 验证不等同实际游戏玩法验证。

本次自动测试为 **22 passed、1 skipped，另有 4 项子测试通过**。已通过 DeepSeek 官方模型实际制作收集金币游戏；当前修复版为 `0bef526b223e431d9755dcf686e369f5`，Unity Web 构建成功（0 errors、16 warnings，用时 3 分 28.391 秒）。

实际浏览器已验证原网页 APP 的 HUD 与 Restart、PC 按键收集 **8/8** 金币和 **You Win**，以及通关后按 R 恢复 **0/8** 并闲置 4 秒不穿地。本地发布页在 390×844 视口下验证了屏幕方向键收集金币、Restart、持续向上及向右各 10 秒的边界阻挡和再次重开，上述操作未再复现第一版的穿地问题。通关截图见 [game-win.jpg](../docs/images/game-win.jpg)。

浏览器检查未见 error，但仍有 FSR shader unsupported 警告；左右方向箭头字形未显示，按钮区域存在且右方向移动已验证。真实手机、跨设备性能及长时间稳定性尚未验证，当前结论限于本地浏览器 MVP。

上述作品 ID 是历史验证记录。生成的游戏和本机发布链接没有随源码分发，新拉取者需要自己生成作品；公网部署和原生 Android / iOS App 尚未完成。详细记录见 [VALIDATION.md](VALIDATION.md)。

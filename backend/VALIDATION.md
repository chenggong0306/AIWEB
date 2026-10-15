# 本次验证记录（2026-10-15）

这是开发机上的历史实测记录，不会在新拉取后自动复现。文中的作品 ID、构建 ID 和本机试玩地址只属于原开发环境；数据库、Unity 工程与构建不随源码发布。新用户请按根目录 [README](../README.md) 安装并生成自己的作品。

## 实际网页与官方模型

在现有 AIWEB 网页发送只读测试，使用启动环境里已有的 DeepSeek API Key，实际通过以下链路：

`AIWEB 网页 → Python API → DSH SDK → DeepSeek 官方 API → skill(unity-cli) → pwsh(unity --version) → 网页回复`

- DSH SDK：0.1.5rc1，sdk-minimal 加官方技能插件。
- 实际技能加载成功，CLI 返回 `1.0.0-beta.6`。
- 第二轮不调用工具询问上一轮版本，正确返回 `1.0.0-beta.6`，验证同一 DSH 会话连续对话。
- 刷新网页后，聊天记录从 SQLite 重新加载，安全 Markdown 正常显示。
- 以上是前期只读验证；本次已继续完成下述真实游戏制作与 Web 构建。

## 首个真实 Unity 游戏构建

许可证已激活。使用 Unity Editor `6000.5.6f1` 和已安装的 Web 模块，通过现有 AIWEB、DSH 与官方模型制作了收集金币测试游戏，并实际导出 Unity Web 构建。

- 作品 ID：`7c1eabbdd1cd4b6c8ae08d85813203a7`。
- 构建 ID：`982ada68693f4f368a55abd8412acf16`。
- 构建日志：作品工作区内的 `CoinCollectorTest/Logs/build-webgl-2.log`。
- Unity 构建结果：**Succeeded，0 errors，15 warnings，用时 7 分 54 秒**。
- 原网页 APP / PC 预览已做基础显示检查；独立游戏页面已做真实按键试玩。
- 首屏短时检查的浏览器 error / warn 均为空；后续试玩检测到了 FSR shader warning，不能据首屏检查声称全程无警告。
- 导出使用单线程、无压缩、关闭 dataCaching 的配置，以及随 iframe 尺寸调整的网页模板。

这些结果确认了实际模型制作、Unity 编译、Web 导出、保存预览版本和原网页加载链路。第一版的真实玩法检查结果如下：

- 键盘移动、屏幕方向键和金币计数已实际验证。
- 已真实收集 **8/8** 枚金币，画面显示 **You Win**，控制台出现两条对应的 You Win 日志。
- 按 R 后计数恢复为 **0/8**。
- 通关后按 R 重开，再不按键等待，确实出现角色穿过地面的问题；因此**第一版未通过重开稳定性验证**，修复版的复测结果见下。
- 通关截图：`data/validation/game-win-first-build.jpg`。

## 修复版构建与真实浏览器复测

- 构建 ID：`0bef526b223e431d9755dcf686e369f5`。
- 增量构建日志：`CoinCollectorTest/Logs/build-webgl-3.log`。
- 已核对日志 Result 行：**Succeeded，0 errors，16 warnings，用时 3 分 28.391 秒**。
- 对应 DSH 任务：`55dca6d4d2e943b8bec52077f8d0a3d6`；新构建已由 AIWEB 保存为预览版本。
- 原网页 APP 预览实际显示 HUD、Restart 和简短提示；顶部留白使 HUD 不再被手机模型刘海遮挡。
- 原网页 PC 预览通过真实按键收集 **8/8** 枚金币，显示 **You Win**，控制台出现两条对应的胜利日志。
- 通关后按 R 重开，计数恢复 **0/8**；不按键等待 **4 秒**，角色仍站在地面上。
- 修复版通关截图：[game-win.jpg](../docs/images/game-win.jpg)。

从原网页发布按钮生成的本地链接已打开并真实运行：

[打开本机发布游戏](http://127.0.0.1:8000/play/0bef526b223e431d9755dcf686e369f5/index.html)

发布页以 **390×844** 浏览器视口检查，结果如下：

- 按屏幕向上按钮实际收集 **1/8** 枚金币；点击 Restart 恢复 **0/8**，闲置时保持站稳。
- 持续向上 **10 秒**、向右 **10 秒**，角色被东北边界挡住，没有越界坠落；控制台未出现 below-field fallback 警告。
- 再次点击 Restart 成功。

以上范围内没有再复现第一版的重开后穿地问题，浏览器检查未见 error。修复版仍观察到两条 FSR shader unsupported 警告，不能声称已消除全部警告或覆盖所有游戏状态。

## 自动检查

- `uv run pytest -q`：**22 passed, 1 skipped，另有 4 项子测试通过**。
- 跳过项是 Windows 当前账户无法创建符号链接的测试；其余路径限制、密钥脱敏、加密配置、上传、排队、取消、事件、发布隔离和构建检查通过。
- 测试依赖 Starlette 对 httpx 的适配有一条弃用提示，不影响本次通过。
- `runtime_probe.py --patch .dsh/unity-skills.patch.yml --expect-skill unity-cli`：实际启动 DSH、调用 skill 并验证返回正文、执行 pwsh、多轮保留正文均通过。
- 该 probe 使用 localhost 模拟 API；去掉技能补丁的负例正确失败。
- 本地模拟 API 下额外测试执行中的停止：pwsh 开始 15 秒等待后关闭 DSH，约 1.1 秒退出；17 秒后仍未产生后续写文件，原 pwsh 进程已结束。该结果仅覆盖 DSH 直接执行的工具，不涵盖另起的独立 Editor 或服务。
- 前端 JS 语法检查通过；HTML/script/img onerror/javascript 链接输入保持惰性文本。
- 浏览器检查 390px 手机和 1365px 桌面布局；未发现横向溢出，原有面板和预览标签保留。

## 已通过范围与剩余限制

本次已跑通本地浏览器 MVP：现有网页发送需求、DSH 调用官方模型与 Unity Skills 制作游戏、Unity Web 构建、APP / PC 预览、通关、重开和本地发布。结论仅覆盖上述实际操作与等待时长。

- FSR shader unsupported 警告仍存在，渲染兼容性需后续检查。
- 屏幕方向键的左右箭头字形未显示；两个按钮区域存在，右方向移动已实际验证。这是原型 UI 的待完善项。
- 手机尺寸测试使用桌面浏览器视口，尚未验证真实手机、其他设备与浏览器的性能或长时间稳定性。

“发布”当前仅在本机服务生成链接；未部署到公网，也未打包原生手机 App。

# 微信 / QQ 聊天与公众号文章导出

Windows 本地导出工具：输入好友或群聊名称、起止时间，导出 TXT、Markdown、JSON，方便本地留存和交给 GPT、Claude、Gemini 阅读。当前版本：**v1.3.5-local.9**。

[下载 Windows 版](https://github.com/xunguangzlj-cloud/wechat-qq-export/releases) · [自动构建](https://github.com/xunguangzlj-cloud/wechat-qq-export/actions) · [使用说明](docs/使用说明.md)

## 已实现的功能

- 微信私聊、群聊导出与聊天预览；图片、文件、语音、视频以本机可取得的缓存为准。
- 可选本地语音转文字；首次需下载约 230 MB 的 SenseVoice 模型，下载包不含模型。
- 默认最近 7 天；日期下拉、快捷范围、手动输入时间、右键粘贴。
- 批量会话、增量导出；同名会话需要选择准确身份。
- QQ 通过本机 NapCat OneBot HTTP 接口读取历史和附件。
- 公众号文章链接批量采集、浏览器页面采集、HTML/MHTML 导入、微信本地消息链接提取、WeRSS 已采集历史读取。
- TXT / Markdown / JSON 按**文章标题或好友 / 群聊名**命名，独立身份目录防止同名覆盖；公众号正文保留段落、代码空白与原位图片标记。

| 入口 | 运行条件 | 覆盖范围 |
| --- | --- | --- |
| 微信聊天 | Windows 微信已登录并运行；底层固定 wechatauto-replica 1.2.4.4 | 本机已有数据库和可读取的媒体缓存 |
| QQ 聊天 | 用户另外安装、登录 QQ 并配置本机 NapCat HTTP 接口 | 客户端接口实际返回的历史与附件 |
| 公众号文章 | 可访问的文章链接；浏览器入口需要已安装 Chrome / Edge | 已加载正文和可下载图片 |
| 公众号名称 | 微信本地会话，或用户另外部署并完成采集的 WeRSS | 本地消息中的文章链接，或服务已保存的文章 |

## 开始使用

1. 从 Releases 下载 Windows x64 ZIP，解压后运行 `WeChat-Chat-Export-for-LLM.exe`。不需要安装 Python。
2. 保持微信登录，选择平台，输入名称和时间。导出目录默认是程序旁的 `exports`。
3. 选择需要的媒体，点击开始导出；批量入口可以处理多个会话，增量模式在原输出目录追加新记录。
4. 公众号文章入口默认使用浏览器页面。每行粘贴一个文章链接，旧文章先选“全部时间”；若页面要求验证，用户手动完成后继续。

程序没有商业代码签名。源码、依赖版本和构建脚本公开，可按发布页 SHA-256 校验下载包。

## 输出与时间规则

时间均按北京时间 UTC+8 解释。开始时间包含；结束日期包含整日，带时分秒的结束值不包含该时刻。两端留空表示全部本地记录。公众号按**文章发布时间**筛选；限定时间时，发布时间未知会记录原因并跳过。

```text
exports/
├─ 会话身份目录/
│  ├─ 好友或群聊名.txt
│  ├─ 好友或群聊名.md
│  ├─ 好友或群聊名.json
│  └─ media/
└─ mp_articles/
   ├─ 文章身份目录/
   │  ├─ 文章标题.txt
   │  ├─ 文章标题.md
   │  ├─ 文章标题.json
   │  └─ images/
   ├─ articles_index.json
   └─ reports/
```

文件名自动处理 Windows 非法字符、保留名和过长名称；JSON 保留完整名称。导出默认留在本机；用户自行选择是否将内容发送给大模型。公众号采集、首次模型安装及客户端获取媒体会联网。

## 源码运行与构建

需要 Windows x64、Python 3.12（含 Tcl/Tk）。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build-lock.txt
python app.py
python cli.py "测试群" --start "2026-10-01" --end "2026-10-06" --media
python cli.py --install-rust-silk
```

微信 CLI 的 SILK 解码器可通过最后一条命令从固定上游版本下载；Windows 成品已包含。QQ、公众号、批量和增量通过 GUI 使用。

```powershell
python -m unittest discover -s tests -p "test_*.py"
pwsh -File scripts/build_windows.ps1
```

部分浏览器解析测试需要 Node.js。GitHub Actions 使用 Python 3.12 和 Node.js 22，检查固定底层 API、测试、构建并验证 Tk 组件，输出 ZIP 和校验文件。依赖版本集中在 `requirements-build-lock.txt`。

## 验证范围与已知限制

本版本本机完整测试 **238 项通过，0 失败、0 跳过**，另通过合成 GUI、浏览器协议联调和 Windows EXE 启动检查。已对可访问公众号文章校对正文和图片位置。真实账号聊天解密、QQ 登录与历史完整性没有在该测试中验收；这些能力受客户端版本、本机缓存和接口状态影响。

公众号名称无法单独获取全部后台历史。WeRSS 接入仅读取已有订阅和文章，不代替登录、添加订阅或刷新采集。浏览器验证由用户完成；删除文章、不可访问正文或缺失附件会记录原因。公众号图片使用公开资源地址，不继承浏览器验证凭据。当前不采集文章视频或评论。

微信增量扫描选定窗口的本地消息，QQ 增量受历史分页和检查点限制；后来补回的早期 QQ 历史需扩大范围或关闭增量重导。文章修改后如需更新已保存正文，取消增量重新采集。百万条消息的低内存性能尚未验证。

## 来源与许可证

基于 [zhuzhangxue/wechat-chat-export](https://github.com/zhuzhangxue/wechat-chat-export) 的 v1.3.5 派生，基线提交 `f438105d5043e899002ca04c6f5d74e4d36c9e8c`。新增 QQ、公众号、批量增量、日期交互及命名等功能，并修复文章正文解析。具体改动见 [NOTICE](NOTICE)，相关项目见 [参考项目](docs/参考项目.md)。

保留上游 [Apache-2.0 许可证](LICENSE) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。QQ / WeRSS 服务需要分别部署，不打包其运行组件。本仓库不包含聊天数据、账号密钥、真实文章导出或本机安装记录。

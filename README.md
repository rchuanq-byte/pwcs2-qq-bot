# 牛牛精灵 · PWCS2 QQ 群机器人

牛牛精灵是一个面向 QQ 群的 CS2 助手。当前使用 AstrBot 和 NapCat/OneBot，保留旧版 nanobot 适配器。主要功能包括：

- QQ 与 SteamID64 绑定、完美平台和官匹战绩查询、战绩图片、排名、对局播报和每日战报。
- 观战、Steam 游戏状态、开黑组队、TeamSpeak 状态、牛币商店和群内答题。
- 可选的 AI 群聊、多模态、记忆和只读游戏数据工具；B 站链接读取区分公开资料和字幕，不用标题猜测视频发言。
- 可选的 CS2 Demo 高光筛选与 Windows 按需录制、主播开播通知、群主页和网页训练场。

这是可独立配置的源码快照。真实群号、成员绑定、聊天记录、运行配置、凭据、原群专属记忆和个人素材不在仓库内。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `#帮助` | 查看当前部署启用的指令 |
| `#绑定 <SteamID64>` | 绑定自己的 Steam 账号 |
| `#数据` / `#数据 gp` | 查询完美平台 / 官匹赛季数据 |
| `#战绩` / `#战绩 gp` | 查询完美平台 / 官匹最近比赛 |
| `#战绩 @群友` | 查询已绑定成员 |
| `#游戏状态` | 查看群友公开的 Steam 游戏状态 |
| `#ai <问题>` | 向 AI 提问（需部署者启用） |

## 在自己的服务器上部署

需要 Docker Engine 和 Docker Compose。建议先用测试群验收；不要将 NapCat、OneBot 或 AstrBot 的管理端口直接暴露到公网。

1. 克隆此仓库，创建本地配置：

   ```bash
   cp .env.example .env
   cp config/config.example.json config/config.json
   chmod 600 .env config/config.json
   ```

2. 在本机 `.env` 填写机器人 QQ、随机生成的 OneBot 令牌和所需服务凭据。在 `config/config.json` 的 `channels.pwcs2.allowedGroups` 填入自己的群号字符串；只聊天的群用 `aiChatOnlyGroups`，仅被 @ 才回答的群可列入 `aiMentionOnlyGroups`。默认不绑定任何真实群。
3. 用以下命令构建提供渲染依赖的基础镜像，再构建和启动 AstrBot：

   ```bash
   docker build --target bot-runtime -t pwcs2-qq-bot:local .
   docker compose build data-init bot
   docker compose up -d napcat data-init bot
   ```

4. 用 SSH 转发访问本机绑定的 NapCat WebUI（默认 `127.0.0.1:6099`），在官方 QQ 中完成登录。在 NapCat 配置反向 WebSocket `ws://bot:6199/ws`，令牌须与 `.env` 中的 `NAPCAT_ONEBOT_TOKEN` 一致。通过 `docker compose ps` 和 `docker compose logs --tail=80 bot` 检查。

完美、Steam 和 AI 的真实凭据只填入本机 `.env`。AI 的模型和 API 地址应当与你自己的服务匹配。可选功能（例如管理审批、称号、录制、群主页）按需开启，不要把线上数据库、AI 记忆或配置上传。管理审批需要在本机配置 `NIUNIU_OPERATOR_QQ`；未配置时没有默认维护者。

梗图不随源码分发。要启用 `xh` / `xhnb` 图片回复，可把有权使用的 PNG 放在服务器私有目录，挂载至容器，并在 `.env` 设置 `NIUNIU_XH_REACTION_IMAGE` 为容器内路径，在本地配置打开 `xhReactionEnabled`。

部署说明与辅助脚本见 [deploy](deploy/)，Windows 录制见 [本地录制说明](deploy/windows-highlight-worker/README.md)，公开视频链接读取策略见 [链接读取说明](deploy/ASTRBOT-LINK-EVIDENCE.md)。各部署环境的代理、群设置和服务密钥需要单独配置。

## 开发与测试

```bash
python -m pip install -e '.[dev,legacy]' pillow tzdata
python -m pytest -q
```

源代码见 [src/pwcs2_bot](src/pwcs2_bot/)，AstrBot 适配插件见 [astrbot_plugins](astrbot_plugins/)。第三方代码及许可证在 [vendor/dafeiyu](vendor/dafeiyu/)。


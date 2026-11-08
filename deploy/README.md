# 部署检查顺序

```bash
cp .env.example .env
# 编辑 .env 和 config/config.json
chmod +x deploy/*.sh
./deploy/preflight.sh
docker compose up -d napcat
```

进入 NapCat WebUI 完成 QQ 登录并启用 `0.0.0.0:3001` 的 OneBot 11 WebSocket
服务器后，再执行：

```bash
docker compose up -d bot
docker compose ps
docker compose logs --tail=100 bot
```

查看状态使用 `./deploy/status.sh`；更新版本使用 `./deploy/update.sh`，更新前使用 `./deploy/backup.sh`。
确定性命令烟测使用 `./deploy/check-commands.sh`。
HTML/CSS 赛季数据图片烟测使用 `./deploy/check-stats-card.sh`；完美 PVP 近十场战绩使用
`./deploy/check-match-history.sh <SteamID64>`，官匹近十场使用
`./deploy/check-match-history.sh <SteamID64> gp`。群内手动命令中，`#数据`、`#战绩`、
`#排行` 和 `#羁绊` 默认查询完美，增加独立参数 `gp` 才查询官匹；`#观战` 与
`#谁在打` 自动探测完美和官匹，旧 `gp` 写法仅作兼容且不再指定来源；`#游戏状态`
不按比赛来源拆分，后台比赛推送仍分别轮询完美和官匹。
完美排行榜烟测使用 `./deploy/check-ranking.sh [群号]`，官匹排行榜使用
`./deploy/check-ranking.sh gp`（或指定群号：`./deploy/check-ranking.sh <群号> gp`）。
未绑定成员禁言权限使用 `./deploy/check-unbound-guard.sh [群号]` 只读验证。
动作级 OneBot 自检使用 `./deploy/check-onebot.sh`。
配置完美 token 后，可用 `./deploy/check-perfectworld.sh <SteamID64>` 直接验证完美接口。
需要通过短信验证码安全获取并写入完美 token 时，使用 `python3 deploy/configure_perfectworld.py`。
`#游戏状态` 可选用 Steam 用户 Web API Key：先运行 `python3 deploy/configure_steam.py` 隐藏输入并
验证，再重建 bot，最后用 `./deploy/check-steam-presence.sh` 检查实时状态接口。如果阿里云直连
不稳定且已有可信香港节点订阅，可运行 `python3 deploy/configure_steam_proxy.py` 隐藏输入
订阅 URL，或先上传完整 YAML 后运行
`python3 deploy/configure_steam_proxy.py --config-file ~/steam-hk.yaml` 私密导入节点。随后运行
`python3 deploy/install_mihomo.py` 下载并校验官方程序，构建和启动 `steam-mihomo` 后用
`./deploy/check-steam-proxy.sh` 连续检查 10 次；只有 Steam 状态请求会走代理。
竞猜功能当前已关闭，自动竞猜后台不会启动。
旧版 `users(user_id, steam_id)` SQLite 绑定可用 `deploy/import_legacy_bindings.py` 预览并迁移。
完整上线验收步骤见 `deploy/ACCEPTANCE.md`。

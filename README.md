# 米家设备管家 (mihome-admin)

fnOS 飞牛 OS 的 FPK 套件：Web 端小米/米家设备管理面板。

登录小米账号获取在线设备，按栏目自由布置一键动作卡片；支持云端 MIoT 属性/动作、旧版 RPC 与局域网 miIO 直连四种执行方式；每个动作可独立开启**二次确认**与**小米安全密码**校验。

## 功能

- **小米账号登录**：手机号/邮箱 + 密码登录小米云（可选记住密码用于 token 失效自动续期）。触发米家 App「登录确认」2FA 时，界面会引导确认后重试。
- **在线设备列表**：名称 / 型号 / IP / token / 在线状态，token 一键复制。
- **栏目化主页**：任意添加/重命名/删除栏目，栏目内自由添加动作卡片，点击秒执行。
- **四种动作类型**
  | 类型 | 说明 | 参数 |
  |---|---|---|
  | 云端属性 (miot_prop_set) | MIoT spec 属性写入，如开关/亮度/温度 | siid、piid、value（数字/布尔/文本） |
  | 云端动作 (miot_action) | MIoT spec 动作调用，如扫地/开机 | siid、aiid、in 入参 |
  | 旧版 RPC (rpc) | /home/rpc 通用命令，兼容老设备 | method、params |
  | 局域网 miIO (miio_lan) | UDP 54321 直连（AES-128-CCM），不依赖云端 | ip、token、method、params |
- **从设备规格选取**：编辑器内按设备型号拉取 miot-spec.org 规格，可写属性/可用动作下拉选择，自动填 siid/piid/aiid 与值类型。
- **安全开关（按动作独立设置）**
  - 二次确认：点击后先弹确认框
  - 小米安全密码：执行前必须输入小米账号密码（服务端比对 MD5，不回传明文）
- **miIO 调试台**：局域网直发任意 miIO 命令（miIO.info / get_prop / set_power…）。

## 安装（fnOS）

应用中心 → 手动安装 → 选择 `dist/mihome-admin-<版本>-amd64.fpk`。

安装向导里可改 Web 端口（默认 28100）。安装完成后桌面图标打开（网关路径 `/app/mihome-admin/`）。

数据（SQLite：账号 MD5、登录态、栏目、动作）存放在 `/vol1/@appdata/mihome-admin/`。

## 本地开发

```bash
# 直接运行（Python 3.11，依赖 flask/pycryptodome/requests）
pip install -r requirements.txt
MIA_PORT=28100 MIA_DATA_DIR=./data python3 app/server/app.py

# 构建 FPK
./build.sh            # 产物: dist/mihome-admin-<版本>-amd64.fpk
```

前端为无构建原生 JS（`app/ui/`），资源一律相对路径，兼容 fnOS 网关前缀代理。

## 目录结构

```
app/
  server/
    app.py            # Flask：API + 静态 UI
    store.py          # SQLite：凭据/登录态/栏目/动作
    xiaomi/
      cloud.py        # 小米云登录(2FA)/设备列表/spec 控制（基于 micloud 增强）
      miutils.py      # RC4/签名加密（vendor 自 micloud, MIT）
      miio.py         # miIO 局域网协议（UDP 54321, AES-CCM）
    vendor/           # 构建时 pip --target 安装的依赖（不进 git）
  ui/                 # 前端（index.html + assets）
fpk/                 # FPK 打包件（manifest/cmd/main 钩子/wizard）
build.sh             # 构建脚本：vendor → app.tgz → checksum(md5) → .fpk
```

## 安全说明

- 小米账号密码仅存 **MD5**（用于安全密码校验与登录），勾选"记住密码"时才会额外保存明文以便自动续期，全部落在本机 SQLite（`/vol1/@appdata/mihome-admin/mihome.db`）。
- 云端 serviceToken 持久化于同一数据库；失效后需重新登录。
- 服务监听 0.0.0.0，建议仅在可信局域网使用，或经 fnOS 反向代理加认证后暴露。

## 致谢

- [micloud](https://github.com/erre-rodrigo/micloud)（MIT）— 小米云登录与 RC4 加密协议
- [python-miio](https://github.com/rytilahti/python-miio) — miIO 协议参考

## License

MIT

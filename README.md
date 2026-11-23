# Claude Desktop Isolation

适用于 Ubuntu / Debian 的 Claude Guard。可分别启动官方 Claude Desktop、Claude Code CLI 和独立浏览器。每种模式使用独立的资料目录，并经指定的本机代理联网。

## 功能

| 功能 | 启用方式 |
|---|---|
| 桌面、CLI、浏览器 | `--mode desktop`、`--mode cli --cli-bin …` 或 `--mode browser`。图形会话有独立窗口、登录浏览器和加密密钥环 |
| 网络与出口切换 | `--proxy-port` 指定本机 HTTP/mixed 入口。附带 Mihomo 监听器示例，可随 Clash 策略组选择切换新连接 |
| 输入法、剪贴板、窗口 | `--rime` 启用 Rime，可装雾凇拼音。`--clipboard` 同步文本和图片。`--resize` 适配缩放和全屏。默认界面缩放 125% |
| 项目、Shell 和审批 | `--workspace` 挂载单一项目，`--workspace-write` 开放写入。Shell 继承启动器的隔离。Claude Code 默认按常规方式审批，支持时可用 Auto，关闭 Bypass |
| Conda 和 CUDA | `--conda-root`、`--conda-env` 选择已有环境。Conda 默认只读。`--cuda` 开放 NVIDIA 计算设备 |
| 账号额度 | `--usage-panel` 显示五小时和每周额度及重置时间。默认显示在输入框上方，保留 Context。可用 `/quota-panel` 打开临时详情 |
| Git 和实验室集群 | `--connections` 加载本地配置。Git 可拉取指定仓库，并可开放指定分支前缀的普通推送。集群入口支持状态、文件、指定源码及 Git 历史的只读查询 |

## 快速开始

1. 按 [Anthropic 官方 Linux 安装说明](https://code.claude.com/docs/en/desktop-linux) 安装 Claude Desktop。默认使用 `/usr/lib/claude-desktop`。
2. 安装依赖，另按 [Google Chrome 官方说明](https://www.google.com/chrome/) 安装登录浏览器。需要本机图形会话里有 X11 或 Xwayland。

   ```bash
   sudo apt install bubblewrap xserver-xephyr xauth x11-utils openbox dbus \
     gnome-keyring gcr python3-secretstorage
   ```

3. 在仓库目录跑离线自检。它只连临时模拟代理，不启动 Claude，也不需要账号。

   ```bash
   ./guard.py --check
   ```

4. 从图形会话的终端启动。示例中的 `17880` 请替换为你的 HTTP 或 mixed 端口。

   ```bash
   ./guard.py --proxy-port 17880 --resize
   ```

5. 首次启动时，在隔离窗口里为私有密钥环设置非空密码。然后在隔离窗口里登录自己的 Claude 账号。密钥环与普通桌面的密钥环独立，之后重开可能需要解锁。

项目挂载位置为 `/work`。使用 `--workspace-write` 时，修改会直接写入宿主机项目目录。用 `--scale 1` 可把桌面版缩放改回 100%。关闭外层隔离窗口即可退出。选项的依赖和本地配置示例见 [集成配置](docs/integrations.md)。

## 设计边界

- 网络走 **隔离进程 → 私有回环代理 → Unix socket → 用户指定的本机代理**。网络空间只看到回环接口。代理断开时没有自动直连。外部出口、DNS 解析和代理规则由用户自己的代理决定。
- X11、D-Bus、登录浏览器和密钥环在各自的私有会话内运行。启动器不会挂载普通浏览器资料、宿主机的 SSH agent、桌面总线或 Docker socket。
- 私有资料保存在 `~/.local/share/claude-desktop-isolation/` 下。只共享明确选中的项目目录，先确保里面没有凭据、设备文件、服务套接字或敏感的嵌套挂载。
- 要限制 Claude 经代理可访问的主机，还需在代理自身配置相应规则，例如拒绝访问内网和管理接口。启动器不会修改代理、系统路由、账号权限或服务端政策。
- 这是进程隔离，依赖 Linux 用户命名空间及系统补丁。适用范围是普通用户的桌面与项目隔离。请按服务要求使用自己的账号和网络。

## 检查和故障定位

- `--check` 应返回两条 PASS，分别对应模拟上游可用和断开。如果 bubblewrap 因用户命名空间限制启动失败，检查发行版策略或联系机器管理员。
- 若私有窗口打不开登录页，先检查自己的代理协议和端口，再确认 Google Chrome 安装在 `/opt/google/chrome`。登录和回调请从私有窗口完成。
- 如果密钥环弹窗反复出现，确认私有密钥环已建立并能用非空密码解锁。私有资料保留在上述目录，可在完全退出应用后自行备份。不要把这个目录加入 Git。
- 更新官方桌面版后先跑自检，再验证一次私有窗口、账号登录和回调。本仓库不分发 Anthropic 或 Google 的应用文件。

新机器上的桌面渲染、OAuth 和密钥环交互需完成首次登录后验证。Git 与集群使用操作者自己的凭据和访问范围。

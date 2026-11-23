# 集成配置

## CLI 和独立浏览器

安装 Linux 原生 Claude Code CLI 后，把实际 ELF 可执行文件路径传给启动器。支持在 `--` 后传 Claude Code 参数。按 CLI 给出的登录提示打开 OAuth 链接并完成登录。图形模式下，桌面版的 OAuth 浏览器使用桌面版自己的资料目录。

```bash
./guard.py --mode cli --cli-bin "$(realpath "$(command -v claude)")" --proxy-port 17880 -- --version
./guard.py --mode browser --proxy-port 17880 --resize
```

## Mihomo 出口

在活动配置中添加选择组 `Claude-Egress`，成员填自己的出口。可使用 [监听器示例](../examples/clash-listener.js)，或直接给 Mihomo 配置添加相同的 loopback mixed listener，令 `proxy` 精确指向该组。保持组内所有选项符合你的出口要求。切换组成员会影响新建连接。若更换订阅，新配置也需要同名组或等效的专用监听器。 [Mihomo 监听器文档](https://wiki.metacubex.one/en/config/inbound/listeners/)

## 输入法、剪贴板、窗口与额度

- 安装 `ibus ibus-rime python3-gi dconf-service`，启动时加 `--rime`。用 `Ctrl+Shift+Space` 切换私有 IBus 内的 Rime 和英文。若要使用雾凇拼音，退出窗口后将上游词典和方案放入对应资料目录的 `.config/ibus/rime/`，并用 `default.custom.yaml` 在方案列表中选择 `rime_ice`。桌面版资料目录为 `profile`，浏览器为 `browser-profile`。 [雾凇拼音](https://github.com/iDvel/rime-ice)
- 安装 `python3-gi gir1.2-gtk-3.0` 以使用 `--clipboard`。它转发 X11 文本和图片，限制为 2 MiB 文本和 800 万像素。同步后，任一侧应用都能读取新的剪贴板内容。
- `--resize` 启用嵌套窗口跟随外层尺寸。首次使用时测试拖动、最大化、全屏和退出全屏后的点击。它需要桌面提供 X11 窗口及 RandR 支持。
- 在支持本地 UI mod 的 Claude Code 版本使用 `--usage-panel`。现有会话可用 `/reload-plugins` 刷新。用量随会话收到的数据更新，空闲时可能滞后。 [插件说明](../plugins/private-usage-panel/README.md)

## Conda、CUDA 和权限

`--conda-root /absolute/path/to/miniconda --conda-env training` 将已安装环境加入 PATH。`--conda-write` 令修改直接写进该安装目录，适合专用环境。CUDA 使用宿主 NVIDIA 驱动和设备。桌面渲染仍使用软件模式。

CLI 和桌面版 Code 读取项目提供的托管权限配置。Auto 的可用性由 Claude Code 版本、账号及组织政策决定。`--workspace-write` 和 `--conda-write` 允许修改挂载目录。

## Git 和集群

复制 [配置示例](../examples/connections.example.json) 到仓库及挂载目录之外，替换示例值，并对配置和密钥设置 `chmod 600`。用 `--connections ~/guard-connections.local.json` 启用。可以删除整个 `git` 或 `clusters` 段。通过可信渠道验证服务端公钥，并预先写入 `known_hosts`。SSH 使用可非交互使用的专用密钥。密钥和配置留在宿主机，隔离进程只访问 Unix socket。

- **Git** 使用 `git@github.com:OWNER/REPO.git` 作为隔离会话里的远端地址。宿主通过 GitHub SSH 443 连接。默认经同一个代理入口，需要 `netcat-openbsd`。删除 `review_prefix` 只启用拉取。设定前缀后仅可新增或快进对应分支。建议专用仓库级凭据。审阅和合并在正常的 GitHub 流程里进行。
- **集群** 的 `roots` 可查账号所有的非隐藏文件和目录。正文限 2 MiB UTF-8 文本。过滤常见凭据格式，但开放目录应自行确认可以分享。`mounts` 用于读取存储状态。`repositories` 单独设置仓库、允许的源码子目录，以及用于读取历史的批准提交 ID。远程需有 Python 3，源码历史另外需要 Git。

```bash
cluster-code hosts
cluster-code worker status
cluster-code worker list /home/example-user/project
cluster-code worker read /home/example-user/project/README.md > /work/cluster-readme.md
cluster-code worker log demo
cluster-code worker list-worktree demo
```

集群状态包含 GPU 显存、占用进程和运行时长、内存及配置的挂载点余量。只读接口由宿主执行固定操作。用于复制的重定向写入本地项目，需项目可写。服务端支持专用只读账号时，可将其作为额外权限边界。

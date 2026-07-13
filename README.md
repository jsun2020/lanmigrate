# LanMigrate

局域网内两台电脑之间的文件迁移工具:**可中断、可续传、自动跳过依赖目录**。
支持 Windows -> Windows 和 Windows -> Mac(双向均可,接收端/发送端跨平台)。

- 断了再续:Ctrl+C、合盖、断网、关机都安全,重跑即从断点继续
- 换网也行:按设备指纹重新发现新 IP,任务自动接续
- 自动跳过 `node_modules` / `venv` / `target` 等依赖目录(上下文感知,只有旁边有 `package.json` 等标记文件才排除)
- 传输引擎为 [rclone](https://rclone.org)(自动下载,无需手动安装)

## 快速开始

两台电脑都需要 Python 3.11+,克隆本仓库后:

```
pip install typer rich zeroconf
```

### 1. 新电脑(接收端)

Windows 或 Mac 均可:

```
python -m lanmigrate receive D:\Migration        # Windows
python -m lanmigrate receive ~/Migration          # Mac
```

屏幕会显示一个 **6 位配对码** 和本机 IP。窗口保持打开。

> Windows 首次使用请以管理员放行防火墙端口(命令已在屏幕上给出);
> Mac 首次运行会弹一次"允许接受传入网络连接",点允许。

### 2. 旧电脑(发送端)

```
python -m lanmigrate send D:\projects
```

流程:扫描 -> 显示"智能排除建议"(可勾选)-> 自动发现接收端(或手动
`--host <IP>`)-> 输入配对码 -> 开始传输。之后即可走人:

- 任何时候 Ctrl+C / 断网 / 关机都安全
- 恢复:`python -m lanmigrate resume`(换了 Wi-Fi/IP 也能自动找回设备)
- 全自动循环重跑,直到所有文件成功才停(被占用的文件下一轮自动补传)

### 3. 其他命令

```
python -m lanmigrate tasks               # 查看所有迁移任务
python -m lanmigrate send --help         # 全部参数(--yes 免确认、--dest 子目录等)
python -m lanmigrate receive --code 123456 --port 2022   # 固定配对码/端口
```

## 排除规则

规则在 `lanmigrate/rules.toml`,数据与代码分离:

- **上下文相关排除**:目录旁存在标记文件才排除(有 `package.json` 才排 `node_modules`;
  一个恰好叫 `build` 的普通文件夹不会被误杀)
- **全局垃圾**:`Thumbs.db`、`$RECYCLE.BIN`、`.DS_Store`、`__pycache__` 等无条件排除
- `.git` 默认保留(历史有价值),体积超过 1GB 时在报告中提示

欢迎提交 PR 扩充语言生态。

## 工作原理

```
发送端(旧电脑)                          接收端(新电脑)
scanner: 扫描 + 规则引擎                  mDNS 广播 + 配对码
discovery: mDNS 发现          --SFTP-->   rclone serve sftp(会话凭据)
taskstore: 任务持久化/断点
engine: rclone copy 封装(JSON 日志进度)
```

- SFTP 密码由配对码本地推导,不经网络传输;rclone 自带传输加密
- 断点判定:大小 + 修改时间(可跑 `rclone check` 严格校验,见 `M0-迁移指南.md` 第五节)
- 任务状态存于 `~/.lanmigrate/tasks/`,原子写入,中断不损坏

## 手动兜底(不装 Python 的机器)

`migrate.ps1` + `filters.txt` + `M0-迁移指南.md` 是纯 rclone 的手工方案,
行为与本工具一致,可用于无法装 Python 的接收端(如临时借用的电脑)。

## 开发

```
python -m venv .venv && .venv\Scripts\activate
pip install typer rich zeroconf pytest
pytest
```

产品需求与验收标准见 `prd.md`。MIT License。

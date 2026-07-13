# PRD: LanMigrate — 局域网智能断点迁移工具

> 版本:v1.0 | 日期:2026-07-13 | 状态:草稿
> 定位:开源工具 + AI 编程教学案例

---

## Version Update: v0.2.0 - 2026-07-13

### Feature Summary
快速启动模式(新默认):仅遍历目录结构生成排除清单(秒级),跳过全量体积统计,配对后立即开始传输 — AirDrop 式体验。原完整预扫描(F6 体积报告)改为 `--full` 可选。

### Business Value
用户反馈:配对前的全盘扫描等待数分钟,与 AirDrop"配对即传"的预期差距大。传输层本身已是线速(rclone 8 并发),唯一可省的开销就是预扫描的 stat 全量文件 + 排除目录体积计算。

### Solution Overview
`scanner.scan()` 增加 `compute_sizes` 开关:关闭时不逐文件 stat、不计算排除目录体积(size 记为 -1 = 未知),目录结构遍历秒级完成;排除确认表仍然展示(F3 人工确认是硬性要求),体积列显示 "?"。`--full` 保留原行为与"节省了 X GB"报告。

### Affected Components
| Component | Change Type | Description |
|-----------|-------------|-------------|
| scanner.py | Modified | compute_sizes 开关;saved_bytes 只累加已知体积 |
| cli.py | Modified | send 默认快速模式,新增 --full;显示逻辑适配未知体积 |
| engine/taskstore/discovery/pairing | No Change | 与扫描深度正交 |

### Acceptance Criteria
- [x] 默认 send 在大目录上数秒内进入排除确认(不再分钟级)
- [x] 快速模式排除仍然上下文相关且可确认;filter 内容与完整模式一致
- [x] --full 行为与 v0.1 完全一致(体积报告、节省统计)
- [x] 全部既有测试通过,e2e(中断+续传)通过

---

## 1. 背景与问题

### 1.1 真实场景

用户要将旧电脑(Windows 10)上 **500GB** 文件迁移到新电脑(Windows 11 或 Mac),面临四个痛点:

| 痛点 | 描述 |
|------|------|
| 一次传不完 | 500GB 在千兆局域网理想状态也要 1~2 小时,实际混杂大量小文件,常常需要数天 |
| 白天要干活 | 迁移只能利用碎片时间,必须支持"随时停、随时续" |
| 传输常中断 | 睡眠、断网、换 Wi-Fi 都会打断,传统拷贝(资源管理器/AirDrop)中断即作废 |
| 依赖文件浪费时间 | 开发者的项目目录里,`node_modules`、`venv` 等依赖动辄占一半体积,传过去毫无意义 |

### 1.2 现有方案的不足

- **资源管理器 / 访达直接拷贝**:不支持断点续传,中断需重来
- **rclone**:能力完备,但命令行门槛高,排除规则要手写,普通用户(包括零基础学员)用不起来
- **Syncthing**:持续同步模型,不适合"一次性迁移"场景,配置概念多
- **微信/网盘中转**:500GB 不现实

### 1.3 机会

rclone 已解决 90% 的底层难题(断点续传、校验、跨平台、局域网传输)。缺的是:

1. **一个傻瓜化的图形界面/交互式 CLI**
2. **自动识别项目依赖并跳过的"智能层"**

这正是本工具的价值:**做 rclone 之上的体验层与智能层,不重复造传输轮子。**

---

## 2. 产品定位

**一句话**:局域网内两台电脑之间的"可中断、可续传、会自动跳过依赖"的文件迁移工具。

- **名称(暂定)**:LanMigrate(备选:EasyMove / 搬家侠)
- **形态**:桌面工具,MVP 为交互式 CLI,v2 提供 GUI
- **平台**:Windows 10/11、macOS(Intel & Apple Silicon)
- **引擎**:内置 rclone 二进制,用户无需单独安装
- **开源协议**:MIT
- **教学价值**:涵盖 CLI 设计、进程封装、网络发现、状态持久化、跨平台打包——是一个完整的中级实战项目

### 2.1 目标用户

| 用户 | 场景 |
|------|------|
| 换电脑的开发者(核心) | 项目多、依赖多,最需要智能排除 |
| 普通换机用户 | 照片、文档、视频批量搬家 |
| AI 编程学员 | 作为教学项目,从 PRD 到发布走完全流程 |

### 2.2 非目标(明确不做)

- ❌ 不做持续双向同步(那是 Syncthing 的领域)
- ❌ 不做公网/跨局域网传输(v1 不做,依赖中断后"回到同一局域网"即可续传)
- ❌ 不做手机端
- ❌ 不自研传输协议(rclone 引擎)

---

## 3. 核心功能

### 3.1 功能总览与优先级

| 编号 | 功能 | 优先级 | MVP |
|------|------|--------|-----|
| F1 | 局域网设备发现与配对 | P0 | ✅ |
| F2 | 断点续传迁移(rclone 引擎) | P0 | ✅ |
| F3 | 智能依赖识别与排除 | P0 | ✅ |
| F4 | 迁移任务持久化(停机/换网后恢复) | P0 | ✅ |
| F5 | 进度展示与"省了多少 GB"报告 | P1 | ✅ |
| F6 | 迁移前预扫描与体积估算 | P1 | ✅ |
| F7 | 完整性校验报告(抽样/全量哈希) | P1 | ⬜ |
| F8 | 排除规则自定义与白名单 | P1 | ⬜ |
| F9 | GUI(Tauri/Electron) | P2 | ⬜ |
| F10 | 迁移完成后的"依赖重建指南"生成 | P2 | ⬜ |

### 3.2 F1 局域网设备发现与配对

**用户故事**:我在两台电脑上打开工具,它们能自动发现彼此,我输入一个 6 位配对码即可建立连接,不需要查 IP。

- 接收端启动时通过 **mDNS(zeroconf)** 广播服务:`_lanmigrate._tcp.local`
- 发送端自动列出局域网内的接收设备(设备名 + IP)
- 配对采用 **6 位数字码**(接收端屏幕显示,发送端输入),防止误连
- 配对成功后保存设备指纹(公钥),换局域网/换 IP 后重连**无需重新配对**
- 兜底:支持手动输入 `IP:端口` 直连(应对 mDNS 被防火墙拦截的情况)

**验收标准**:
- [ ] 同一局域网内 5 秒内发现对方
- [ ] 换 Wi-Fi 后重启双方工具,30 秒内自动重连并恢复任务
- [ ] mDNS 不可用时手动直连可用

### 3.3 F2 断点续传迁移

**用户故事**:我晚上开始迁移,早上直接合盖出门;晚上回家打开两台电脑,工具从昨晚断掉的地方继续。

- 接收端运行 `rclone serve sftp`(内置随机生成的用户名/密码,仅本次迁移会话有效)
- 发送端调用 `rclone copy`,关键参数:
  - `--partial-suffix .lanmigrate-part`:大文件传一半保留断点
  - 文件级跳过:按 `大小 + 修改时间` 判断(默认),可选 `--checksum` 严格模式
  - `--transfers 8 --checkers 16`:并发拉满局域网带宽
- **任意时刻可暂停/中断**(Ctrl+C、关机、断网都安全),已完成的文件不会重传
- 目标端已存在且一致的文件自动跳过 → 天然支持"跑 N 次直到传完"

**全自动无人值守(硬性要求)**:除启动时的一次性配置外,传输全程**零弹窗、零人工确认**:

| 场景 | Explorer 行为 | 本工具行为 |
|------|---------------|-----------|
| 保留设备名文件(`nul`/`con`/`aux`/`prn`/`com1~9`/`lpt1~9`) | 弹窗等确认 | rclone 走 `\\?\` 前缀直接读写,无感通过 |
| 路径超过 260 字符 | 报错中断 | `\\?\` 前缀绕过限制,正常传输 |
| 文件被其他程序锁定 | 弹窗"文件正在使用" | 本轮跳过 + 记日志,后续轮次自动重试 |
| 符号链接 / junction | 可能死循环 | `--skip-links` 跳过并记录 |
| 网络瞬断 / Wi-Fi 抖动 | 整体失败 | `--retries 5 --low-level-retries 20` 自动重试 |
| 本轮结束仍有失败文件 | — | 外层循环自动重跑,直到 exit code 0 |

**验收标准**:
- [ ] 传输中强制断网,恢复后不重传已完成文件
- [ ] 10GB 单个大文件传输 50% 时中断,恢复后从断点继续(而非从 0 开始)
- [ ] 千兆局域网实测吞吐 ≥ 80 MB/s(大文件场景)
- [ ] 源目录含名为 `nul`、`con` 的文件时,全程无任何弹窗,文件正常传输
- [ ] 路径长度 >260 字符的文件正常传输
- [ ] 人为锁定某文件(用程序占用),该文件本轮被跳过且记入日志,解锁后下一轮自动补传
- [ ] 从启动传输到全部完成,除 Ctrl+C 外无任何需要人工输入/点击的环节

### 3.4 F3 智能依赖识别与排除 ⭐(差异化核心)

**用户故事**:我的 `D:\projects` 下有 40 个项目,工具扫描后告诉我"检测到 32 个 node_modules、8 个 venv,共 87GB 可跳过",我确认后它们不会被传输。

**识别策略 —— 基于"项目标记文件"的规则引擎(无需 AI,快且准)**:

| 检测到的标记文件 | 判定项目类型 | 排除目录 |
|------------------|--------------|----------|
| `package.json` | Node.js | `node_modules/`, `.next/`, `dist/`, `build/`, `.turbo/`, `.cache/` |
| `requirements.txt` / `pyproject.toml` | Python | `venv/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.pyc` |
| `Cargo.toml` | Rust | `target/` |
| `pom.xml` / `build.gradle` | Java | `target/`, `build/`, `.gradle/` |
| `Podfile` | iOS | `Pods/`, `DerivedData/` |
| `go.mod` | Go | `vendor/`(可选,提示用户) |
| `composer.json` | PHP | `vendor/` |
| `.git/` 存在 | Git 仓库 | `.git/` 默认**保留**(历史有价值),体积 >1GB 时提示用户选择 |

**关键设计**:
- 排除是**上下文相关**的:只有当目录旁存在对应标记文件时才排除(避免误杀某个恰好叫 `build` 的用户文件夹)
- 扫描后输出**排除清单报告**,用户可逐项勾选/取消,确认后才生效
- 规则表存于 `rules.toml`,开源社区可提交 PR 扩充语言生态
- 全局兜底排除:`$RECYCLE.BIN`、`System Volume Information`、`Thumbs.db`、`.DS_Store`、`pagefile.sys` 等系统垃圾

**验收标准**:
- [ ] 对含 `package.json` 的目录正确排除 `node_modules`
- [ ] 对**不含**任何标记文件、但目录名为 `build` 的普通文件夹**不排除**
- [ ] 报告准确显示"预计跳过 X 个目录,共节省 Y GB"

### 3.5 F4 任务持久化

- 任务状态存于本地 `~/.lanmigrate/tasks/<task-id>.json`:
  - 源/目标路径、排除规则快照、配对设备指纹
  - rclone 运行日志与最后进度
- 重启工具 → 检测到未完成任务 → 提示"继续上次迁移?"一键恢复
- 换局域网:设备指纹不变,重新发现 IP 后自动续跑

### 3.6 F5 进度与报告

- 实时显示:总进度条、当前文件、速度、剩余时间(解析 rclone `--use-json-log` 输出)
- 完成报告:
  - 传输文件数 / 总体积 / 耗时 / 平均速度
  - **"智能排除为你节省了 87.3 GB(约 2.1 小时)"** ← 教学与传播亮点
  - 失败文件清单(如有)与重试建议

---

## 4. 技术方案

### 4.1 技术选型

| 层 | 选型 | 理由 |
|----|------|------|
| 语言 | **Python 3.11+** | 教学友好(学员零基础可读懂),生态全 |
| CLI 框架 | Typer + Rich | 现代交互式 CLI,进度条/表格开箱即用 |
| 传输引擎 | rclone(随包分发对应平台二进制) | 不重造轮子,断点续传/校验/跨平台全解决 |
| 设备发现 | python-zeroconf | mDNS 标准实现 |
| 状态存储 | JSON 文件(MVP)→ SQLite(v2) | 由简入繁,教学有梯度 |
| 打包 | PyInstaller → 单文件 exe / dmg | 用户零依赖运行 |
| GUI(v2) | Tauri(前端 React) | 体积小,可复用团队 React 经验 |

### 4.2 架构图

```
┌─────────────── 发送端(旧电脑)───────────────┐
│  CLI (Typer/Rich)                             │
│  ├── Scanner:目录扫描 + 规则引擎(F3)        │
│  ├── Discovery:mDNS 浏览(F1)               │
│  ├── TaskStore:任务持久化(F4)              │
│  └── Engine:rclone copy 子进程封装(F2)     │
│        │  解析 --use-json-log → 进度(F5)    │
└────────┼──────────────────────────────────────┘
         │  SFTP over LAN
┌────────▼──────────────────────────────────────┐
│  接收端(新电脑)                              │
│  ├── Discovery:mDNS 广播 + 配对码(F1)      │
│  └── rclone serve sftp(临时凭据)            │
└───────────────────────────────────────────────┘
```

### 4.3 核心流程(发送端)

```
启动 → 检测未完成任务?
  ├─ 是 → 恢复任务 → 重新发现设备 → 续传
  └─ 否 → 选择源目录
           → 预扫描(F6):统计体积 + 依赖识别(F3)
           → 展示排除报告,用户确认
           → 发现/配对接收端(F1)
           → 生成 rclone filter 文件
           → 启动 rclone copy(F2)
           → 实时进度(F5)…… 可随时中断
           → 完成 → 校验(F7)→ 输出报告
```

### 4.4 安全设计(局域网场景够用即可)

- SFTP 自带传输加密
- 每次会话随机生成凭据,迁移结束即失效
- 配对码防止连错设备;设备指纹防中间人
- 接收端仅监听局域网网卡,不暴露公网

---

## 5. 里程碑

| 阶段 | 内容 | 产出 | 预估 |
|------|------|------|------|
| M0 Demo ✅ | 手写 rclone 命令跑通 Win→Mac 断点迁移 | `migrate.ps1` + `filters.txt` + 操作指南(见附录 B) | 已完成,待实测 |
| M1 MVP | F1~F6,CLI 版,双平台打包 | v0.1 可用版(先解决自己的 500GB) | 1~2 周 |
| M2 完善 | F7 校验、F8 自定义规则、单元测试、文档 | v0.5 开源发布(GitHub) | 2 周 |
| M3 GUI | Tauri 图形界面、F10 依赖重建指南 | v1.0 | 1 个月 |

> M0 的意义:**先用最土的办法把这次 500GB 迁移跑起来**,同时验证 rclone 参数,MVP 开发与实际迁移并行,互不阻塞。

---

## 6. 开源与教学规划

### 6.1 开源

- **仓库结构**:`lanmigrate/`(核心)+ `rules.toml`(社区可贡献的规则库)+ `docs/`
- **README 卖点**:三张图讲清楚 —— ①断了再续 ②换网也行 ③自动跳过 node_modules 省 XX GB
- **首发渠道**:GitHub + V2EX/少数派/掘金 迁移话题帖
- **国际化**:CLI 文案中英双语(`--lang` 参数)

### 6.2 教学拆解(SuperAI编程团队 课程模块)

| 课次 | 主题 | 对应代码 |
|------|------|----------|
| 1 | 从真实痛点到 PRD | 本文档 |
| 2 | 子进程封装:让 Python 指挥 rclone | Engine |
| 3 | 规则引擎:用数据结构替代 if-else | Scanner + rules.toml |
| 4 | 网络发现:mDNS 是怎么回事 | Discovery |
| 5 | 状态机与持久化:程序如何"记住"进度 | TaskStore |
| 6 | 打包发布:从脚本到产品 | PyInstaller + GitHub Release |

每一课都是"当天可运行的 demo",符合 build first 教学法。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| mDNS 被公司网络/防火墙屏蔽 | 发现失败 | 手动 IP 直连兜底(F1 已含) |
| Windows Defender 拦截 SFTP 端口 | 连接失败 | 首次运行引导添加防火墙规则,文档给出手动步骤 |
| 用户误排除了重要目录 | 数据缺失 | 排除必须人工确认;报告持久化可追溯;`.git` 默认保留 |
| rclone 授权协议 | 开源合规 | rclone 为 MIT,可随包分发,注明来源即可 |
| PyInstaller 打包被杀软误报 | 安装受阻 | 提供源码运行方式;后期考虑代码签名 |

---

## 8. 成功指标

- **自用验证**:本次 500GB 迁移全程使用本工具完成,中断 ≥3 次仍成功
- **智能排除**:实际迁移中跳过体积 ≥ 总量的 15%
- **开源**:发布 3 个月内 GitHub ≥ 200 star,收到 ≥ 5 个社区规则 PR
- **教学**:≥ 1 期学员完整跟做并交付自己的构建版本

---

## 附录 A:MVP 命令行交互示意

```
$ lanmigrate send D:\projects

🔍 正在扫描 D:\projects ...
   共 42 个项目,总计 486.2 GB

💡 智能排除建议(可节省 87.3 GB):
   [x] 32 × node_modules     71.4 GB
   [x]  8 × venv/.venv       12.1 GB
   [x] 全部 __pycache__       2.6 GB
   [x] 系统垃圾文件           1.2 GB
   [ ] .git 目录(默认保留)  14.8 GB
   回车确认 / 输入编号切换选项

📡 发现局域网设备:
   1. Jason-MacBook (192.168.1.8)
   请在对方屏幕查看配对码并输入:______

🚀 开始迁移(实际需传输 398.9 GB)
   ██████████░░░░░░░░░░ 52.3% | 96 MB/s | 剩余 34 分钟
   当前:projects/history-story/assets/bgm.wav
   (随时 Ctrl+C 中断,下次运行自动继续)
```

---

## 附录 B:M0 参考实现(已验证方案,MVP 开发的行为基准)

> M1 开发的本质 = 把本附录的手工流程产品化。工具的每个行为都能在这里找到对应的 rclone 原语。
> 配套文件:`migrate.ps1`(发送端脚本)、`filters.txt`(排除规则)、`M0-迁移指南.md`(完整操作文档)。

### B.1 接收端:Mac(Windows → Mac 场景)

```bash
# 1. 安装 rclone
brew install rclone

# 2. 创建接收目录,查看本机 IP
mkdir -p ~/Migration
ipconfig getifaddr en0        # 该 IP 填入发送端脚本 $RemoteHost

# 3. 启动接收服务(caffeinate 同时防止 Mac 睡眠)
caffeinate -s rclone serve sftp ~/Migration \
  --addr :2022 --user lanmigrate --pass ChangeMe2026
```

- 首次运行 macOS 弹一次"允许接受传入网络连接"→ 允许(仅此一次)
- 终端窗口保持打开;中断后重跑同一条命令即可,发送端自动续传
- 密码必须与发送端脚本中 `$RemotePass` 一致

### B.2 接收端:Windows 11(Windows → Windows 场景)

```powershell
winget install Rclone.Rclone
mkdir D:\Migration
# 防火墙放行(管理员权限,一次性)
New-NetFirewallRule -DisplayName "LanMigrate SFTP" -Direction Inbound `
  -LocalPort 2022 -Protocol TCP -Action Allow
rclone serve sftp D:\Migration --addr :2022 --user lanmigrate --pass ChangeMe2026
```

### B.3 发送端:Windows 10(核心命令)

完整脚本见 `migrate.ps1`,核心命令等价于:

```powershell
rclone copy D:\ ":sftp,host=<接收端IP>,port=2022,user=lanmigrate,pass=<obscured>:/" `
  --filter-from filters.txt `
  --transfers 8 --checkers 16 `
  --partial-suffix ".part" `
  --retries 5 --retries-sleep 15s --low-level-retries 20 `
  --skip-links --create-empty-src-dirs `
  --log-file migrate.log --log-level INFO --progress
```

外层 while 循环:exit code ≠ 0 → 等 60 秒重跑,直到 0(全部成功)为止。

### B.4 M0 已验证的关键结论(MVP 必须继承)

1. `rclone copy` 为非交互命令 → 保留名/长路径/锁定文件均不产生弹窗(对应 F2 无人值守验收标准)
2. 断点判定用"大小+修改时间"足够快且可靠;`--checksum` 留作可选严格模式
3. 密码需经 `rclone obscure` 处理后才能用于连接字符串
4. 防睡眠是实操刚需:Mac 用 `caffeinate -s` 包裹命令,Windows 需引导用户设置电源选项(MVP 可调用 `powercfg` 临时禁止睡眠、结束后恢复)

---

## 附录 C:Claude Code 开发交接说明

> 本 PRD + 附录 B 即完整开发输入。按以下顺序实施,每步可独立验收。

### C.1 仓库结构

```
lanmigrate/
├── lanmigrate/
│   ├── __init__.py
│   ├── cli.py            # Typer 入口:send / receive / resume 三个子命令
│   ├── engine.py         # rclone 子进程封装(启动/停止/解析 --use-json-log 进度)
│   ├── scanner.py        # 目录扫描 + 规则引擎(F3:标记文件 → 上下文排除)
│   ├── discovery.py      # mDNS 广播与浏览(F1),含手动 IP 兜底
│   ├── pairing.py        # 6 位配对码 + 设备指纹持久化
│   ├── taskstore.py      # 任务持久化(~/.lanmigrate/tasks/<id>.json)
│   ├── rclone_bin.py     # rclone 二进制定位/下载(按平台)
│   └── rules.toml        # 排除规则库(数据与代码分离,社区可贡献)
├── tests/
│   ├── test_scanner.py   # 重点:上下文排除的正/反用例
│   ├── test_engine.py    # mock rclone 输出,测进度解析与断点恢复
│   └── fixtures/         # 模拟项目目录树(含 nul 文件、长路径用例)
├── docs/
├── pyproject.toml
└── README.md
```

### C.2 开发顺序(每步一个可运行 demo)

| 步骤 | 模块 | 验收方式 |
|------|------|----------|
| 1 | `rclone_bin.py` + `engine.py` | Python 调 rclone 完成一次小目录复制,实时打印进度 |
| 2 | `scanner.py` + `rules.toml` | 对 fixtures 目录输出排除报告,正反用例全过(见 F3 验收标准) |
| 3 | `taskstore.py` | 中断后 `lanmigrate resume` 恢复任务 |
| 4 | `discovery.py` + `pairing.py` | 两台真机 5 秒内互相发现,配对码验证通过 |
| 5 | `cli.py` 整合 | 复现附录 A 的完整交互流程 |
| 6 | PyInstaller 打包 | Win10/Win11/macOS 三平台单文件产物,真机跑通 500GB 级迁移 |

### C.3 关键实现约束

1. **engine.py 必须用 `--use-json-log` + 逐行解析 stderr**,不要用正则抓普通文本输出(格式不稳定)
2. **scanner.py 的排除必须上下文相关**:仅当同级/父级存在标记文件时才排除(F3 表格),规则全部来自 `rules.toml`,代码里不写死目录名
3. **所有 rclone 参数集中在 engine.py 一处定义**,以附录 B.3 为基准参数集,禁止散落各处
4. **无人值守是硬约束**:任何代码路径不允许出现阻塞式确认(除迁移开始前的排除清单确认);测试需覆盖 F2 无人值守全部验收项
5. **中断安全**:SIGINT/SIGTERM 时先写 taskstore 再退出;taskstore 写入用"临时文件 + 原子重命名"防止半截 JSON
6. **跨平台路径**:统一用 `pathlib`,Windows 侧注意 `\\?\` 前缀由 rclone 处理、Python 层不重复处理
7. **rclone 二进制**:开发期允许使用系统已安装的 rclone;打包时按平台内置对应二进制并校验哈希

### C.4 建议给 Claude Code 的启动指令

```
请阅读 prd.md(含附录 B/C),按附录 C.2 的顺序实施 LanMigrate 项目。
从步骤 1 开始,每完成一步先运行验收再进入下一步。
技术栈:Python 3.11 + Typer + Rich + python-zeroconf,rclone 为传输引擎。
参考 migrate.ps1 和 filters.txt 中已验证的参数与规则。
```

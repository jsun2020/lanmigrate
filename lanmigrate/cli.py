"""LanMigrate CLI: send / receive / resume / tasks (PRD Appendix A flow).

Unattended by design (PRD C.3-4): the only interactive moments are the
pre-transfer exclusion confirmation and pairing-code entry, both skippable
with flags. The transfer loop reruns rclone until exit code 0.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from . import __version__, discovery, engine, pairing, scanner, taskstore

app = typer.Typer(add_completion=False, help="局域网断点续传迁移工具 (LAN migration with resume)")
console = Console()

ASSUMED_LAN_SPEED = 50 * 1024 * 1024  # bytes/s, for "time saved" estimate only


def _human(n: float) -> str:
    if n < 0:
        return "?"  # unknown (fast scan skips sizing)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


@app.callback()
def _version_banner() -> None:
    pass


def _prepare_rclone():
    """ensure_rclone with visible feedback: the first run downloads ~25MB,
    which previously happened in total silence and looked like a hang."""
    from . import rclone_bin

    found = rclone_bin.find_rclone()
    if found:
        return found
    console.print("  首次运行: 正在下载 rclone(约 25MB,视网络情况需 1~3 分钟)…")
    console.print("  [dim]提示: 预先安装 rclone 可跳过此步(Mac: brew install rclone)[/]")
    with console.status("下载 rclone 中…"):
        path = rclone_bin.download_rclone()
    console.print(f"  rclone 已就绪: {path}")
    return path


# ---------------------------------------------------------------- receive


@app.command()
def receive(
    directory: Path = typer.Argument(None, help="接收目录,默认 ~/Migration"),
    port: int = typer.Option(2022, help="SFTP 监听端口"),
    code: Optional[str] = typer.Option(None, help="固定配对码(默认随机生成)"),
    mdns: bool = typer.Option(True, help="是否开启 mDNS 广播(局域网自动发现)"),
) -> None:
    """在新电脑上运行:启动接收服务并显示配对码。"""
    directory = directory or (Path.home() / "Migration")
    directory.mkdir(parents=True, exist_ok=True)
    console.print(f"\n[bold cyan]LanMigrate v{__version__} - 接收端[/]")
    rclone = _prepare_rclone()
    code = code or pairing.generate_code()
    password = pairing.session_password(code)
    fp = pairing.device_fingerprint()

    console.print(f"  接收目录: {directory}")
    console.print(f"  本机地址: {discovery.local_ip()}:{port}")
    console.print(f"  rclone:   {rclone}")
    console.print(f"\n  配对码(在发送端输入): [bold yellow]{code}[/]\n")
    console.print("  发送端命令: lanmigrate send <源目录>")
    console.print(f"  (若自动发现失败,发送端加参数 --host {discovery.local_ip()} --port {port})")
    console.print("  [dim]Windows 若连不上,请以管理员放行防火墙端口:")
    console.print(f"  [dim]New-NetFirewallRule -DisplayName LanMigrate -Direction Inbound -LocalPort {port} -Protocol TCP -Action Allow[/]")

    announcer = None
    if mdns:
        announcer = discovery.Announcer(port=port, fingerprint=fp)
        announcer.start()
        console.print("  mDNS 广播已开启,发送端可自动发现本机")

    proc = engine.serve_sftp(directory, port, pairing.SFTP_USER, password)
    console.print("\n[green]接收服务已启动,等待传输…(Ctrl+C 停止,重启后发送端自动续传)[/]\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        console.print("\n[yellow]接收服务已停止。重新运行本命令即可继续接收(断点自动续传)。[/]")
    finally:
        proc.terminate()
        if announcer:
            announcer.stop()


# ---------------------------------------------------------------- send


def _confirm_exclusions(report: scanner.ScanReport, assume_yes: bool) -> set[str]:
    """Show the exclusion report; let the user toggle entries. Returns the
    set of rel-paths to exclude."""
    enabled = {e.rel for e in report.exclusions}
    if not report.exclusions:
        console.print("  未发现可跳过的依赖目录")
        return enabled

    while True:
        if report.saved_bytes > 0:
            title = f"智能排除建议(共可节省 {_human(report.saved_bytes)})"
        else:
            title = f"智能排除建议(共 {len(report.exclusions)} 个依赖目录)"
        table = Table(title=title)
        table.add_column("#", justify="right")
        table.add_column("选中")
        table.add_column("类型")
        table.add_column("目录")
        table.add_column("大小", justify="right")
        for i, e in enumerate(report.exclusions, 1):
            mark = "[green][x][/]" if e.rel in enabled else "[dim][ ][/]"
            table.add_row(str(i), mark, e.rule, e.rel, _human(e.size))
        console.print(table)
        for rel, size in report.large_git_dirs:
            console.print(f"  [dim]提示: {rel} 体积较大({_human(size)}),.git 默认保留[/]")

        if assume_yes:
            return enabled
        answer = typer.prompt("回车确认 / 输入编号切换选中 / n 取消", default="", show_default=False).strip()
        if answer == "":
            return enabled
        if answer.lower() == "n":
            raise typer.Abort()
        try:
            idx = int(answer)
            rel = report.exclusions[idx - 1].rel
            enabled.symmetric_difference_update({rel})
        except (ValueError, IndexError):
            console.print("[red]无效输入[/]")


def _pick_receiver(host: Optional[str], port: int) -> tuple[str, int, str]:
    """Returns (host, port, fingerprint). Manual --host wins; otherwise mDNS."""
    if host:
        return host, port, ""
    console.print("正在搜索局域网设备…")
    receivers = discovery.discover(timeout=5.0)
    if not receivers:
        console.print("[yellow]未发现设备(mDNS 可能被防火墙拦截)。请手动输入接收端 IP。[/]")
        host = typer.prompt("接收端 IP")
        return host, port, ""
    for i, r in enumerate(receivers, 1):
        console.print(f"  {i}. {r.name} ({r.host}:{r.port})")
    if len(receivers) == 1:
        chosen = receivers[0]
    else:
        idx = typer.prompt("选择设备编号", type=int)
        chosen = receivers[idx - 1]
    return chosen.host, chosen.port, chosen.fingerprint


def _run_task(task: taskstore.MigrationTask, max_rounds: int, wait: int) -> None:
    """Unattended transfer loop: rerun rclone until exit code 0 (PRD F2)."""
    task.status = taskstore.STATUS_RUNNING
    taskstore.save(task)
    remote = engine.sftp_remote(task.host, task.port, task.user, task.obscured_pass, task.dest)
    started = time.time()
    last = engine.Progress()

    try:
        for rnd in range(task.rounds_completed + 1, max_rounds + 1):
            console.print(f"[yellow]======== 第 {rnd} 轮传输 {time.strftime('%Y-%m-%d %H:%M:%S')} ========[/]")
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as bar:
                tid = bar.add_task("传输中", total=None)

                def on_progress(p: engine.Progress) -> None:
                    nonlocal last
                    last = p
                    name = p.current if len(p.current) <= 40 else "…" + p.current[-39:]
                    bar.update(tid, completed=p.bytes_done,
                               total=p.total_bytes or None,
                               description=name or "传输中")

                rc = engine.run_copy(Path(task.source), remote,
                                     filter_file=task.filter_file,
                                     log_file=task.log_file,
                                     on_progress=on_progress)
            task.rounds_completed = rnd
            taskstore.save(task)

            if rc == 0:
                task.status = taskstore.STATUS_DONE
                taskstore.save(task)
                elapsed = time.time() - started
                console.print("\n[bold green]全部完成!所有文件已成功迁移。[/]")
                console.print(f"  本次传输: {_human(last.bytes_done)},耗时 {elapsed/60:.1f} 分钟")
                if task.saved_bytes:
                    saved_min = task.saved_bytes / ASSUMED_LAN_SPEED / 60
                    console.print(f"  [bold]智能排除为你节省了 {_human(task.saved_bytes)}(约 {saved_min:.0f} 分钟)[/]")
                console.print(f"  日志: {task.log_file}")
                return
            console.print(f"[yellow]本轮结束,仍有文件未完成(exit {rc},详见日志)。{wait} 秒后自动重试…[/]")
            console.print("  常见原因: 文件被占用、网络瞬断 - 下一轮自动重试这些文件。")
            time.sleep(wait)
        console.print(f"[red]已达最大轮数({max_rounds})仍未完成,请检查日志: {task.log_file}[/]")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        taskstore.save(task)  # 中断安全: 先落盘再退出 (PRD C.3-5)
        console.print("\n[yellow]已中断。任务已保存,运行 lanmigrate resume 从断点继续。[/]")
        raise typer.Exit(130)


@app.command()
def send(
    source: Path = typer.Argument(..., exists=True, file_okay=False, help="要迁移的源目录"),
    host: Optional[str] = typer.Option(None, help="接收端 IP(mDNS 失败时手动指定)"),
    port: int = typer.Option(2022, help="接收端端口"),
    code: Optional[str] = typer.Option(None, help="接收端屏幕上的 6 位配对码"),
    dest: str = typer.Option("/", help="接收目录下的子路径"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过所有确认(排除建议全部采纳)"),
    full: bool = typer.Option(False, "--full", help="完整预扫描:统计体积并给出节省报告(大目录需几分钟)"),
    max_rounds: int = typer.Option(100, help="最大自动重跑轮数"),
    wait: int = typer.Option(60, help="每轮之间等待秒数"),
) -> None:
    """在旧电脑上运行:扫描、确认排除、配对并开始迁移。"""
    console.print(f"\n[bold cyan]LanMigrate v{__version__} - 发送端[/]")
    _prepare_rclone()
    if full:
        console.print(f"正在完整扫描 {source} …(统计体积,大目录可能需要几分钟)")
    else:
        console.print(f"正在快速扫描 {source} …(仅识别排除目录,--full 可统计体积)")
    last_update = [0.0]
    with console.status("扫描中…") as status:

        def scan_progress(files: int, size: int, rel: str) -> None:
            now = time.monotonic()
            if now - last_update[0] < 0.5:
                return
            last_update[0] = now
            shown = rel if len(rel) <= 50 else "…" + rel[-49:]
            info = f"已扫描 {files} 个文件"
            if full:
                info += f" / {_human(size)}"
            status.update(f"{info}  当前: {shown}")

        report = scanner.scan(source, on_progress=scan_progress, compute_sizes=full)
    if full:
        console.print(f"  共 {report.file_count} 个文件,总计 {_human(report.total_bytes)}")
    else:
        console.print(f"  共发现 {report.file_count} 个文件,{len(report.exclusions)} 个可跳过的依赖目录")

    enabled = _confirm_exclusions(report, assume_yes=yes)
    filter_lines = scanner.build_filter_lines(report, enabled)
    saved = sum(e.size for e in report.exclusions if e.rel in enabled and e.size > 0)
    if full:
        console.print(f"实际需传输约 {_human(report.total_bytes - saved)}")

    host, port, fp = _pick_receiver(host, port)
    if not code:
        remembered = pairing.recall_device(fp) if fp else None
        if remembered:
            code = remembered["code"]
            console.print(f"已配对设备 {remembered['name']},自动使用上次配对码")
        else:
            code = typer.prompt("请输入对方屏幕上的 6 位配对码")
    password = pairing.session_password(code)
    if fp:
        pairing.remember_device(fp, host, code)

    task = taskstore.MigrationTask(
        task_id=taskstore.new_task_id(),
        source=str(Path(source).resolve()),
        host=host,
        port=port,
        user=pairing.SFTP_USER,
        obscured_pass=engine.obscure(password),
        dest=dest,
        device_fp=fp,
        filter_lines=filter_lines,
        saved_bytes=saved,
        total_bytes=report.total_bytes,
    )
    taskstore.save(task)
    console.print(f"任务已创建: {task.task_id}(随时 Ctrl+C 中断,lanmigrate resume 续传)\n")
    _run_task(task, max_rounds, wait)


# ---------------------------------------------------------------- resume


@app.command()
def resume(
    task_id: Optional[str] = typer.Argument(None, help="任务 ID,默认最近一个未完成任务"),
    max_rounds: int = typer.Option(100, help="最大自动重跑轮数"),
    wait: int = typer.Option(60, help="每轮之间等待秒数"),
) -> None:
    """恢复上次未完成的迁移(换网/重启后使用)。"""
    task = taskstore.load(task_id) if task_id else taskstore.latest_incomplete()
    if task is None:
        console.print("没有未完成的任务。")
        raise typer.Exit(0)
    console.print(f"[cyan]恢复任务 {task.task_id}: {task.source} -> {task.host}:{task.port}[/]")

    # IP 可能已变化: 若当初通过 mDNS 配对,按设备指纹重新发现 (PRD F1/F4)
    if task.device_fp:
        for r in discovery.discover(timeout=5.0):
            if r.fingerprint == task.device_fp and (r.host, r.port) != (task.host, task.port):
                console.print(f"  设备 IP 已变化: {task.host} -> {r.host}")
                task.host, task.port = r.host, r.port
                taskstore.save(task)
                break
    _run_task(task, max_rounds, wait)


# ---------------------------------------------------------------- tasks


@app.command()
def tasks() -> None:
    """列出所有迁移任务。"""
    all_tasks = taskstore.all_tasks()
    if not all_tasks:
        console.print("暂无任务。")
        return
    table = Table(title="迁移任务")
    for col in ("任务 ID", "状态", "源目录", "目标", "轮次", "更新时间"):
        table.add_column(col)
    for t in all_tasks:
        table.add_row(t.task_id, t.status, t.source, f"{t.host}:{t.port}",
                      str(t.rounds_completed), t.updated_at)
    console.print(table)


# ---------------------------------------------------------------- ipc (GUI)


@app.command(hidden=True)
def ipc() -> None:
    """(内部) 桌面 GUI 后端:stdin/stdout JSON-lines 协议。"""
    from . import ipc as ipc_mod

    ipc_mod.run_stdio()


if __name__ == "__main__":
    app()

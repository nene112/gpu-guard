---
name: windows-desktop-app-packaging
description: "Use when packaging a Python app into a Windows exe."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [pyinstaller, pyqt5, windows, packaging, exe, verification, msys]
    category: software-development
    related_skills: [computer-use]
---

# Windows 桌面应用打包与跨机验证

把一个 Python 脚本/GUI 变成「拷到别的电脑双击就能跑」的 exe，并且**拿出证据**证明它真的跑起来了。
适用于 PyQt5/PySide2/Tkinter 等任何 GUI 程序；命令行工具同样适用（去掉 `--windowed`）。

## When to Use

- 用户要求「打包为 exe / 单文件 / 免安装 / 拷到别的电脑能跑」，针对的是 Python 脚本或 GUI 程序。
- 需要验证一个已经存在的 exe / PyInstaller 产物到底能不能在别的机器跑（报错、闪退、白窗口、缺 DLL）。
- 需要在 Windows 上用真实截图核验自己的 GUI 窗口渲染（文字截断、重叠、留白）。
- 改完桌面应用后要重新出包并重新验证。

不适用：只要在源码层面调试就不需要打包；Web 应用用 browser 工具截屏即可（见 `dogfood`）。

## 0. 交付形态（先定，别边做边改）

- 交付物是 `dist/<Name>.exe` **加一个可重跑的 `build_exe.py`**，不是一串手敲的 pyinstaller 命令。
- `build/`、`*.spec`、构建 venv 一律写临时目录：`--workpath`/`--specpath` 指到 `%TEMP%`，`--distpath` 指到工程 `dist/`。这样 VCS 工作副本（SVN/git）只多出一个 exe。
- 用户原有脚本**不要改**：新增实现文件，README 追加一节说明用法/重新打包；提交留给用户，不要代提交。

## 1. 选构建解释器

GUI 工具链的 wheel 覆盖 CPython 3.8–3.12；Hermes 自带的解释器往往太新，装不上 PyQt5。
先看有没有现成的 Python：

```bash
py -0p 2>/dev/null || ls ~/AppData/Local/Programs/Python/
```

然后在临时目录建独立 venv 并钉版本：

```bash
PY=/c/Users/<user>/AppData/Local/Programs/Python/Python39/python.exe
"$PY" -m venv /e/tmp/buildenv
/e/tmp/buildenv/Scripts/python.exe -m pip install "PyQt5==5.15.11" "pyinstaller==6.22.3"
```

- `pip --version` 先确认它指向哪个解释器——多 Python 机器上常指向别的版本，装完 `import PyQt5` 仍失败就是这个原因。
- 记录 venv 的**真实落点**：MSYS 下 `$TMPDIR` 可能是 `/tmp` 而映射到 `E:\Temp\houvert`，后续调用一律用解析后的绝对路径，别连续依赖 `$TMPDIR`。
- 32 位解释器打出的 exe 在 64 位系统上加载不了 Qt：构建脚本里 `sys.maxsize <= 2**32` 直接报错退出。

## 2. 写 build_exe.py，而不是手敲 pyinstaller

构建脚本按顺序做四件事（可照抄）：

1. 依赖检查 `__import__('PyQt5.QtWidgets')` / `__import__('PyInstaller')`，缺了就 `pip install`；Bitness 检查。
2. 用 Qt 画一个 `.ico`（`QImage` → `save(path,'ICO')`，`QT_QPA_PLATFORM=offscreen`）；失败就跳过，图标是装饰。
3. 写 `version.txt` 版本资源（`VSVersionInfo` + `FixedFileInfo` + `StringFileInfo`），否则 exe 属性面板里名字/版本是空的，分发给别人看着像来路不明的程序。
4. 调 PyInstaller：

```
python -m PyInstaller --noconfirm --clean --onefile --windowed --name <Name> \
  --distpath <project>/dist --workpath <tmp>/build --specpath <tmp> \
  --version-file <tmp>/version.txt --icon <tmp>/icon.ico \
  --exclude-module tkinter --exclude-module pytest --exclude-module unittest \
  <entry>.py
```

打包完打印产物大小 + 自检命令；中间目录 `shutil.rmtree`。

**windowed 构建必须配一个 `--dump` 自检分支：** `--windowed` 下 `sys.stdout` 可能是 `None`，`print` 会抛异常。所以自检分支要「print 包在 try 里 + 结果同时落盘」（默认 `%TEMP%/<App>-dump.txt`，支持 `--out <path>`），否则你在目标机上根本拿不到输出。这个分支也是排查目标机环境的第一入口。

onefile 的代价写进 README：运行时解包到 `%TEMP%`，首启 1~3 秒；目标机 `%TEMP%` 只读、或有白名单杀软时会被拦，此时把 `--onefile` 换成 `--onedir` 整目录分发。

## 3. 验证顺序（每步都要真实输出，不靠推断）

**a) 隔离 + 剥离 Python 的 PATH，跑自检**（证明目标机不需要 Python）：

```bash
cp dist/<Name>.exe /e/tmp/iso/ && cd /e/tmp/iso
env -u PYTHONHOME -u PYTHONPATH PATH="/c/Windows/system32:/c/Windows" \
  ./<Name>.exe --dump --out "E:/tmp/iso/out.txt"
cat out.txt
```

**b) 确认平台插件与运行库真的进包了**：

```bash
<venv>/Scripts/pyi-archive_viewer.exe -l dist/<Name>.exe | grep -iE "qwindows|msvcp|vcruntime|platforms"
```

看到 `plugins\\platforms\\qwindows.dll` 和 `MSVCP140/VCRUNTIME140.dll` 才算「目标机不需要 VC 运行库」。

**c) 启 GUI，确认真的有窗口**：

```powershell
Get-Process <Name> | Select-Object Id,MainWindowTitle,MainWindowHandle
```

onefile 会出**两个 PID**（bootloader 父进程 + 应用子进程），只有 `MainWindowHandle≠0` 的那个有窗口——别看到两个进程就以为启动了两次。

**d) 抓窗口截图，用 vision 核验布局**（文字截断、重叠、大片留白、N/A 渲染）：

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/capture-window.ps1 \
  -TitleLike "<Name>" -Out "E:/tmp/iso/ui.png"
```

然后 `vision_analyze` 看这张图。

**e) 布局/交互改动后要重打包再截图**——源码测试通过 ≠ 打包件渲染正确；反过来说，只有在 d) 看过图之后才说「打包完成」。

多实例/多卡这类「随数据变长」的界面，用**伪造数据渲染真实窗口**再截图是合法且高效的布局验证（数据是假的，渲染路径是真的），比在真实机器上凑硬件快得多；报告时说明数据是构造的。

## 4. MSYS/git-bash 踩坑（Windows 上必踩）

- `taskkill //F //IM x.exe` 报 `无效参数/选项 - '//F'`：MSYS 会改写斜杠参数。杀进程用
  `powershell -NoProfile -Command "Get-Process x -ErrorAction SilentlyContinue | Stop-Process -Force"`。
  `cmd //c start "" app.exe` 同样不可靠，会掉进交互 shell。
- **给原生程序传路径一律用 `E:/Temp/...` 正斜杠原生路径**。`/e/Temp/...` 会被原生程序当成「当前盘根下的 \e\Temp」，`--out` 文件静默消失就是这么来的（工具还返回 exit 0）。
- 后台跑 GUI：用 `terminal(background=true)`，不要在前台命令里写 `&`（会被拒）。
- PowerShell 的 `Add-Type -MemberDefinition` **不能声明 struct**；要 `RECT`/`EnumWindows`/`PrintWindow` 这类 P/Invoke，必须 `Add-Type -TypeDefinition` 贴完整 C#，否则得到 `InvokeMethodOnNull`。
- 屏幕区域截屏会拍到**上层窗口**：`SetForegroundWindow` 常静默失败（前台锁），你以为在截自己的窗口，实际截到的是压在上面的应用。抓特定窗口只用 `PrintWindow(hwnd, hdc, 2)`（PW_RENDERFULLCONTENT，被遮挡也能渲染）。

## 5. Qt 界面自身的硬规则

- **采集在 QThread，界面只在主线程改**：worker 用 `pyqtSignal(dict)` 回传快照，主线程刷新控件。子线程直接 `setText` 在 PyQt5 下会随机崩。
- **自适应高度只能在窗口显示之后测量**：`chrome = height() - scroll.viewport().height()` 在 `show()` 前 viewport 高度是 0 → 算出整屏高、窗口撑满全屏。把目标尺寸存成 `_pending_fit`，在 `showEvent` 和下一次刷新时重试。
- **数据字段要能降级**：`[N/A]` 归一成 `None` → UI 显示 `N/A` 而不是崩；缺字段就少显示一段，不要整块报错。

细节（含 nvidia-smi 多卡字段查询/降级阶梯、卡片增删不重建、进度条配色）见 `references/qt-live-monitor.md`。

## 6. 收尾

- 清掉工程目录里的 `__pycache__`（源码测试会生成）。
- 报告写清：产物路径+大小、验证过什么（含真实数字）、目标机前提（驱动/VC 运行库/Windows 版本）、已知限制（首启慢、防病毒、`--onedir` 备选）。
- 交付用 `MEDIA:<绝对路径>` 给 exe 和截图，别只贴路径。

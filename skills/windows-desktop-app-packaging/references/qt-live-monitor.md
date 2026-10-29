# Qt 实时监控窗口（PyQt5）

一个「每秒刷新、多实例卡片、窗口自适应」的监控界面配方。以 NVIDIA 多卡监控为例，模式可直接搬到温度/队列/服务健康度等任何轮询面板。

## 采集线程

```python
class CollectorThread(QThread):
    snapshot_ready = pyqtSignal(dict)      # dict 快照，主线程负责渲染
```

- 循环里 `collect()` 一次 → `emit` → 用 50ms 小片睡眠等待到下一个周期（`while waited < interval and self._running: time.sleep(0.05)`），这样 `stop()` 后 100ms 内退出。
- `except Exception` 也要 `emit` 一个带 `error` 字段的快照：线程静默死掉比崩溃更难查。
- `closeEvent`：`stop()` + `wait(3000)`。
- 刷新间隔给下拉框（0.5/1/2/5 秒），不要 0.05 秒起一个子进程——每轮 spawn 一次 `nvidia-smi` 已经够贵了。

## 卡片增删不重建

按 `index` 建 `dict[str, Card]`；快照来了逐卡 `update_data()`，缺的卡 `setParent(None)` + `deleteLater()`。整块重建会闪，而且会丢滚动位置。

## 自适应高度（易错）

```python
viewport_h = self.scroll.viewport().height()
if not self.isVisible() or viewport_h <= 0:
    self._pending_fit = (n, m)          # show() 前量不到 chrome
    return
target = self.container.sizeHint().height() + (self.height() - viewport_h) + 16
```

- `show()` 之前 viewport 高度是 0，`height()-viewport` 会等于整个窗口高 → 窗口被撑到屏幕高度。必须在 `showEvent` 里（或下一次刷新）重试，量到才置 `_autofitted=True`。
- `+16` 的余量是必须的：`setText` 之后 QLabel 的 sizeHint 还没更新，按精确值算底部会被切一行。
- 最后 `min(target, screen.availableGeometry().height() - 80)`，多卡机器上别超过屏幕。

## 视觉细节

- 数值标签在右侧时 `bar.setTextVisible(False)`，否则进度条里还有一份重复的百分比。
- 阈值配色：`>=90` 红 `#F0553C`、`>=70` 黄 `#E3A008`、其余绿 `#3FB950`，通过每个 bar 的 `QProgressBar::chunk` 样式设。
- 温度同样按阈值染色，用 `QLabel.setStyleSheet` 直接改 color。
- 深色底 `#2A2A2A` + 卡片 `#333333` + 圆角 10px，全局 `STYLE_SHEET` 一次设好。

## 数据源：Windows 上的 nvidia-smi

定位：`shutil.which('nvidia-smi')` → `%SystemRoot%\System32\nvidia-smi.exe` → `%ProgramW6432%\NVIDIA Corporation\NVSMI\nvidia-smi.exe`。
调用时带 `creationflags=CREATE_NO_WINDOW`（否则弹黑框）和 `timeout`（驱动挂住不返回时会卡死界面）。

字段查询按「全量 → 中量 → 基础」降级（老驱动遇到不认识的字段会非零退出）：

```
index,name,driver_version,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,fan.speed,power.draw,power.limit,clocks.sm
index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw
index,name,utilization.gpu,memory.used,memory.total,temperature.gpu
```

全部失败再退回整块 `nvidia-smi` 文本显示（等同于 `watch` 行为）——这是最后的兜底，不是主路径。

坑：

- `--format=csv,noheader,nounits` 里缺失值是字面量 `[N/A]`（也可能 `N/A`）：统一归一成 `None`，UI 显示 `N/A`。
- 笔记本 GPU 的 `fan.speed`、很多卡的 `power.limit` 恒为 `[N/A]`，别当成解析失败。
- `--query-compute-apps=pid,process_name,used_gpu_memory` 在部分驱动上会把**所有图形程序**都返回、且显存列为 `[N/A]`（几十行噪音）。只保留显存是数字的行、按显存降序、截断 20 条，全空就隐藏整个进程区。
- 输出按控制台代码页解码：`locale.getpreferredencoding(False)` → `utf-8` → `gbk` → `replace`。中文 Windows 是 GBK，直接 `bytes.decode()` 会炸。
- 没有 N 卡/没装驱动的机器：`nvidia-smi` 不存在，要显示明确中文提示，不要空白窗口。

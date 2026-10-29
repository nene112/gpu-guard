# -*- coding: utf-8 -*-
"""CudaSupervisor —— Windows 下多 GPU 实时监控（PyQt5）

相对初版的改动：
  * 支持多 GPU：逐个 GPU 卡片展示，而不是整块 nvidia-smi 文本
  * 解析 nvidia-smi 的 CSV 查询结果，驱动版本差异时自动降级字段
  * 采集在后台线程完成，界面由主线程刷新（Qt 不允许子线程直接改控件）
  * 输出按系统编码解码，兼容中文 Windows 的 GBK 代码页
  * 找不到 nvidia-smi 时给出明确提示，而不是空白窗口

用法：
    python cuda_supervisor.py            # 启动界面
    python cuda_supervisor.py --dump     # 只打印一次采集结果（自检/排障）
"""
from __future__ import print_function

import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QMainWindow, QProgressBar, QScrollArea, QVBoxLayout, QWidget,
)

APP_NAME = 'CudaSupervisor'
APP_VERSION = '1.0.0'
REFRESH_CHOICES = [('0.5 秒', 500), ('1 秒', 1000), ('2 秒', 2000), ('5 秒', 5000)]

# 采集字段：优先全量，驱动不认某个字段时逐级降级
FIELDS_FULL = ('index,name,driver_version,utilization.gpu,utilization.memory,'
               'memory.used,memory.total,temperature.gpu,fan.speed,power.draw,'
               'power.limit,clocks.sm')
FIELDS_MID = ('index,name,utilization.gpu,memory.used,memory.total,'
              'temperature.gpu,power.draw')
FIELDS_BASIC = 'index,name,utilization.gpu,memory.used,memory.total,temperature.gpu'

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


# ---------------------------------------------------------------- 采集层
def _decode(raw):
    """nvidia-smi 输出按控制台代码页解码；中文 Windows 常见为 GBK。"""
    if not raw:
        return ''
    encodings = [locale.getpreferredencoding(False) or 'utf-8', 'utf-8', 'gbk', 'latin-1']
    tried = set()
    for enc in encodings:
        if enc.lower() in tried:
            continue
        tried.add(enc.lower())
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', 'replace')


def find_nvidia_smi():
    """nvidia-smi 通常随驱动装在 System32，部分老驱动在 NVSMI 目录。"""
    found = shutil.which('nvidia-smi')
    if found:
        return found
    roots = {os.environ.get('SystemRoot'), os.environ.get('windir'), r'C:\Windows'}
    candidates = [os.path.join(r, 'System32', 'nvidia-smi.exe') for r in roots if r]
    for prefix_key in ('ProgramW6432', 'ProgramFiles'):
        prefix = os.environ.get(prefix_key)
        if prefix:
            candidates.append(os.path.join(prefix, 'NVIDIA Corporation', 'NVSMI', 'nvidia-smi.exe'))
    candidates.append(r'C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe')
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def run_smi(exe, args, timeout=10):
    """返回 (stdout 文本, 错误文本)；两者只会有一个非空。"""
    try:
        proc = subprocess.run([exe] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW,
                              timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 进程起不来、超时都当采集失败
        return None, '%s: %s' % (type(exc).__name__, exc)
    if proc.returncode != 0:
        err = _decode(proc.stderr).strip() or _decode(proc.stdout).strip()
        return None, err or 'nvidia-smi 退出码 %d' % proc.returncode
    return _decode(proc.stdout), ''


def _to_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in ('N/A', '[N/A]', 'NA', 'UNKNOWN'):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_rows(text, fields):
    """把 noheader,nounits 的 CSV 输出解析成 dict 列表。"""
    rows = []
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < len(fields):
            parts += [''] * (len(fields) - len(parts))
        rows.append(dict(zip(fields, parts[:len(fields)])))
    return rows


def _normalize_gpu(row, used_fields):
    gpu = {
        'index': row.get('index', '?'),
        'name': row.get('name', 'Unknown GPU'),
        'driver': row.get('driver_version') or None,
        'util': _to_float(row.get('utilization.gpu')),
        'mem_util': _to_float(row.get('utilization.memory')),
        'mem_used': _to_float(row.get('memory.used')),
        'mem_total': _to_float(row.get('memory.total')),
        'temp': _to_float(row.get('temperature.gpu')),
        'fan': _to_float(row.get('fan.speed')),
        'power': _to_float(row.get('power.draw')),
        'power_limit': _to_float(row.get('power.limit')),
        'clock': _to_float(row.get('clocks.sm')),
        'fields': used_fields,
    }
    return gpu


def collect_snapshot():
    """一次完整采集，返回 dict（供界面线程使用，也供 --dump 自检）。"""
    snapshot = {
        'ok': False, 'error': '', 'gpus': [], 'procs': [], 'raw': '',
        'smi': '', 'driver': '', 'ts': time.time(),
    }
    exe = find_nvidia_smi()
    if not exe:
        snapshot['error'] = ('未找到 nvidia-smi。请先安装 NVIDIA 显卡驱动'
                             '（GeForce/Radeon 之外的机器不支持该工具）。')
        return snapshot
    snapshot['smi'] = exe

    gpus = []
    last_err = ''
    for fields in (FIELDS_FULL, FIELDS_MID, FIELDS_BASIC):
        text, err = run_smi(exe, ['--query-gpu=' + fields, '--format=csv,noheader,nounits'])
        if text is None:
            last_err = err
            continue
        used = fields.split(',')
        gpus = [_normalize_gpu(row, used) for row in parse_rows(text, used)]
        break

    if not gpus:
        # 驱动过老：退回整块文本显示（等同初版行为）
        raw, err2 = run_smi(exe, [])
        if raw:
            snapshot['raw'] = raw.strip()
            snapshot['error'] = '无法按字段解析 nvidia-smi 输出，已退回原始文本模式。'
        else:
            snapshot['error'] = last_err or err2 or 'nvidia-smi 采集失败。'
        return snapshot

    drivers = {g['driver'] for g in gpus if g['driver']}
    snapshot['driver'] = '/'.join(sorted(drivers))
    snapshot['gpus'] = gpus

    procs = []
    for field in ('used_gpu_memory', 'used_memory'):
        text, _ = run_smi(exe, ['--query-compute-apps=pid,process_name,' + field,
                                '--format=csv,noheader,nounits'])
        if text is not None:
            # 该查询在部分驱动上会把所有图形程序一起返回，且显存为 [N/A]。
            # 只保留真正能读到显存的进程，按显存降序，最多 20 条。
            rows = []
            for row in parse_rows(text, ['pid', 'process_name', field]):
                pid = (row.get('pid') or '').strip()
                mem = _to_float(row.get(field))
                if not pid or mem is None:
                    continue
                name = (row.get('process_name') or '').strip()
                rows.append({'pid': pid, 'name': os.path.basename(name) or name, 'mem': mem})
            rows.sort(key=lambda item: item['mem'], reverse=True)
            procs = rows[:20]
            break
    snapshot['procs'] = procs
    snapshot['ok'] = True
    return snapshot


# ---------------------------------------------------------------- 界面层
STYLE_SHEET = """
QWidget { background-color: #2A2A2A; color: #ECECEC;
          font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 13px; }
QFrame#card { background-color: #333333; border: 1px solid #3F3F3F; border-radius: 10px; }
QLabel#gpuTitle { font-size: 15px; font-weight: 600; color: #FFFFFF; }
QLabel#metric  { color: #B9B9B9; font-size: 12px; }
QLabel#value   { color: #FFFFFF; font-size: 12px; font-weight: 600; }
QLabel#headline { font-size: 16px; font-weight: 600; color: #FFFFFF; }
QLabel#hint    { color: #8C8C8C; font-size: 12px; }
QLabel#error   { color: #FF7B72; font-size: 13px; }
QProgressBar { background-color: #262626; border: none; border-radius: 6px;
               min-height: 12px; max-height: 12px; text-align: center;
               color: #DADADA; font-size: 10px; }
QComboBox { background-color: #3A3A3A; border: 1px solid #4A4A4A; border-radius: 6px;
            padding: 3px 8px; min-width: 74px; }
QComboBox QAbstractItemView { background-color: #3A3A3A; selection-background-color: #4C6EF5; }
QCheckBox { color: #B9B9B9; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #2A2A2A; width: 10px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #4A4A4A; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; }
"""


def _bar_color(percent):
    if percent is None:
        return '#555555'
    if percent >= 90:
        return '#F0553C'
    if percent >= 70:
        return '#E3A008'
    return '#3FB950'


class GpuCard(QFrame):
    """单个 GPU 的指标卡片。"""

    def __init__(self, gpu, parent=None):
        super().__init__(parent)
        self.setObjectName('card')
        self.setFrameShape(QFrame.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        head = QHBoxLayout()
        self.title = QLabel(self)
        self.title.setObjectName('gpuTitle')
        self.temp = QLabel(self)
        self.temp.setObjectName('value')
        self.temp.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.title, 1)
        head.addWidget(self.temp, 0)
        layout.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        self.util_label = QLabel('负载', self)
        self.util_label.setObjectName('metric')
        self.util_bar = QProgressBar(self)
        self.util_bar.setTextVisible(False)
        self.util_text = QLabel('-', self)
        self.util_text.setObjectName('value')

        self.mem_label = QLabel('显存', self)
        self.mem_label.setObjectName('metric')
        self.mem_bar = QProgressBar(self)
        self.mem_bar.setTextVisible(False)
        self.mem_text = QLabel('-', self)
        self.mem_text.setObjectName('value')

        grid.addWidget(self.util_label, 0, 0)
        grid.addWidget(self.util_bar, 0, 1)
        grid.addWidget(self.util_text, 0, 2)
        grid.addWidget(self.mem_label, 1, 0)
        grid.addWidget(self.mem_bar, 1, 1)
        grid.addWidget(self.mem_text, 1, 2)
        layout.addLayout(grid)

        self.detail = QLabel(self)
        self.detail.setObjectName('metric')
        self.detail.setTextFormat(Qt.PlainText)
        layout.addWidget(self.detail)

        self.update_data(gpu)

    def update_data(self, gpu):
        title = 'GPU %s  ·  %s' % (gpu.get('index', '?'), gpu.get('name', 'Unknown GPU'))
        if gpu.get('driver'):
            title += '   (驱动 %s)' % gpu['driver']
        self.title.setText(title)

        temp = gpu.get('temp')
        self.temp.setText('%d °C' % temp if temp is not None else '温度 N/A')
        self.temp.setStyleSheet('color: %s; font-size: 14px; font-weight: 600' %
                                ('#F0553C' if temp is not None and temp >= 85 else
                                 '#E3A008' if temp is not None and temp >= 75 else '#FFFFFF'))

        util = gpu.get('util')
        if util is None:
            self.util_bar.setRange(0, 100)
            self.util_bar.setValue(0)
            self.util_text.setText('N/A')
        else:
            self.util_bar.setRange(0, 100)
            self.util_bar.setValue(int(round(util)))
            self.util_text.setText('%d %%' % round(util))
        self._style_bar(self.util_bar, util)

        used, total = gpu.get('mem_used'), gpu.get('mem_total')
        if used is None or not total:
            self.mem_bar.setRange(0, 100)
            self.mem_bar.setValue(0)
            self.mem_text.setText('N/A')
            self._style_bar(self.mem_bar, None)
        else:
            percent = used / total * 100.0 if total else 0.0
            self.mem_bar.setRange(0, 100)
            self.mem_bar.setValue(int(round(percent)))
            self.mem_text.setText('%d / %d MiB' % (round(used), round(total)))
            self._style_bar(self.mem_bar, percent)

        bits = []
        if gpu.get('power') is not None:
            limit = gpu.get('power_limit')
            bits.append('功耗 %.1f W%s' % (gpu['power'],
                                           ' / %.0f W' % limit if limit else ''))
        if gpu.get('fan') is not None:
            bits.append('风扇 %.0f %%' % gpu['fan'])
        if gpu.get('clock') is not None:
            bits.append('核心 %d MHz' % round(gpu['clock']))
        if gpu.get('mem_util') is not None:
            bits.append('显存带宽 %d %%' % round(gpu['mem_util']))
        self.detail.setText('    '.join(bits) if bits else '该驱动的 nvidia-smi 未提供功耗/风扇/频率字段')

    @staticmethod
    def _style_bar(bar, percent):
        color = _bar_color(percent)
        bar.setStyleSheet(
            'QProgressBar { background-color: #262626; border: none; border-radius: 6px; '
            'min-height: 12px; max-height: 12px; text-align: center; color: #DADADA; '
            'font-size: 10px; }'
            'QProgressBar::chunk { background-color: %s; border-radius: 6px; }' % color)


class CollectorThread(QThread):
    """后台采集线程：避免 nvidia-smi 子进程阻塞界面。"""

    snapshot_ready = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self.interval_ms = 1000
        self.force_now = False

    def set_interval(self, ms):
        self.interval_ms = max(200, int(ms))

    def run(self):
        while self._running:
            started = time.time()
            try:
                self.snapshot_ready.emit(collect_snapshot())
            except Exception as exc:  # noqa: BLE001 - 线程不能死
                self.snapshot_ready.emit({
                    'ok': False, 'error': '采集异常：%s' % exc, 'gpus': [], 'procs': [],
                    'raw': '', 'smi': '', 'driver': '', 'ts': time.time(),
                })
            wait = self.interval_ms / 1000.0 - (time.time() - started)
            waited = 0.0
            self.force_now = False
            while self._running and waited < max(wait, 0.1):
                time.sleep(0.05)
                waited += 0.05
                if self.force_now:
                    break
            if not self._running:
                break

    def stop(self):
        self._running = False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('%s v%s' % (APP_NAME, APP_VERSION))
        self.resize(920, 780)
        self.setStyleSheet(STYLE_SHEET)
        self.cards = {}
        self.last_gpu_count = -1
        self._autofitted = False
        self._pending_fit = None

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        self.headline = QLabel('GPU 监控', central)
        self.headline.setObjectName('headline')
        top.addWidget(self.headline, 1)

        self.interval_box = QComboBox(central)
        for text, ms in REFRESH_CHOICES:
            self.interval_box.addItem(text, ms)
        self.interval_box.setCurrentIndex(1)
        self.interval_box.currentIndexChanged.connect(self.on_interval_changed)
        top.addWidget(QLabel('刷新', central))
        top.addWidget(self.interval_box)

        self.pin = QCheckBox('始终置顶', central)
        self.pin.stateChanged.connect(self.on_pin_changed)
        top.addWidget(self.pin)
        root.addLayout(top)

        self.status = QLabel('正在采集…', central)
        self.status.setObjectName('hint')
        root.addWidget(self.status)

        self.scroll = QScrollArea(central)
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.cards_layout = QVBoxLayout(self.container)
        self.cards_layout.setContentsMargins(0, 0, 6, 0)
        self.cards_layout.setSpacing(10)
        self.cards_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)

        self.raw_view = QLabel(central)
        self.raw_view.setObjectName('metric')
        self.raw_view.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.raw_view.setTextFormat(Qt.PlainText)
        self.raw_view.setWordWrap(False)
        self.raw_view.hide()
        root.addWidget(self.raw_view, 1)

        self.proc_title = QLabel('GPU 进程', central)
        self.proc_title.setObjectName('headline')
        self.proc_title.hide()
        root.addWidget(self.proc_title)

        self.proc_view = QLabel(central)
        self.proc_view.setObjectName('metric')
        self.proc_view.setTextFormat(Qt.PlainText)
        self.proc_view.setWordWrap(False)
        self.proc_view.hide()
        root.addWidget(self.proc_view)

        self.setCentralWidget(central)

        self.thread = CollectorThread(self)
        self.thread.snapshot_ready.connect(self.on_snapshot)
        self.thread.start()
        self._dump_lines = []

    # ------------------------------------------------------------ 槽函数
    def on_interval_changed(self):
        self.thread.set_interval(self.interval_box.currentData())

    def on_pin_changed(self, state):
        flags = self.windowFlags()
        if state == Qt.Checked:
            self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowStaysOnTopHint)
        self.show()

    def on_snapshot(self, snap):
        stamp = time.strftime('%H:%M:%S', time.localtime(snap.get('ts', time.time())))
        gpus = snap.get('gpus') or []

        if not snap.get('ok') and not gpus:
            self.headline.setText('GPU 监控 — 不可用')
            self.status.setText(snap.get('error') or '采集失败')
            self.status.setObjectName('error')
            self.status.setStyleSheet('color: #FF7B72')
            if snap.get('raw'):
                self.raw_view.setText(snap['raw'])
                self.raw_view.show()
            else:
                self.raw_view.hide()
            self._clear_cards()
            self.proc_title.hide()
            self.proc_view.hide()
            return

        self.status.setStyleSheet('')
        self.status.setObjectName('hint')
        self.raw_view.hide()
        self.headline.setText('GPU 监控 · %d 张显卡' % len(gpus))
        driver = snap.get('driver')
        self.status.setText('更新于 %s    nvidia-smi: %s%s' %
                            (stamp, snap.get('smi', ''),
                             '    驱动 %s' % driver if driver else ''))

        seen = set()
        for gpu in gpus:
            key = str(gpu.get('index'))
            seen.add(key)
            card = self.cards.get(key)
            if card is None:
                card = GpuCard(gpu, self.container)
                self.cards[key] = card
                self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            else:
                card.update_data(gpu)
        for key in list(self.cards):
            if key not in seen:
                card = self.cards.pop(key)
                card.setParent(None)
                card.deleteLater()

        procs = snap.get('procs') or []
        if procs:
            lines = ['%-8s %10s   %s' % ('PID', '显存(MiB)', '进程')]
            for row in procs:
                lines.append('%-8s %10.0f   %s' % (row.get('pid', ''), row.get('mem', 0),
                                                   row.get('name', '')))
            self.proc_view.setText('\n'.join(lines))
            self.proc_title.show()
            self.proc_view.show()
        else:
            self.proc_title.hide()
            self.proc_view.hide()

        self._autofit(len(gpus), len(procs))

    def _autofit(self, gpu_count, proc_count):
        """首次拿到数据后按实际内容高度调整窗口，避免单卡时大片留白。"""
        if self._autofitted:
            return
        self._pending_fit = (gpu_count, proc_count)
        viewport_h = self.scroll.viewport().height()
        if not self.isVisible() or viewport_h <= 0:
            # 窗口还没显示出来时量不到 chrome，等 showEvent 或下一次刷新再算
            return
        target = None
        try:
            self.cards_layout.activate()
            self.centralWidget().layout().activate()
            content = self.container.sizeHint().height()
            chrome = self.height() - viewport_h
            if content > 0 and 0 < chrome < self.height():
                target = content + chrome + 16
        except Exception:  # noqa: BLE001 - 量不到就用估算值
            target = None
        if not target:
            target = 150 + 140 * max(gpu_count, 1)
            if proc_count:
                target += 30 + 17 * min(proc_count, 20)
        screen = QApplication.primaryScreen()
        if screen is not None:
            target = min(target, screen.availableGeometry().height() - 80)
        self._autofitted = True
        self.resize(self.width(), max(target, 240))

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending_fit and not self._autofitted:
            self._autofit(*self._pending_fit)

    def _clear_cards(self):
        for key in list(self.cards):
            card = self.cards.pop(key)
            card.setParent(None)
            card.deleteLater()

    def closeEvent(self, event):
        self.thread.stop()
        self.thread.wait(3000)
        super().closeEvent(event)


def _render_dump(snap):
    lines = ['ok      : %s' % snap['ok'],
             'smi     : %s' % snap['smi'],
             'driver  : %s' % snap['driver'],
             'error   : %s' % snap['error'],
             'gpus    : %d' % len(snap['gpus'])]
    for gpu in snap['gpus']:
        lines.append('  - #%s %s util=%s%% mem=%s/%s MiB temp=%sC fan=%s%% power=%sW' % (
            gpu['index'], gpu['name'], gpu['util'], gpu['mem_used'],
            gpu['mem_total'], gpu['temp'], gpu['fan'], gpu['power']))
    lines.append('procs   : %d' % len(snap['procs']))
    for row in snap['procs']:
        lines.append('  - pid=%s mem=%sMiB %s' % (row['pid'], row['mem'], row['name']))
    return '\n'.join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if '--dump' in argv:
        # 自检模式：不开窗口，把一次采集结果输出。打包版是 --windowed，
        # 没有控制台时 stdout 为 None，所以结果同时落盘到文件。
        text = _render_dump(collect_snapshot())
        try:
            print(text)
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - 无控制台的窗口程序打不出来，正常
            pass
        target = None
        if '--out' in argv:
            pos = argv.index('--out')
            if pos + 1 < len(argv):
                target = argv[pos + 1]
        if not target:
            target = os.path.join(tempfile.gettempdir(), APP_NAME + '-dump.txt')
        try:
            with open(target, 'w', encoding='utf-8') as handle:
                handle.write(text + '\n')
        except Exception:  # noqa: BLE001
            pass
        return 0

    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
        except Exception:  # noqa: BLE001 - 纯外观，失败无所谓
            pass

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())

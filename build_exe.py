# -*- coding: utf-8 -*-
"""把 cuda_supervisor.py 打包成单文件 exe（免安装 Python，可拷到别的电脑直接跑）。

    python build_exe.py

产物：dist\\CudaSupervisor.exe
依赖：本机 Windows x64 + Python 3.8~3.12 + 联网（首次会自动 pip 安装 PyQt5 / pyinstaller）
中间文件写到系统临时目录，不污染工程目录。
"""
from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, 'cuda_supervisor.py')
NAME = 'CudaSupervisor'
DIST = os.path.join(HERE, 'dist')
NEEDED = [('PyQt5', 'PyQt5.QtWidgets'), ('PyInstaller', 'PyInstaller')]


def ensure_deps():
    missing = []
    for pip_name, module in NEEDED:
        try:
            __import__(module)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print('[1/3] 安装依赖：%s' % ' '.join(missing))
        cmd = [sys.executable, '-m', 'pip', 'install'] + missing
        if subprocess.call(cmd) != 0:
            sys.exit('依赖安装失败，请手动执行：' + ' '.join(cmd))
    else:
        print('[1/3] 依赖已就绪（PyQt5 / PyInstaller）')

    if sys.version_info < (3, 8):
        sys.exit('需要 Python 3.8 及以上（当前 %s）' % sys.version.split()[0])
    if sys.maxsize <= 2 ** 32:
        sys.exit('检测到 32 位 Python：请用 64 位 Python 打包，否则无法在 64 位系统上正常加载 Qt。')


def make_icon(path):
    """用 Qt 画一个简易图标；失败就跳过（没有图标也能正常运行）。"""
    try:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt5.QtCore import QRectF, Qt
        from PyQt5.QtGui import QColor, QFont, QImage, QPainter
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])  # noqa: F841
        size = 256
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor('#2A2A2A'))
        painter.setPen(QColor('#4C6EF5'))
        painter.drawRoundedRect(QRectF(8, 8, size - 16, size - 16), 40, 40)
        painter.setPen(QColor('#3FB950'))
        painter.setBrush(QColor('#3FB950'))
        painter.drawRoundedRect(QRectF(52, 150, 152, 26), 13, 13)
        painter.setBrush(QColor('#E3A008'))
        painter.drawRoundedRect(QRectF(52, 186, 104, 26), 13, 13)
        painter.setPen(QColor('#FFFFFF'))
        font = QFont('Segoe UI', 74, QFont.Bold)
        painter.setFont(font)
        painter.drawText(QRectF(0, 34, size, 100), Qt.AlignCenter, 'GPU')
        painter.end()
        image.save(path, 'ICO')
        return True
    except Exception as exc:  # noqa: BLE001 - 图标是可选装饰
        print('    图标生成失败（忽略）：%s' % exc)
        return False


def write_version_file(path):
    """给 exe 写入版本信息（属性面板里能看到名字/版本，不像来路不明的程序）。"""
    content = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=(1, 0, 0, 0), prodvers=(1, 0, 0, 0), mask=0x3f,
                    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'win-watch'),
        StringStruct('FileDescription', 'NVIDIA GPU Monitor'),
        StringStruct('FileVersion', '1.0.0'),
        StringStruct('InternalName', 'CudaSupervisor'),
        StringStruct('OriginalFilename', 'CudaSupervisor.exe'),
        StringStruct('ProductName', 'CudaSupervisor'),
        StringStruct('ProductVersion', '1.0.0')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(content)


def build():
    work = tempfile.mkdtemp(prefix='cs-pyinstaller-')
    icon = os.path.join(work, 'icon.ico')
    version_file = os.path.join(work, 'version.txt')
    has_icon = make_icon(icon)
    write_version_file(version_file)

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', NAME,
        '--distpath', DIST,
        '--workpath', os.path.join(work, 'build'),
        '--specpath', work,
        '--version-file', version_file,
        '--exclude-module', 'tkinter',
        '--exclude-module', 'pytest',
        '--exclude-module', 'unittest',
        '--exclude-module', 'pydoc_data',
    ]
    if has_icon:
        cmd += ['--icon', icon]
    cmd.append(ENTRY)

    print('[2/3] 开始打包（约 1~2 分钟）…')
    print('    ' + ' '.join(cmd))
    code = subprocess.call(cmd)
    shutil.rmtree(work, ignore_errors=True)
    if code != 0:
        sys.exit('PyInstaller 打包失败，退出码 %d' % code)

    exe = os.path.join(DIST, NAME + '.exe')
    if not os.path.isfile(exe):
        sys.exit('未找到产物：' + exe)
    size_mb = os.path.getsize(exe) / 1024.0 / 1024.0
    print('[3/3] 完成：%s（%.1f MB）' % (exe, size_mb))
    print('    自检：%s --dump' % exe)


if __name__ == '__main__':
    ensure_deps()
    build()

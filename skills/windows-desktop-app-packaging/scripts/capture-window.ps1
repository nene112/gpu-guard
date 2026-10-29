# 截取指定窗口（被遮挡也能截）——用 EnumWindows 找 hwnd + PrintWindow 渲染。
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File capture-window.ps1 `
#       -TitleLike "CudaSupervisor" -Out "E:/tmp/iso/ui.png"
#
# 为什么不用屏幕截屏：SetForegroundWindow 常因前台锁静默失败，CopyFromScreen 会截到压在上面
# 的窗口。PrintWindow(hwnd, hdc, 2)（PW_RENDERFULLCONTENT）不受遮挡影响。
param(
    [string]$TitleLike = '',
    [string]$ProcessName = '',
    [string]$Out = "$env:TEMP\window.png",
    [int]$MinWidth = 200
)
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public struct RECT { public int Left, Top, Right, Bottom; }
public class WinShot {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  public static IntPtr FindByTitle(string needle, int minWidth) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      if (!IsWindowVisible(h)) return true;
      StringBuilder sb = new StringBuilder(512);
      GetWindowTextW(h, sb, 512);
      if (sb.ToString().IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0) {
        RECT r; GetWindowRect(h, out r);
        if ((r.Right - r.Left) > minWidth) { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
'@

$h = [IntPtr]::Zero
if ($ProcessName) {
    # onefile 会有两个 PID（bootloader + 应用），只有 MainWindowHandle != 0 的那个有窗口
    $p = @(Get-Process $ProcessName -ErrorAction SilentlyContinue) |
         Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($p) { $h = [IntPtr]$p.MainWindowHandle }
}
if ($h -eq [IntPtr]::Zero -and $TitleLike) { $h = [WinShot]::FindByTitle($TitleLike, $MinWidth) }
if ($h -eq [IntPtr]::Zero) { Write-Output 'NO_WINDOW'; exit 2 }

$r = New-Object RECT
[void][WinShot]::GetWindowRect($h, [ref]$r)
$w = $r.Right - $r.Left
$hh = $r.Bottom - $r.Top
$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [WinShot]::PrintWindow($h, $hdc, 2)
$g.ReleaseHdc($hdc)
$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output ("PRINTWINDOW=" + $ok + " SIZE=" + $w + "x" + $hh + " OUT=" + $Out)

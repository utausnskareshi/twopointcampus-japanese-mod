# Capture the primary screen (or the game window) to a PNG for in-game verification.
# usage: powershell -File tools/screenshot.ps1 -Out work/shots/menu.png [-Window] [-MaxWidth 1600]
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [switch]$Window,
    [int]$MaxWidth = 1600
)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class TpcWin {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[TpcWin]::SetProcessDPIAware() | Out-Null

$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
if ($Window) {
    $proc = Get-Process -Name TPC -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc -and $proc.MainWindowHandle -ne 0) {
        [TpcWin]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
        Start-Sleep -Milliseconds 300
        $r = New-Object TpcWin+RECT
        [TpcWin]::GetWindowRect($proc.MainWindowHandle, [ref]$r) | Out-Null
        $bounds = New-Object System.Drawing.Rectangle($r.Left, $r.Top, ($r.Right - $r.Left), ($r.Bottom - $r.Top))
    }
}
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$g.Dispose()
if ($bmp.Width -gt $MaxWidth) {
    $h = [int]($bmp.Height * $MaxWidth / $bmp.Width)
    $small = New-Object System.Drawing.Bitmap($bmp, $MaxWidth, $h)
    $bmp.Dispose()
    $bmp = $small
}
$dir = Split-Path -Parent $Out
if ($dir) { New-Item -ItemType Directory -Force $dir | Out-Null }
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
"saved $Out ($($bounds.Width)x$($bounds.Height) at $($bounds.X),$($bounds.Y))"

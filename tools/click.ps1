# Click (or press a key) in the game window for in-game verification.
# Sends nothing and exits 1 when the game window is missing, does not come to the foreground,
# or the point lies outside it.
# usage: powershell -File tools/click.ps1 -X 275 -Y 772          (screen pixels, DPI-aware)
#        powershell -File tools/click.ps1 -Key ESC
param([int]$X = -1, [int]$Y = -1, [string]$Key = "")
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class TpcInput {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint f, UIntPtr e);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
}
"@
function Fail([string]$message) { [Console]::Error.WriteLine("click.ps1: $message; nothing sent"); exit 1 }
[TpcInput]::SetProcessDPIAware() | Out-Null
$vk = 0
if ($Key) {
    $vk = @{ ESC = 0x1B; ENTER = 0x0D; SPACE = 0x20; TAB = 0x09 }[$Key.ToUpper()]
    if (-not $vk) { throw "unknown key $Key" }
}
$proc = Get-Process -Name TPC -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1
if (-not $proc) { Fail "Two Point Campus (TPC) window not found" }
$hwnd = $proc.MainWindowHandle
[TpcInput]::SetForegroundWindow($hwnd) | Out-Null
$deadline = (Get-Date).AddMilliseconds(1500)
while ([TpcInput]::GetForegroundWindow() -ne $hwnd -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 50 }
if ([TpcInput]::GetForegroundWindow() -ne $hwnd) { Fail "the game window did not come to the foreground" }
Start-Sleep -Milliseconds 200
if ($X -ge 0 -and $Y -ge 0) {
    $rect = New-Object TpcInput+RECT
    if (-not [TpcInput]::GetWindowRect($hwnd, [ref]$rect)) { Fail "cannot read the game window position" }
    if ($X -lt $rect.Left -or $X -ge $rect.Right -or $Y -lt $rect.Top -or $Y -ge $rect.Bottom) {
        Fail "point $X,$Y is outside the game window ($($rect.Left),$($rect.Top)-$($rect.Right),$($rect.Bottom))"
    }
    [TpcInput]::SetCursorPos($X, $Y) | Out-Null
    Start-Sleep -Milliseconds 150
    [TpcInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)  # left down
    Start-Sleep -Milliseconds 80
    [TpcInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)  # left up
    "clicked $X,$Y"
}
if ($Key) {
    if ([TpcInput]::GetForegroundWindow() -ne $hwnd) { Fail "the game window lost the foreground" }
    [TpcInput]::keybd_event([byte]$vk, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 80
    [TpcInput]::keybd_event([byte]$vk, 0, 2, [UIntPtr]::Zero)
    "pressed $Key"
}

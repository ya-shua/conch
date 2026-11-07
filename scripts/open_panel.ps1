# 打开（或提到最前）海螺控制台页面。
#
# 为什么不是「VBS 里 shell.Run 一下就完事」：
#   Edge/Chrome 已经在跑时，shell.Run 只会在旧窗口里加一个**后台**标签，
#   窗口不前置 —— 人看起来就是「没打开」。
#
# 为什么要记窗口句柄（data\panel.hwnd）：
#   Chromium 的窗口标题跟着「当前标签」走；把海螺那个窗口切到别的页面，
#   标题里就没有「海螺」了 —— 只按标题找会误判成「没开着」，再开一个窗口，
#   于是越点越多。记下句柄就不会认错。
#
# 一次调用做完：找窗口 → 没有就开 → 提到最前。省掉两趟 powershell 启动。

param(
    [string]$Url = 'http://127.0.0.1:7801/',
    [string]$TitlePattern = '海螺|运行总览'
)

$ErrorActionPreference = 'SilentlyContinue'
$here = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $here 'data\panel.hwnd'

$code = @'
using System;
using System.Text;
using System.Diagnostics;
using System.Runtime.InteropServices;

public class PanelWindow
{
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);

    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool f);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();

    delegate bool EnumProc(IntPtr h, IntPtr p);

    public static string[] BrowserProcesses = { "msedge", "chrome", "brave", "firefox", "vivaldi", "opera" };

    public static bool Alive(IntPtr h) { return h != IntPtr.Zero && IsWindow(h) && IsWindowVisible(h); }

    public static IntPtr Find(string pattern)
    {
        IntPtr found = IntPtr.Zero;
        var rx = new System.Text.RegularExpressions.Regex(pattern);
        EnumWindows((h, p) =>
        {
            if (!IsWindowVisible(h)) return true;
            var sb = new StringBuilder(400);
            GetWindowText(h, sb, 400);
            string title = sb.ToString();
            if (title.Length == 0) return true;
            if (!rx.IsMatch(title)) return true;
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            string name = "";
            try { name = Process.GetProcessById((int)pid).ProcessName.ToLowerInvariant(); } catch { }
            bool isBrowser = false;
            foreach (var b in BrowserProcesses) if (name.Contains(b)) { isBrowser = true; break; }
            if (!isBrowser) return true;
            found = h;
            return false;
        }, IntPtr.Zero);
        return found;
    }

    public static bool Maximize(IntPtr h)
    {
        if (h == IntPtr.Zero) return false;
        return ShowWindow(h, 3);   // SW_MAXIMIZE
    }

    public static bool Raise(IntPtr h)
    {
        if (h == IntPtr.Zero) return false;
        // 只有「最小化」时才 SW_RESTORE。对最大化窗口调 SW_RESTORE 会把它打回小窗，
        // 这就是之前「一唤起就变小」的原因。
        if (IsIconic(h)) ShowWindow(h, 9);
        else ShowWindow(h, 5);   // SW_SHOW：不改变最大化/还原状态，只是让它可见
        // 唤起时如果还是小窗，就铺满 —— 「打开页面」的默认状态是最大化
        if (-not (IsZoomed(h))) ShowWindow(h, 3);
        BringWindowToTop(h);
        SetForegroundWindow(h);
        if (GetForegroundWindow() != h)
        {
            uint fg = 0;
            GetWindowThreadProcessId(GetForegroundWindow(), out fg);
            uint me = GetCurrentThreadId();
            if (fg != 0 && AttachThreadInput(me, fg, true))
            {
                SetForegroundWindow(h);
                AttachThreadInput(me, fg, false);
            }
        }
        // 抢不到前台（后台会话很常见）不能算失败：窗口已经可见、已经置顶就够了，
        // 否则会被误判成“没开着”，每点一次就多开一个窗口。
        return IsWindow(h) && IsWindowVisible(h);
    }
}
'@

if (-not ('PanelWindow' -as [type])) {
    Add-Type -TypeDefinition $code -ReferencedAssemblies 'System.Text.RegularExpressions'
}

$sw = [Diagnostics.Stopwatch]::StartNew()

function Save-Handle($h) {
    try { Set-Content -LiteralPath $stateFile -Value ($h.ToInt64()).ToString() -Encoding ASCII } catch {}
}

function Try-Raise($h) {
    for ($i = 0; $i -lt 6; $i++) {
        if ([PanelWindow]::Raise($h)) { return $true }
        Start-Sleep -Milliseconds 120
    }
    return $false
}

# 1. 上次记下的窗口还活着？
if (Test-Path -LiteralPath $stateFile) {
    $txt = (Get-Content -LiteralPath $stateFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $old = [IntPtr]::Zero
    $n = [int64]0
    if ($txt -and [int64]::TryParse($txt.Trim(), [ref]$n)) { $old = [IntPtr]$n }
    if ([PanelWindow]::Alive($old)) {
        if (Try-Raise $old) {
            Write-Host ("已经开着，提到最前（{0:N1}s）" -f $sw.Elapsed.TotalSeconds)
            exit 0
        }
    }
}

# 2. 记录失效：按标题找一次
$h = [PanelWindow]::Find($TitlePattern)
if ($h -ne [IntPtr]::Zero) {
    Save-Handle $h
    if (Try-Raise $h) {
        Write-Host ("按标题找到，提到最前（{0:N1}s）" -f $sw.Elapsed.TotalSeconds)
        exit 0
    }
}

# 3. 真没有：开一个独立窗口（Chromium 系），等它出来再前置
function Get-BrowserExe {
    $cands = @()
    $prog = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice' -Name ProgId).ProgId
    if ($prog -match 'Edge') {
        $cands = @(
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
        )
    } elseif ($prog -match 'Chrome') {
        $cands = @(
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
        )
    }
    foreach ($c in $cands) { if ($c -and (Test-Path -LiteralPath $c)) { return $c } }
    return ''
}

$exe = Get-BrowserExe
if ($exe) {
    Start-Process -FilePath $exe -ArgumentList '--new-window', '--start-maximized', $Url | Out-Null
} else {
    Start-Process -FilePath $Url | Out-Null
}

for ($i = 0; $i -lt 50; $i++) {
    $h = [PanelWindow]::Find($TitlePattern)
    if ($h -ne [IntPtr]::Zero) {
        Save-Handle $h
        [PanelWindow]::Maximize($h) | Out-Null   # 新开的一律铺满，别给人看小窗
        if (Try-Raise $h) {
            Write-Host ("新窗口打开并前置（{0:N1}s）" -f $sw.Elapsed.TotalSeconds)
            exit 0
        }
    }
    Start-Sleep -Milliseconds 150
}
Write-Host ("窗口开了但没抢到前台（{0:N1}s）" -f $sw.Elapsed.TotalSeconds)
exit 1
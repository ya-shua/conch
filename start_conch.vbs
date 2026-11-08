' Conch launcher: hand everything to the tray icon.
' ASCII only (wscript reads .vbs as ANSI).
'
' One entry point, one process: scripts/tray.py owns the whole stack.
'   - starts every service (SnowLuma, memory panel, mood, plugins...) in the background
'   - keeps a single icon in the notification area; closing the browser changes nothing
'   - services stop only when "Quit" is picked from that icon's menu
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
py = base & "\.venv\Scripts\pythonw.exe"
tray = base & "\scripts\tray.py"

If Not fso.FileExists(py) Then
  shell.Popup "pythonw.exe not found: " & py, 8, "Conch", 16
  WScript.Quit 1
End If
If Not fso.FileExists(tray) Then
  shell.Popup "tray.py not found: " & tray, 8, "Conch", 16
  WScript.Quit 1
End If

' 0 = hidden window; the icon itself is a shell notification icon, no console needed.
shell.Run """" & py & """ """ & tray & """", 0, False
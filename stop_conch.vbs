' Conch: stop everything (host + all plugin services), hidden.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
py = base & "\.venv\Scripts\pythonw.exe"
script = base & "\scripts\stop_all.py"
If Not fso.FileExists(script) Then
  shell.Popup "stop_all.py not found: " & script, 8, "Conch", 16
  WScript.Quit 1
End If
shell.Run """" & py & """ """ & script & """", 0, False
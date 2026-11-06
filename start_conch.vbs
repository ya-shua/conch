' Conch launcher: start every service, then open the console page.
' ASCII only (wscript reads .vbs as ANSI).
' The browser is opened from THIS process: a browser launched by the window-less
' pythonw child never gets a visible window.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
py = base & "\.venv\Scripts\pythonw.exe"
script = base & "\scripts\start_all.py"
urlFile = base & "\data\panel.url"
If Not fso.FileExists(py) Then
  shell.Popup "pythonw.exe not found: " & py, 8, "Conch", 16
  WScript.Quit 1
End If
If Not fso.FileExists(script) Then
  shell.Popup "start_all.py not found: " & script, 8, "Conch", 16
  WScript.Quit 1
End If

url = "http://127.0.0.1:7801/"
If fso.FileExists(urlFile) Then
  url = Trim(fso.OpenTextFile(urlFile, 1).ReadAll())
End If
If Len(url) = 0 Then url = "http://127.0.0.1:7801/"

shell.Run """" & py & """ """ & script & """", 1, False

' Wait until the panel answers (/ping.js), then open it. The panel binds its port
' before plugins load, so this is normally ready within a second or two.
probe = url
pos = InStr(probe, "?")
If pos > 0 Then probe = Left(probe, pos - 1)
probeUrl = probe & "ping.js"

Set http = CreateObject("MSXML2.XMLHTTP")
ready = False
For i = 1 To 200
  On Error Resume Next
  http.Open "GET", probeUrl, False
  http.Send
  If Err.Number = 0 Then
    If http.Status = 200 Then ready = True
  End If
  Err.Clear
  On Error GoTo 0
  If ready Then Exit For
  WScript.Sleep 100
Next

' --- open the panel -------------------------------------------------------
' One powershell call does it all: raise the panel window if it is already
' open, otherwise open it and raise it. The old code ran powershell twice and
' sat 6 pointless seconds waiting before even trying to open anything.
openPs = base & "\scripts\open_panel.ps1"
opened = False
If fso.FileExists(openPs) Then
  On Error Resume Next
  rc = shell.Run("powershell -NoProfile -ExecutionPolicy Bypass -File """ & openPs & """ -Url """ & url & """", 0, True)
  If rc = 0 Then opened = True
  On Error GoTo 0
End If
If Not opened Then
  If fso.FileExists(base & "\scripts\focus_panel.ps1") Then
    On Error Resume Next
    shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & base & "\scripts\focus_panel.ps1""", 0, True
    On Error GoTo 0
  Else
    shell.Run url, 1, False
  End If
End If
Function ChromiumExe()
  Dim prog, cand, i
  ChromiumExe = ""
  On Error Resume Next
  prog = shell.RegRead("HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice\ProgId")
  If Err.Number <> 0 Then Err.Clear : Exit Function
  If InStr(1, prog, "Edge", vbTextCompare) > 0 Then
    cand = Array("C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "C:\Program Files\Microsoft\Edge\Application\msedge.exe")
  ElseIf InStr(1, prog, "Chrome", vbTextCompare) > 0 Then
    cand = Array("C:\Program Files\Google\Chrome\Application\chrome.exe", "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
  Else
    Exit Function
  End If
  For i = 0 To UBound(cand)
    If fso.FileExists(cand(i)) Then
      ChromiumExe = cand(i)
      Exit Function
    End If
  Next
End Function
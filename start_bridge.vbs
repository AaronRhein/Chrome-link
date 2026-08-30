' Chrome Window Bridge silent launcher (prefers bundled python-embed runtime)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir_ = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir_

local_pyw = dir_ & "\python-embed\pythonw.exe"
If fso.FileExists(local_pyw) Then
    pyw = """" & local_pyw & """"
Else
    Set exec = sh.Exec("cmd /c where pythonw.exe 2>nul")
    found = ""
    Do While Not exec.StdOut.AtEndOfStream
        found = Trim(exec.StdOut.ReadLine())
        If found <> "" Then Exit Do
    Loop
    If found = "" Then
        MsgBox "pythonw.exe not found: keep the python-embed folder next to this script, or install Python and add it to PATH.", 16, "Chrome Window Bridge"
        WScript.Quit
    End If
    pyw = """" & found & """"
End If

sh.Run pyw & " """ & dir_ & "\bridge.py""", 0, False

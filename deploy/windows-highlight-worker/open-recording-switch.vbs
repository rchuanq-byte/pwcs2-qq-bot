Option Explicit

Dim fileSystem, scriptDirectory, pythonw, application, shell
Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
pythonw = fileSystem.BuildPath(scriptDirectory, ".venv\Scripts\pythonw.exe")
application = fileSystem.BuildPath(scriptDirectory, "recording-switch.pyw")

If Not fileSystem.FileExists(pythonw) Then
    MsgBox "Local Python environment was not found. Run setup-worker.ps1 first.", 16, "Niuniu CS2 Highlight"
    WScript.Quit 1
End If

Set shell = CreateObject("WScript.Shell")
' pythonw.exe has no console; use a normal window style so the Tk switch stays visible.
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & application & Chr(34), 1, False

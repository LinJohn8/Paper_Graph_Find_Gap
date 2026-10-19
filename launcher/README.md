# PaperGraph.exe launcher

From the repository root after the frontend build:

```powershell
.\launcher\build-launcher.ps1
.\PaperGraph.exe
```

The launcher is a small .NET Framework WinForms executable. It starts only the existing local Python/Next services, checks the SQLite API path, opens `http://127.0.0.1:3000/map?venue=icml&year=2025`, and shuts down its child process trees when the window closes. It does not bundle Python, Node, `node_modules`, `.next`, or the database, so keep the EXE in the project root.

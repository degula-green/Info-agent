# RAG service

使用系统 Python，不创建虚拟环境。依赖由 `uv` 管理。

```powershell
uv pip install --python "C:\Program Files\Python311\python.exe" --target .runtime/python --link-mode copy -r requirements.txt
$env:PYTHONPATH = "$PWD\.runtime\python"
python -m uvicorn app.main:app --reload --port 8000
```

通常直接双击 `../../scripts/start-platform.cmd` 即可；依赖缺失或
`requirements.txt` 发生变化时，启动脚本会自动执行上述安装。

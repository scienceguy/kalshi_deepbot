import sys, os
print("sys.executable:", sys.executable)
p = os.path.join(os.path.dirname(sys.executable), "mempalace")
print("venv mempalace path:", p)
print("exists:", os.path.exists(p), "executable:", os.access(p, os.X_OK) if os.path.exists(p) else None)

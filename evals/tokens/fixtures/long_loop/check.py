"""Quick per-module check: python3 check.py NN [NN ...]"""
import ast
import sys

for n in sys.argv[1:]:
    fns = [f.name for f in ast.parse(open(f"modules/m{int(n):02d}.py").read()).body if isinstance(f, ast.FunctionDef)]
    print(f"m{int(n):02d}: {len(fns)} functions, {sum(f.startswith('sync_') for f in fns)} sync_, ok")

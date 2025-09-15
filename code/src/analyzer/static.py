# map file paths to module names (folder-based)
import os
from typing import List, Set

def map_files_to_modules(files: List[str]) -> List[str]:
    modules: Set[str] = set()
    for f in files:
        # assume repo structure: src/<module_name>/...
        parts = f.split(os.sep)
        if len(parts) >= 2 and parts[0] in ("src","packages"):
            modules.add(parts[1])
        else:
            # fallback: top-level folder name
            modules.add(parts[0])
    return list(modules)

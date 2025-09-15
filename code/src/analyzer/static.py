# code/src/analyzer/static.py
"""
Simple static analyzer utilities.
- Map file paths -> module names (configurable group_depth)
- Build dependency graph from Python imports under src roots
- Compute impacted modules given changed files using BFS up to max_hops
"""

from typing import List, Dict, Set, Tuple, Optional
import os
import ast
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _normalize_path(p: str) -> str:
    """Normalize path separators and strip leading ./"""
    if p is None:
        return p
    p = p.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p.strip()


def discover_source_roots(repo_root: str = ".", src_roots: List[str] = None) -> List[str]:
    if src_roots is None:
        src_roots = ["src", "packages", "lib"]
    existing = []
    for r in src_roots:
        full = os.path.join(repo_root, r)
        if os.path.isdir(full):
            existing.append(r)
    # if none found, still include default 'src' so analyzer can operate on paths
    if not existing:
        existing = src_roots
    return existing


def _file_to_module_from_parts(parts: List[str], group_depth: int) -> str:
    """
    Build module name using the first `group_depth` parts.
    E.g., parts=['src','pkg','a','file.py'], group_depth=2 => 'pkg_a'
    """
    if len(parts) <= 1:
        # fallback to filename (without extension)
        return os.path.splitext(parts[-1])[0]
    # exclude leading 'src' / 'packages' root if present
    if parts[0] in ("src", "packages", "lib"):
        relevant = parts[1:]
    else:
        relevant = parts
    if not relevant:
        # fallback
        return os.path.splitext(parts[-1])[0]
    use = relevant[:group_depth]
    # sanitize: remove file extensions and invalid chars
    use_clean = [os.path.splitext(p)[0] for p in use]
    return "_".join(use_clean)


def map_files_to_modules(
    files: List[str],
    repo_root: str = ".",
    src_roots: Optional[List[str]] = None,
    group_depth: int = 2,
) -> Tuple[Dict[str, str], List[str]]:
    """
    Map each file path to a module name (heuristic).
    Returns (file_to_module_map, unique_module_list).
    - group_depth: number of path parts to join after top-level src root.
      Example: 'src/pkg/a/file.py' with group_depth=2 -> 'pkg_a'
    """
    if src_roots is None:
        src_roots = ["src", "packages", "lib"]

    file_to_module: Dict[str, str] = {}
    modules: Set[str] = set()

    for f in files:
        if not isinstance(f, str) or not f.strip():
            continue
        f_norm = _normalize_path(f)
        # strip any leading repo root
        if f_norm.startswith(repo_root + "/"):
            f_norm = f_norm[len(repo_root) + 1 :]
        parts = f_norm.split("/")
        module = _file_to_module_from_parts(parts, group_depth)
        file_to_module[f] = module
        modules.add(module)

    return file_to_module, sorted(list(modules))


# --------- Import parsing / dependency graph ---------

def _extract_top_level_import_names_from_ast(node: ast.AST) -> Set[str]:
    """
    Return top-level module names imported by the AST node.
    E.g., "import pkg.sub" -> {'pkg'}, "from pkg.sub import x" -> {'pkg'}.
    """
    names: Set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for alias in n.names:
                top = alias.name.split(".")[0]
                if top:
                    names.add(top)
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                top = n.module.split(".")[0]
                if top:
                    names.add(top)
    return names


def _safe_parse_file(path: str) -> Optional[ast.AST]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read()
        return ast.parse(data, filename=path)
    except (SyntaxError, UnicodeDecodeError) as e:
        logger.debug("Could not parse %s: %s", path, e)
        return None
    except OSError as e:
        logger.debug("Could not open %s: %s", path, e)
        return None


def discover_repo_modules_from_fs(
    repo_root: str = ".",
    src_roots: Optional[List[str]] = None,
    group_depth: int = 2,
) -> Set[str]:
    """
    Discover modules by scanning directories under src_roots.
    A module is produced by grouping first `group_depth` parts after src root.
    """
    if src_roots is None:
        src_roots = ["src", "packages", "lib"]
    modules: Set[str] = set()
    for root in src_roots:
        base = os.path.join(repo_root, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # consider only directories containing .py files to avoid scanning irrelevant folders
            py_files = [f for f in filenames if f.endswith(".py")]
            if not py_files:
                continue
            rel = os.path.relpath(dirpath, repo_root)
            parts = rel.split(os.sep)
            mod = _file_to_module_from_parts(parts, group_depth)
            modules.add(mod)
    return modules


def build_import_dependency_graph(
    repo_root: str = ".",
    src_roots: Optional[List[str]] = None,
    group_depth: int = 2,
) -> Dict[str, Set[str]]:
    """
    Scan Python files under src_roots and build a module->set(dependent_modules) adjacency map.
    Uses top-level import names and maps them to module tokens using same group_depth.
    """
    if src_roots is None:
        src_roots = ["src", "packages", "lib"]

    repo_modules = discover_repo_modules_from_fs(repo_root, src_roots, group_depth)
    adjacency: Dict[str, Set[str]] = {m: set() for m in repo_modules}

    for root in src_roots:
        base = os.path.join(repo_root, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            py_files = [f for f in filenames if f.endswith(".py")]
            if not py_files:
                continue
            rel = os.path.relpath(dirpath, repo_root)
            parts = rel.split(os.sep)
            this_module = _file_to_module_from_parts(parts, group_depth)
            if this_module not in adjacency:
                adjacency.setdefault(this_module, set())
            for py in py_files:
                fullpath = os.path.join(dirpath, py)
                node = _safe_parse_file(fullpath)
                if not node:
                    continue
                imported = _extract_top_level_import_names_from_ast(node)
                # map imported top-level names to module tokens if they exist in repo_modules
                for imp in imported:
                    # map 'imp' into our module token using same group_depth (if that folder exists)
                    # try to find a match: look for modules that start with imp or equal to imp
                    # simple mapping: transform candidate by replacing '.' with '_' up to group_depth
                    cand_parts = [imp]  # just top-level
                    cand_token = "_".join(cand_parts[:group_depth])
                    # best-effort map: if cand_token exists or any repo module startswith cand_token
                    matches = [m for m in repo_modules if m == cand_token or m.startswith(cand_token + "_")]
                    if matches:
                        # add edges to all matching modules (conservative)
                        for mm in matches:
                            adjacency[this_module].add(mm)

    # ensure adjacency contains all modules
    for m in list(adjacency.keys()):
        adjacency.setdefault(m, set())
    return adjacency


def get_impacted_modules(
    changed_files: List[str],
    repo_root: str = ".",
    src_roots: Optional[List[str]] = None,
    group_depth: int = 2,
    max_hops: int = 1,
) -> List[Dict]:
    """
    Given changed_files, compute impacted modules.
    Returns a list of dicts: {component: <module>, confidence: float, reason: str}
    Confidence:
      - 1.0 for direct-change
      - decay for hops: 0.8 for hop1, 0.6 for hop2, etc.
    """
    if src_roots is None:
        src_roots = ["src", "packages", "lib"]

    # basic mapping
    file_to_module, direct_modules = map_files_to_modules(changed_files, repo_root, src_roots, group_depth)

    # build repo graph
    graph = build_import_dependency_graph(repo_root=repo_root, src_roots=src_roots, group_depth=group_depth)
    # ensure graph includes direct modules even if not discovered from FS
    for dm in direct_modules:
        graph.setdefault(dm, set())

    impacted: Dict[str, Tuple[float, str]] = {}

    # direct modules
    for f, mod in file_to_module.items():
        impacted.setdefault(mod, (1.0, f"direct-change:{f}"))

    # BFS for neighbors up to max_hops
    from collections import deque

    # queue entries: (module, hop_distance, origin_module)
    q = deque()
    for mod in list(impacted.keys()):
        q.append((mod, 0, mod))

    visited: Dict[str, int] = {m: 0 for m in impacted.keys()}

    while q:
        current, hop, origin = q.popleft()
        if hop >= max_hops:
            continue
        neighbors = graph.get(current, set())
        for nb in neighbors:
            next_hop = hop + 1
            if nb in visited and visited[nb] <= next_hop:
                continue
            visited[nb] = next_hop
            # compute confidence decay: 1.0 -> 0.8 -> 0.6...
            confidence = max(0.2, 1.0 - 0.2 * next_hop)
            reason = f"dep-hop-{next_hop} from {origin}"
            # If already present with higher confidence, keep highest
            prev = impacted.get(nb)
            if prev is None or confidence > prev[0]:
                impacted[nb] = (confidence, reason)
            q.append((nb, next_hop, origin))

    # format predictions sorted by confidence desc then name
    preds = [
        {"component": comp, "confidence": float(conf), "reason": reason}
        for comp, (conf, reason) in impacted.items()
    ]
    preds.sort(key=lambda x: (-x["confidence"], x["component"]))
    return preds

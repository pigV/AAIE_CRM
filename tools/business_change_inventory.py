"""Read Python source without importing it; inventory possible mutation paths.

This is a conservative discovery aid, not proof of full runtime coverage.
Dynamic SQL/calls and transaction ownership still require business review.
No database connections, file mutations or evaluated source are used to scan.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import json
from pathlib import Path
import re


MUTATION = re.compile(r'\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+([\w.]+)', re.I)
FILE_WRITES = {'write_bytes', 'write_text', 'unlink', 'rename', 'replace'}


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return '.'.join(filter(None, (_name(node.value), node.attr)))
    return ''


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(x.value if isinstance(x, ast.Constant) and isinstance(x.value, str) else '{dynamic}' for x in node.values)
    return None


def inspect_source(source, path='source.py'):
    tree = ast.parse(source, filename=path)
    rows = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scopes = []
            self.row = None

        def scope(self, node, name):
            previous = self.row
            row = dict(path=path, name=name, line=getattr(node, 'lineno', 1),
                       end_line=getattr(node, 'end_lineno', 1), writes=[],
                       tables=[], calls=[], transactions=[], routes=[], unresolved_calls=[])
            rows.append(row)
            self.row = row
            for child in node.body:
                self.visit(child)
            row['tables'] = sorted(set(row['tables']))
            row['calls'] = sorted(set(row['calls']))
            row['transactions'] = sorted(set(row['transactions']))
            self.row = previous

        def visit_ClassDef(self, node):
            self.scopes.append(node.name)
            for child in node.body:
                self.visit(child)
            self.scopes.pop()

        def visit_FunctionDef(self, node):
            name = '.'.join([*self.scopes, node.name])
            self.scopes.append(node.name)
            self.scope(node, name)
            self.scopes.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = _name(node.func)
            if self.row is not None:
                self.row['calls'].append(name)
                if not name or isinstance(node.func,(ast.Call,ast.Subscript)):
                    self.row['unresolved_calls'].append(dict(line=node.lineno,kind='dynamic_dispatch'))
                leaf = name.rsplit('.', 1)[-1]
                if leaf in {'transaction', 'fee_write_transaction', 'request_scope', 'operation'}:
                    self.row['transactions'].append(name)
                if leaf in {'execute', 'executemany', 'executescript'}:
                    sql = _literal(node.args[0]) if node.args else None
                    matches = list(MUTATION.finditer(sql or ''))
                    if sql is None or matches:
                        self.row['writes'].append(dict(line=node.lineno, sink=name,
                                                       kind='dynamic_sql' if sql is None or '{dynamic}' in sql else 'sql'))
                    self.row['tables'].extend(m.group(1).split('.')[-1] for m in matches if m.group(1).upper()!='SET')
                elif leaf in FILE_WRITES:
                    receiver=node.func.value if isinstance(node.func,ast.Attribute) else None
                    known_string=isinstance(receiver,ast.Constant) and isinstance(receiver.value,str)
                    known_string=known_string or (isinstance(receiver,ast.Call) and _name(receiver.func)=='str')
                    if leaf=='replace' and known_string:
                        pass
                    elif leaf=='replace' and name!='os.replace' and not re.search(r'path|source|dest|target|file',name,re.I):
                        self.row['unresolved_calls'].append(dict(line=node.lineno,kind='ambiguous_replace_method'))
                    else:
                        self.row['writes'].append(dict(line=node.lineno, sink=name, kind='file'))
                elif leaf == 'open':
                    mode_node = next((x.value for x in node.keywords if x.arg=='mode'), node.args[1] if len(node.args)>1 else None)
                    mode = _literal(mode_node) if mode_node else 'r'
                    if mode is None or any(c in mode for c in 'wax+'):
                        self.row['writes'].append(dict(line=node.lineno, sink=name, kind='dynamic_file' if mode is None else 'file'))
            self.generic_visit(node)

    visitor = Visitor()
    visitor.scope(tree, '<module>')
    routes = defaultdict(set)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts)<2:
            continue
        url = _literal(node.elts[0])
        handler = _name(node.elts[1])
        if url and url.startswith(('^/', '/')) and handler.startswith('self.'):
            routes[handler[5:]].add(url)
    for row in rows:
        row['routes'] = sorted(routes[row['name'].rsplit('.',1)[-1]])
    return [row for row in rows if row['name']!='<module>' or row['calls']]


def build_inventory(sources):
    rows = [row for path, source in sorted(sources.items()) for row in inspect_source(source, path)]
    names = defaultdict(list)
    by_file_name = defaultdict(list)
    for row in rows:
        row['id'] = f"{row['path']}:{row['name']}"
        names[row['name'].rsplit('.',1)[-1]].append(row)
        by_file_name[(row['path'], row['name'].rsplit('.',1)[-1])].append(row)
    counts = defaultdict(int)
    for row in rows:
        counts[row['id']] += 1
    for row in rows:
        if counts[row['id']]>1:
            row['id'] += '@'+str(row['line'])
    index = {row['id']:row for row in rows}
    for row in rows:
        dependencies = set()
        for call in row['calls']:
            if not call or ('.' in call and not call.startswith(('self.','cls.'))):
                continue
            leaf = call.rsplit('.',1)[-1]
            candidates = by_file_name.get((row['path'],leaf)) or names.get(leaf, [])
            dependencies.update(x['id'] for x in candidates if x['id']!=row['id'])
        row['dependencies'] = sorted(dependencies)
        row['may_mutate'] = bool(row['writes'])
    changed = True
    while changed:
        changed = False
        for row in rows:
            if not row['may_mutate'] and any(index[x]['may_mutate'] for x in row['dependencies']):
                row['may_mutate'] = True
                changed = True
    return dict(functions=rows, limitations=[
        'Possible mutation is conservative; dynamic SQL may be a read.',
        'Static self/global-name call resolution is approximate; imported aliases, mixin globals, decorators, C extensions, and dynamic dispatch require review.',
        'Routes list literal path/self-handler pairs only; absence of a route is not proof of unreachability.',
        'Transaction calls do not prove the same connection or outermost commit boundary.',
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Use Git's tracked-file inventory; never walk runtime/config/attachment trees.
    import subprocess
    paths = subprocess.check_output(['git','-C',str(args.root),'ls-files','-z','--','*.py']).decode().split('\0')
    sources = {}
    for name in paths:
        if not name or name.startswith(('tests/', 'scripts/')):
            continue
        path = args.root/name
        if path.is_file():
            sources[name] = path.read_text(encoding='utf-8-sig')
    result = build_inventory(sources)
    result['source_files'] = sorted(sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    rows = result['functions']
    print(json.dumps(dict(files=len(sources),functions=len(rows),direct_writers=sum(bool(x['writes']) for x in rows),possible_mutating_routes=sum(bool(x['routes']) and x['may_mutate'] for x in rows))))


if __name__ == '__main__':
    main()

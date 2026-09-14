"""Opt-in deterministic virtual sandbox for local task fixtures only.

This is an interpreter of a small documented command vocabulary, NOT the
official terminal or arbitrary Python. It never opens host files, runs shell
commands, imports fixture code or connects to a network. All content comes from
an explicit environment-owned fixture; it must never enter policy observations.
"""
from __future__ import annotations

import json
import posixpath
import shlex
from decimal import Decimal, InvalidOperation
from typing import Any

OUTPUT_BYTES = 65536  # interface 1.1: local implementation of the 64KB cap
MAX_COMMAND_CHARS = 8192  # engineering bound, not an official command limit


def _reply(body: str, code: int = 0, *, marker: str = '') -> str:
    data = str(body).encode('utf-8')
    if len(data) > OUTPUT_BYTES:
        body = data[:OUTPUT_BYTES].decode('utf-8', errors='ignore') + '\n[TRUNCATED]'
    return (f'[{marker}]' if marker else f'[exitCode:{code}]') + '\n' + body


def _path(value: str, cwd: str) -> str:
    return posixpath.normpath(value if value.startswith('/') else posixpath.join(cwd, value))


def execute(command: str, fixture: dict[str, Any], *, active: bool) -> str:
    """Return a documented lastCmdResult envelope, using virtual data only.

Supported: pwd, ls [-la] [path], cat [--] path, and python3 <virtual-program>
    --<filter> <value>. Programs are declarative lookup/sum fixtures, not scripts.
Unsupported shell constructs return nonzero without partially executing them.
"""
    if not active:
        return _reply('No active local task', marker='JUDGER_ERROR')
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
        return _reply('Invalid command', 2)
    if not isinstance(fixture, dict):
        return _reply('Invalid local fixture', marker='JUDGER_ERROR')
    try:
        # Faults are explicitly authored test cases, not prompt directives.
        fault = (fixture.get('faults') or {}).get(command)
        if fault in ('TIMEOUT', 'JUDGER_ERROR'):
            return _reply('Injected local test fault', marker=fault)
        if fault == 'TRUNCATED':
            return _reply('Injected partial output\n[TRUNCATED]')
        lex = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
        lex.whitespace_split = True
        lex.commenters = ''
        argv = list(lex)
        if not argv or any(token and all(c in ';&|<>' for c in token) for token in argv):
            return _reply('Shell composition is unsupported by the virtual fixture', 2)
        cwd = _path(str(fixture.get('cwd') or '/workspace'), '/')
        files = {_path(str(k), cwd): str(v) for k, v in (fixture.get('files') or {}).items()}
        programs = {_path(str(k), cwd): v for k, v in (fixture.get('programs') or {}).items()}
        # Virtual directories exist independently of readable files. Reporting
        # EISDIR as ENOENT misled a real model into repeatedly searching for a
        # directory already present in ls output.
        directories = {'/', cwd}
        directories.update(_path(str(p), cwd) for p in fixture.get('directories') or [])
        for name in files.keys() | programs.keys() | set(directories):
            parent = posixpath.dirname(name)
            while parent and parent not in directories:
                directories.add(parent)
                parent = posixpath.dirname(parent)
        if argv == ['pwd']:
            return _reply(cwd)
        if argv[0] == 'cat':
            args = argv[1:]
            if args[:1] == ['--']:
                args = args[1:]
            if len(args) != 1:
                return _reply('Usage: cat [--] path', 2)
            name = _path(args[0], cwd)
            if name in directories:
                return _reply('cat: ' + name + ': Is a directory', 1)
            if name in programs:
                return _reply('Virtual executable source is not exposed; use its documented interface', 1)
            if name not in files:
                return _reply('No such virtual file: ' + name, 1)
            return _reply(files[name])
        if argv[0] == 'ls':
            args = argv[1:]
            if args[:1] in (['-la'], ['-l'], ['-a']):
                args = args[1:]
            if len(args) > 1:
                return _reply('Usage: ls [-la] [path]', 2)
            name = _path(args[0], cwd) if args else cwd
            if name in files or name in programs:
                return _reply(posixpath.basename(name))
            prefix = name.rstrip('/') + '/'
            children = sorted({p[len(prefix):].split('/')[0] for p in files.keys() | programs.keys() | directories
                               if p != name and p.startswith(prefix)})
            return _reply('\n'.join(children)) if name in directories else _reply('No such virtual directory', 1)
        if argv[0] in ('python3', 'python') and len(argv) >= 2:
            program = programs.get(_path(argv[1], cwd))
            if program is None:
                return _reply('Unsupported virtual program; Python code is never executed', 127)
            return _query(argv[2:], program)
        return _reply('Unsupported virtual command', 127)
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, ArithmeticError):
        return _reply('Invalid command or local fixture schema', 2)


def _query(args: list[str], program: dict[str, Any]) -> str:
    allowed = program.get('filters') or []
    if len(args) % 2:
        return _reply('Expected --field value pairs', 2)
    filters = {}
    for option, value in zip(args[::2], args[1::2]):
        field = option[2:] if option.startswith('--') else ''
        if field not in allowed or field in filters:
            return _reply('Unknown or duplicate query argument', 2)
        filters[field] = value
    if any(k not in filters for k in program.get('required_filters') or []):
        return _reply('Missing required query argument', 2)
    rows = [row for row in program['records']
            if all(k in row and str(row[k]) == v for k, v in filters.items())]
    if not rows:
        return _reply('No matching records', 1)
    operation = program.get('operation', 'lookup')
    if operation == 'lookup':
        columns = program['output_fields']
        result = [{k: row[k] for k in columns} for row in rows]
    elif operation == 'sum':
        values = [Decimal(str(row[program['value_field']])) for row in rows]
        if not all(v.is_finite() for v in values):
            return _reply('Nonfinite input', 2)
        result = {program.get('result_field', 'total'): str(sum(values, Decimal(0)))}
    else:
        return _reply('Unsupported virtual query operation', 2)
    return _reply(json.dumps(result, ensure_ascii=False, allow_nan=False))


def active_task_fixture(state: dict[str, Any]) -> dict[str, Any] | None:
    """Read simulator-private environment only; never used by policy code."""
    world = (state.get('_demo') or {}).get('task_world') or {}
    side = (state.get('teamOur') or {}).get('type')
    for index in (1, 2):
        active = ((world.get('points') or {}).get(f'{side}TaskPoint{index}') or {}).get('active')
        if active and isinstance(active.get('sandbox_fixture'), dict):
            return active['sandbox_fixture']
    return None

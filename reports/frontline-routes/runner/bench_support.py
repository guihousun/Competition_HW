"""Portable public-only boundary, matching the frozen native lab's JSON format.

Import agent only after the caller adds its explicit --source to sys.path.
This module never inserts a different worktree or silently selects an engine.
"""
import hashlib
import json
import os
from pathlib import Path
from agent.scenarios import observation

PUBLIC_KEYS={'errors','lastCmdResult','lastRoundRoleActionResults','lastSummonTreasureResult',
             'llmResp','mapInfo','phaseTask','robot','roundNo','teamEnemy','teamOur',
             'vendorShopList','weaponShopList','worldNews'}

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def public_state(state):
    return {k:v for k,v in observation(state).items() if k in PUBLIC_KEYS}

def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        f.write(canonical(value)+'\n');f.flush();os.fsync(f.fileno())
    tmp.replace(path)

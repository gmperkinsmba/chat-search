#!/usr/bin/env python3
"""Local import, source inspection, and full-text search for conversation exports."""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from desktop_server import make_server as _make_desktop_server
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('EVIDENCE_NOTEBOOK_DATA',Path.home()/'.local/share/evidence-notebook'))
DATA.mkdir(mode=0o700,parents=True,exist_ok=True)
try: DATA.chmod(0o700)
except OSError: pass
DB=DATA/'notebook.sqlite3'
TOKEN=secrets.token_urlsafe(24)
MAX_BYTES=5_000_000
PENDING={}

def database():
    db=sqlite3.connect(DB);db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, imported_at TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE, record_no INTEGER NOT NULL, conversation TEXT NOT NULL, speaker TEXT NOT NULL, stamp TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, offset_unit TEXT NOT NULL DEFAULT "unicode code points", reference TEXT NOT NULL)')
    db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS record_search USING fts5(record_id UNINDEXED, source_id UNINDEXED, content, speaker, conversation)')
    return db

def text_value(value):
    if value is None:return ''
    return value if isinstance(value,str) else str(value)

def normalize_message(row,index,start,end,conversation=''):
    if not isinstance(row,dict):raise ValueError(f'Record {index} must be a JSON object.')
    content=next((row[k] for k in ('content','text','message') if isinstance(row.get(k),str)),None)
    if content is None:raise ValueError(f'Record {index} has no text, content, or message field.')
    cid=row.get('conversation_id')
    if cid is None:cid=row.get('thread_id')
    if cid is not None and not isinstance(cid,str):raise ValueError(f'Record {index} conversation IDs must be strings.')
    return {'record_no':index,'conversation':cid or conversation,'speaker':text_value(row.get('speaker') or row.get('author') or row.get('name')),'stamp':text_value(row.get('timestamp') or row.get('created_at') or row.get('time')),'role':text_value(row.get('role')),'content':content,'start_offset':start,'end_offset':end,'reference':f'message {index}'}

def parse_json_records(raw):
    def no_duplicate_keys(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError(f'Duplicate JSON key {key!r}; source location would be ambiguous.')
            result[key]=value
        return result
    try:obj=json.loads(raw,object_pairs_hook=no_duplicate_keys)
    except json.JSONDecodeError as e:raise ValueError(f'Invalid JSON at line {e.lineno}, column {e.colno}.') from e
    decoder=json.JSONDecoder()
    if isinstance(obj,dict) and isinstance(obj.get('messages'),list):
        rows=obj['messages'];cursor=raw.find('{')+1;array_start=-1
        while cursor<len(raw):
            while cursor<len(raw) and (raw[cursor].isspace() or raw[cursor]==','):cursor+=1
            if cursor>=len(raw) or raw[cursor]=='}':break
            key,key_end=decoder.raw_decode(raw,cursor);cursor=key_end
            while cursor<len(raw) and (raw[cursor].isspace() or raw[cursor]==':'):cursor+=1
            value_start=cursor;value,value_end=decoder.raw_decode(raw,value_start)
            if key=='messages':array_start=value_start;break
            cursor=value_end
        if array_start<0:raise ValueError('Could not locate the top-level messages array in JSON.')
    elif isinstance(obj,list):rows=obj;array_start=raw.find('[')
    else:raise ValueError('JSON must be a message array or an object with a messages array.')
    if not rows:raise ValueError('No messages found in JSON.')
    result=[];cursor=array_start+1
    for i,row in enumerate(rows,1):
        while cursor<len(raw) and (raw[cursor].isspace() or raw[cursor]==','):cursor+=1
        pos=cursor
        try:_,end=decoder.raw_decode(raw,pos)
        except json.JSONDecodeError as e:raise ValueError(f'Could not map message {i} to its original JSON span.') from e
        result.append(normalize_message(row,i,pos,end));cursor=end
    return result

def parse_jsonl_records(raw):
    result=[];offset=0
    for line_no,line in enumerate(raw.splitlines(keepends=True),1):
        stripped=line.strip()
        if not stripped:offset+=len(line);continue
        try:row=json.loads(stripped)
        except json.JSONDecodeError as e:raise ValueError(f'Invalid JSONL on line {line_no}, column {e.colno}.') from e
        start=offset+len(line)-len(line.lstrip());end=offset+len(line.rstrip('\r\n'))
        result.append(normalize_message(row,len(result)+1,start,end));offset+=len(line)
    if not result:raise ValueError('No messages found in JSONL.')
    return result

def parse_csv_records(raw):
    lines=raw.splitlines(keepends=True)
    if not lines:raise ValueError('CSV file is empty.')
    try:reader=csv.DictReader(io.StringIO(raw,newline=''))
    except csv.Error as e:raise ValueError(f'Invalid CSV: {e}') from e
    if not reader.fieldnames:raise ValueError('CSV needs a header row.')
    fields={str(k).strip().lower():k for k in reader.fieldnames if k}
    if not any(k in fields for k in ('content','text','message')):raise ValueError('CSV needs a content, text, or message column.')
    try:
        header_lines=reader.line_num;offset=sum(map(len,lines[:header_lines]));result=[]
        for row in reader:
            consumed=reader.line_num-(header_lines if not result else previous_line)
            start=offset;end=sum(map(len,lines[:reader.line_num]))
            content_key=next(fields[k] for k in ('content','text','message') if k in fields)
            def value(*names):
                key=next((fields[n] for n in names if n in fields),None)
                return row.get(key) if key is not None else None
            msg={'content':text_value(row.get(content_key)),'speaker':value('speaker','author','name'),'role':value('role'),'timestamp':value('timestamp','created_at','time'),'conversation_id':value('conversation_id','thread_id')}
            normalized=normalize_message(msg,len(result)+1,start,end);result.append(normalized)
            offset=end;previous_line=reader.line_num
    except csv.Error as e:raise ValueError(f'Invalid CSV: {e}') from e
    if not result:raise ValueError('No messages found in CSV.')
    return result

def parse_text_records(raw):
    result=[];offset=0
    for line_no,line in enumerate(raw.splitlines(keepends=True),1):
        content=line.rstrip('\r\n')
        if content.strip():result.append({'record_no':len(result)+1,'conversation':'','speaker':'','stamp':'','role':'','content':content,'start_offset':offset,'end_offset':offset+len(content),'reference':f'line {line_no}'})
        offset+=len(line)
    if not result:raise ValueError('No text lines found.')
    return result

def parse_file(name,raw,redact=()):
    if not isinstance(name,str) or not isinstance(raw,str):raise ValueError('File name and contents must be text.')
    if len(raw.encode('utf-8'))>MAX_BYTES:raise ValueError('Import is larger than 5 MB.')
    ext=Path(name).suffix.lower()
    adapters={'.json':parse_json_records,'.jsonl':parse_jsonl_records,'.ndjson':parse_jsonl_records,'.csv':parse_csv_records,'.txt':parse_text_records}
    if ext not in adapters:raise ValueError(f'Unsupported format {ext or "(no extension)"}. Supported formats: JSON, JSONL, CSV, and TXT.')
    records=adapters[ext](raw)
    redact_set=set(redact)
    invalid=redact_set-{'content','speaker','timestamp','role','conversation'}
    if invalid:raise ValueError('Unknown redaction field.')
    for rec in records:
        for field in redact_set:
            key={'timestamp':'stamp'}.get(field,field)
            if rec.get(key):rec[key]='[REDACTED]'
    return ext.lstrip('.'),records

def digest_for(fmt,raw):return hashlib.sha256((fmt+'\0'+raw).encode('utf-8')).hexdigest()

def seed():
    db=database()
    if db.execute('SELECT 1 FROM sources LIMIT 1').fetchone():db.close();return
    fixture='''{"conversation_id":"thread-blue","role":"user","timestamp":"2026-10-02T09:00:00Z","content":"The launch date is October 12."}
{"conversation_id":"thread-blue","role":"assistant","timestamp":"2026-10-02T09:00:06Z","content":"October 12 is the launch date."}
{"conversation_id":"thread-green","role":"user","timestamp":"2026-10-05T11:00:00Z","content":"Correction: the launch date moved to October 19."}
{"conversation_id":"thread-green","role":"assistant","timestamp":"2026-10-05T11:00:05Z","content":"I will use October 19 as the updated date."}
'''
    fmt,rows=parse_file('synthetic-overlap.jsonl',fixture);sid=secrets.token_hex(8);digest=digest_for(fmt,fixture)
    db.execute('INSERT INTO sources VALUES (?,?,?,?,?)',(sid,'Synthetic overlap and correction sample','jsonl',digest,datetime.now(timezone.utc).isoformat()))
    for rec in rows:
        rid=secrets.token_hex(8);db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(rid,sid,rec['record_no'],rec['conversation'],rec['speaker'],rec['stamp'],rec['role'],rec['content'],rec['start_offset'],rec['end_offset'],'unicode code points',rec['reference']))
        db.execute('INSERT INTO record_search VALUES (?,?,?,?,?)',(rid,sid,rec['content'],rec['speaker'],rec['conversation']))
    db.commit();db.close()

HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0e12"><title>Chat Search</title><link rel="stylesheet" href="/style.css"></head><body><header><a class="brand" href="/" aria-label="Chat Search home"><span class="mark">C</span><span>Chat<br>Search</span></a><nav><button id="libraryTab" class="nav active">Library</button><button id="settingsTab" class="nav">Settings</button></nav><span class="local"><i></i> Stored locally</span></header><main><section id="library"><div class="topline"><div><h1>Chat Search</h1><p class="intro">Search saved chats and open the source message behind each result.</p></div><button id="importBtn" class="primary">Import files <span>+</span></button></div><section class="searchband"><label for="query">Search your notebook</label><div class="searchrow"><input id="query" type="search" placeholder="Search words, people, or threads"><button id="searchBtn" class="primary">Search</button></div><p class="small-muted">Searches run against content kept on this device.</p></section><div class="layout"><section class="result-pane"><div class="pane-head"><div><span class="label">RESULTS</span><h2 id="resultTitle">Recent messages</h2></div><span class="count" id="resultCount">0 messages</span></div><div id="results"></div></section><aside class="source-pane"><span class="label">SOURCE INSPECTOR</span><h2 id="sourceTitle">Choose a result</h2><p class="small-muted" id="sourceHint">Every result points to its imported source.</p><div id="sourceBody" class="source-body">Select a result to inspect its original message, source file, and location.</div></aside></div><p class="footnote">Synthetic sample data is labeled. Imports stay on this device.</p></section><section id="settings" hidden><button id="backBtn" class="text-button">← Back to library</button><h1>Settings</h1><p class="intro">Manage the local notebook and supported import formats.</p><div class="settings-block"><h2>Formats</h2><p>Supported: JSON message arrays, JSON objects with a messages array, JSONL or NDJSON message rows, CSV with a content, text, or message column, and plain TXT lines. ZIP and other formats are not supported.</p><h2>Local data</h2><p>SQLite data stays in your local user data folder. No account or remote connection is used.</p><button id="wipeBtn" class="danger">Delete notebook data</button></div></section></main><dialog id="importDialog"><form method="dialog"><div class="dialog-head"><h2>Preview import</h2><button class="close" value="cancel" aria-label="Close">×</button></div><label for="fileInput">Choose a JSON, JSONL, CSV, or TXT file</label><input id="fileInput" type="file" accept=".json,.jsonl,.ndjson,.csv,.txt"><fieldset><legend>Redact before saving</legend><label><input type="checkbox" value="content"> Message text</label><label><input type="checkbox" value="speaker"> Speaker</label><label><input type="checkbox" value="timestamp"> Timestamp</label><label><input type="checkbox" value="role"> Role</label><label><input type="checkbox" value="conversation"> Conversation ID</label></fieldset><button id="previewBtn" type="button" class="secondary">Preview</button><div id="preview" class="preview"></div><div class="dialog-actions"><button class="secondary" value="cancel">Cancel</button><button id="commitBtn" class="primary" type="button" disabled>Save import</button></div></form></dialog><div id="toast" role="status" aria-live="polite"></div><script src="/app.js" defer></script></body></html>'''

CSS=r''':root{color-scheme:dark;--bg:#0b0e12;--surface:#11171d;--surface2:#171f26;--line:#29333d;--text:#f4f6f8;--muted:#a5afb8;--dim:#7e8a94;--orange:#f4a879;--blue:#9ebeff;--font:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--display:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse 60% 36% at 78% -10%,#26333d 0,transparent 70%),var(--bg);color:var(--text);font:16px/1.5 var(--font);min-height:100vh}button,input{font:inherit}button{cursor:pointer}header{height:74px;border-bottom:1px solid #ffffff12;display:flex;align-items:center;padding:0 max(26px,calc((100vw - 1400px)/2));gap:48px}.brand{color:var(--text);display:flex;align-items:center;gap:11px;text-decoration:none;font:700 13px/1.05 var(--display);letter-spacing:-.03em}.mark{width:32px;height:32px;display:grid;place-items:center;border:1px solid #9a6347;border-radius:10px;color:#ffc39d;font-weight:800}.nav{border:0;background:transparent;color:var(--muted);padding:10px 15px;border-radius:9px}.nav.active,.nav:hover{background:#ffffff0b;color:white}.local{margin-left:auto;color:var(--muted);font-size:12px;display:flex;gap:9px;align-items:center}.local i{width:7px;height:7px;border-radius:50%;background:#9de2be;box-shadow:0 0 12px #9de2be88}main{max-width:1400px;margin:auto;padding:54px 36px}.topline{display:flex;justify-content:space-between;align-items:end;margin-bottom:34px}h1{font:750 clamp(36px,4.3vw,58px)/1.04 var(--display);letter-spacing:-.05em;margin:0 0 12px}h2,p{margin-top:0}.intro{color:#b0b8c0;max-width:62ch;margin:0;font-size:16px}.primary,.secondary,.danger{min-height:44px;padding:0 16px;border-radius:9px;border:1px solid transparent;font-size:13px;font-weight:700;transition:transform .2s,background .2s}.primary{background:#f1a878;color:#291307}.primary:hover{background:#ffc39d;transform:translateY(-1px)}.primary span{font-size:18px;margin-left:8px}.searchband{padding:19px 21px 14px;background:#12181e;border:1px solid var(--line);border-radius:13px;margin-bottom:24px}.searchband>label,.dialog label{display:block;color:#b7c0c7;font-size:12px;font-weight:600;margin-bottom:7px}.searchrow{display:flex;gap:10px}.searchrow input{flex:1;min-width:0;background:#0b1015;border:1px solid #35404a;border-radius:8px;color:white;padding:10px 13px}.small-muted{color:#929da6;font-size:12px;margin:8px 0 0}.layout{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(310px,.8fr);gap:22px;align-items:start}.result-pane,.source-pane{background:#10151a;border-top:1px solid #34404a;padding:19px 19px 20px;min-width:0}.pane-head{display:flex;justify-content:space-between;align-items:start;padding-bottom:16px;border-bottom:1px solid #ffffff12}.label{color:#9da9b2;font-size:9px;letter-spacing:.15em;font-weight:700}.pane-head h2,.source-pane h2{font:600 19px var(--display);letter-spacing:-.03em;margin:6px 0 0}.count{font-size:11px;color:#9ba6af}.result{padding:16px 5px;border-bottom:1px solid #ffffff12;cursor:pointer;transition:background .2s,padding .2s}.result:hover,.result.active{background:#ffffff08;padding-left:10px;padding-right:10px}.result-meta{display:flex;gap:9px;flex-wrap:wrap;font-size:10px;color:#9aa7b0;margin-bottom:7px}.result-meta .source{color:#bdd0f3}.result p{font-size:14px;color:#e7ebee;margin:0}.source-pane{position:sticky;top:18px;border:1px solid #28343d;border-radius:12px;background:#121920}.source-pane h2{margin:7px 0 6px}.source-body{margin-top:16px;padding:15px;border-radius:9px;background:#0b1116;border:1px solid #293740;color:#e2e7ea;font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}.source-ref{font-size:11px;color:#a7c5ff;margin-top:14px}.footnote{font-size:11px;color:#86929a;margin:24px 0}.text-button{background:transparent;border:0;color:#b8d0ff;text-decoration:underline;text-underline-offset:3px}.settings-block{margin-top:30px;padding:24px 0;border-block:1px solid var(--line)}.settings-block h2{font:600 18px var(--display);margin:22px 0 8px}.settings-block p{color:#a7b0b8;font-size:14px;max-width:70ch}.danger{background:#311b19;color:#ffbeb4;border:1px solid #59312d}.dialog-head{display:flex;justify-content:space-between;align-items:center}.dialog-head h2{font:700 22px var(--display)}.close{border:0;background:none;color:#ddd;font-size:24px}dialog{width:min(700px,calc(100vw - 24px));background:#151b21;color:white;border:1px solid #43505a;border-radius:14px;padding:25px;box-shadow:0 28px 90px #000a}dialog::backdrop{background:#020406c9;backdrop-filter:blur(5px)}#fileInput{color:#e1e6e9;font-size:13px;max-width:100%}fieldset{border:1px solid #35404a;border-radius:9px;margin:18px 0;padding:12px;display:flex;gap:16px;flex-wrap:wrap}fieldset legend{font-size:12px;color:#c2cbd2}fieldset label{display:flex!important;gap:6px;align-items:center;font-size:12px!important;margin:0!important}fieldset input{accent-color:#f1a878}.preview{max-height:260px;overflow:auto;color:#bdc6cd;font-size:12px}.preview-row{padding:8px 0;border-bottom:1px solid #ffffff10}.dialog-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:20px}.secondary{background:#222a31;color:#e3e8eb;border-color:#414b53}#toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,10px);opacity:0;background:#edf8f0;color:#102016;padding:10px 16px;border-radius:9px;transition:.2s;pointer-events:none}#toast.show{opacity:1;transform:translate(-50%,0)}:focus-visible{outline:3px solid #a9c8ff!important;outline-offset:3px}::selection{background:#f4a879;color:#241307}*{scrollbar-width:thin;scrollbar-color:#46515a #11171d}@media(max-width:850px){main{padding:38px 22px}.layout{grid-template-columns:1fr}.source-pane{position:static}.result-pane{order:0}.source-pane{order:1}}@media(max-width:560px){header{height:64px;padding:0 16px;gap:10px}.brand span:last-child{display:none}.nav{padding:9px;font-size:12px}.local{font-size:0}.local i{width:8px;height:8px}main{padding:30px 14px}.topline{display:block}.topline h1{font-size:clamp(33px,10vw,40px)}.topline .primary{width:100%;margin-top:20px}.searchband{padding:15px}.searchrow{gap:7px}.searchrow .primary{padding:0 12px}.layout{gap:14px}.result-pane,.source-pane{padding:16px 13px}.result{padding:13px 3px}.result p{font-size:13px}.dialog-actions button{flex:1}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}'''
CSS += '.result{display:block;width:100%;text-align:left;background:transparent;border:0;color:inherit;font:inherit}.result-copy{display:block;font-size:14px;color:#e7ebee;margin:0}.result:focus-visible{position:relative;z-index:1}'

APPJS=r'''const $=s=>document.querySelector(s);let pending=null,allResults=[];async function api(path,method='GET',body){const o={method,headers:{'X-Local-Action':TOKEN_PLACEHOLDER}};if(body!==undefined){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}const r=await fetch(path,o);if(!r.ok){const e=await r.json();throw Error(e.error||'Request failed.')}return r.status===204?null:r.json()}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function flash(s){const t=$('#toast');t.textContent=s;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2400)}async function refresh(){const d=await api('/api/sources');$('#resultCount').textContent=`${d.count} ${d.count===1?'message':'messages'}`;$('#results').innerHTML=d.records.map(r=>row(r)).join('')||'<p class="small-muted">No messages yet. Import a supported export to begin.</p>';allResults=d.records}function row(r){return `<article class="result" data-id="${esc(r.id)}"><div class="result-meta"><span class="source">${esc(r.source_name)}</span><span>${esc(r.reference)}</span><span>${esc(r.stamp||r.speaker||r.role||'message')}</span></div><p>${esc(r.content)}</p></article>`}$('#results').addEventListener('click',async e=>{const item=e.target.closest('.result');if(!item)return;try{const d=await api('/api/records/'+encodeURIComponent(item.dataset.id));document.querySelectorAll('.result.active').forEach(x=>x.classList.remove('active'));item.classList.add('active');$('#sourceTitle').textContent=d.source.name;$('#sourceHint').textContent=`${d.record.reference} · ${d.record.speaker||d.record.role||'unknown speaker'}`;$('#sourceBody').textContent=d.record.content;$('#sourceBody').insertAdjacentHTML('afterend',`<p class="source-ref">${esc(d.source.format.toUpperCase())} · source characters ${d.record.start_offset}–${d.record.end_offset} · ${esc(d.record.offset_unit)}</p>`);document.querySelectorAll('.source-ref').forEach((x,i)=>{if(i<document.querySelectorAll('.source-ref').length-1)x.remove()})}catch(err){flash(err.message)}});async function search(){const q=$('#query').value.trim();const d=await api('/api/search?q='+encodeURIComponent(q));$('#resultTitle').textContent=q?`Matches for “${q}”`:'Recent messages';$('#resultCount').textContent=`${d.records.length} ${d.records.length===1?'message':'messages'}`;$('#results').innerHTML=d.records.map(r=>row(r)).join('')||'<p class="small-muted">No matches. Try another search.</p>'}$('#searchBtn').onclick=()=>search().catch(e=>flash(e.message));$('#query').addEventListener('keydown',e=>{if(e.key==='Enter')search().catch(err=>flash(err.message))});$('#importBtn').onclick=()=>{$('#importDialog').showModal();pending=null;$('#preview').innerHTML='';$('#commitBtn').disabled=true};$('#previewBtn').onclick=async()=>{const file=$('#fileInput').files[0];if(!file){flash('Choose a file first.');return}try{const text=await file.text(),redact=[...document.querySelectorAll('fieldset input:checked')].map(x=>x.value);pending={name:file.name,text,redact};const d=await api('/api/preview','POST',pending);pending.preview_id=d.preview_id;$('#preview').innerHTML=`<p>${d.records.length} messages · ${esc(d.format.toUpperCase())} · ${d.redacted_fields.length?`Redacted: ${esc(d.redacted_fields.join(', '))}`:'No fields selected for redaction'}</p>`+d.records.slice(0,8).map(r=>`<div class="preview-row"><strong>${esc(r.speaker||r.role||'Message')}</strong> · ${esc(r.reference)}<br>${esc(r.content)}</div>`).join('');$('#commitBtn').disabled=false}catch(e){pending=null;$('#preview').innerHTML=`<p role="alert">${esc(e.message)}</p>`;$('#commitBtn').disabled=true}};$('#commitBtn').onclick=async()=>{if(!pending)return;try{const d=await api('/api/import','POST',pending);$('#importDialog').close();await refresh();flash(d.duplicate?'This file was already imported.':'Import saved.');}catch(e){flash(e.message)}};function settings(on){$('#library').hidden=on;$('#settings').hidden=!on;$('#settingsTab').classList.toggle('active',on);$('#libraryTab').classList.toggle('active',!on)}$('#settingsTab').onclick=()=>settings(true);$('#libraryTab').onclick=()=>settings(false);$('#backBtn').onclick=()=>settings(false);$('#wipeBtn').onclick=async()=>{if(!confirm('Delete all imported sources and search records?'))return;await api('/api/data','DELETE');await refresh();$('#sourceTitle').textContent='Choose a result';$('#sourceHint').textContent='Every result points to its imported source.';$('#sourceBody').textContent='Select a result to inspect its original message, source file, and location.';settings(false);flash('Notebook data deleted.')};refresh().catch(e=>flash(e.message));'''
APPJS += r'''$('#deleteSource').onclick=async()=>{const active=document.querySelector('.result.active');if(!active)return;try{const record=await api('/api/records/'+encodeURIComponent(active.dataset.id));if(!confirm(`Delete source “${record.source.name}” and all its search records?`))return;await api('/api/sources/'+encodeURIComponent(record.source.id),'DELETE');$('#sourceTitle').textContent='Choose a result';$('#sourceHint').textContent='Every result points to its imported source.';$('#sourceBody').textContent='Select a result to inspect its original message, source file, and location.';$('#deleteSource').disabled=true;await refresh();flash('Source and its records deleted.')}catch(err){flash(err.message)}};$('#results').addEventListener('click',e=>{if(e.target.closest('.result'))$('#deleteSource').disabled=false});document.querySelectorAll('fieldset input').forEach(x=>x.addEventListener('change',()=>{if(pending){pending=null;$('#commitBtn').disabled=true;$('#preview').textContent='Redaction choices changed. Preview again before saving.'}}));'''
APPJS += r'''$('#fileInput').addEventListener('change',()=>{pending=null;$('#commitBtn').disabled=true;$('#preview').textContent='File changed. Preview it before saving.'});'''
APPJS += r'''row=function(r){return `<button type="button" class="result" data-id="${esc(r.id)}"><span class="result-meta"><span class="source">${esc(r.source_name)}</span><span>${esc(r.reference)}</span><span>${esc(r.stamp||r.speaker||r.role||'message')}</span></span><span class="result-copy">${esc(r.content)}</span></button>`};'''

class Handler(BaseHTTPRequestHandler):
    server_version='EvidenceNotebook/1.0'
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
    def log_message(self,fmt,*args):super().log_message(fmt,*args)
    def safe(self):return self.headers.get('Host','') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
    def send(self,status,data=b'',ctype='application/json; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def json(self,status,obj):self.send(status,json.dumps(obj,ensure_ascii=False).encode())
    def read_json(self):
        n=int(self.headers.get('Content-Length','0'))
        if not 1<=n<=MAX_BYTES:raise ValueError('Request is empty or over 5 MB.')
        obj=json.loads(self.rfile.read(n))
        if not isinstance(obj,dict):raise ValueError('Expected a JSON object.')
        return obj
    def authorized(self):
        origin=self.headers.get('Origin');allowed=(f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}')
        return self.safe() and self.headers.get('X-Local-Action')==TOKEN and (origin is None or origin in allowed)
    def do_GET(self):
        if not self.safe():return self.send(403,b'Forbidden','text/plain')
        path=urlparse(self.path).path
        if path=='/':
            page=HTML.replace('<div id="sourceBody" class="source-body">','<button id="deleteSource" class="danger" type="button" disabled>Delete selected source</button><div id="sourceBody" class="source-body">')
            if 'desktop=1' in self.path:page=page.replace('</head>','<link rel="stylesheet" href="/desktop.css"></head>')
            return self.send(200,page.encode(),'text/html; charset=utf-8')
        if path=='/desktop.css':return self.send(200,(ROOT/'desktop.css').read_bytes(),'text/css; charset=utf-8')
        if path=='/style.css':return self.send(200,CSS.encode(),'text/css; charset=utf-8')
        if path=='/app.js':return self.send(200,APPJS.replace('TOKEN_PLACEHOLDER',json.dumps(TOKEN)).encode(),'application/javascript; charset=utf-8')
        if path=='/api/sources':
            db=database();sources=[dict(x) for x in db.execute('SELECT id,name,format,digest,imported_at FROM sources ORDER BY rowid DESC')];records=[dict(x) for x in db.execute('SELECT r.*,s.name AS source_name FROM records r JOIN sources s ON s.id=r.source_id ORDER BY r.rowid DESC LIMIT 200')];count=db.execute('SELECT COUNT(*) FROM records').fetchone()[0];db.close();return self.json(200,{'sources':sources,'records':records,'count':count})
        if path=='/api/search':
            q=parse_qs(urlparse(self.path).query).get('q',[''])[0].strip();db=database()
            if q:
                try:rows=db.execute('SELECT r.*,s.name AS source_name FROM record_search f JOIN records r ON r.id=f.record_id JOIN sources s ON s.id=r.source_id WHERE record_search MATCH ? ORDER BY rank LIMIT 100',(q,)).fetchall()
                except sqlite3.OperationalError:rows=[]
            else:rows=db.execute('SELECT r.*,s.name AS source_name FROM records r JOIN sources s ON s.id=r.source_id ORDER BY r.rowid DESC LIMIT 100').fetchall()
            db.close();return self.json(200,{'records':[dict(x) for x in rows]})
        if path.startswith('/api/records/'):
            rid=path.rsplit('/',1)[-1];db=database();row=db.execute('SELECT r.*,s.id AS src_id,s.name,s.format,s.imported_at FROM records r JOIN sources s ON s.id=r.source_id WHERE r.id=?',(rid,)).fetchone();db.close()
            if not row:return self.json(404,{'error':'Source message not found.'})
            rec={k:row[k] for k in ('id','record_no','conversation','speaker','stamp','role','content','start_offset','end_offset','offset_unit','reference')};src={k:row[k] for k in ('src_id','name','format','imported_at')};src['id']=src.pop('src_id');return self.json(200,{'record':rec,'source':src})
        return self.send(404,b'Not found','text/plain')
    def do_POST(self):
        if not self.authorized():return self.send(403,b'Forbidden','text/plain')
        try:
            obj=self.read_json();name=obj.get('name');raw=obj.get('text');redact=obj.get('redact',[])
            if not isinstance(name,str) or not isinstance(raw,str) or not isinstance(redact,list) or any(not isinstance(x,str) for x in redact):raise ValueError('File name, file text, and redaction fields have invalid types.')
            name=Path(name).name
            if not name or len(name)>180:raise ValueError('File name is empty or too long.')
            fmt,records=parse_file(name,raw,redact);digest=digest_for(fmt,raw)
            if urlparse(self.path).path=='/api/preview':
                now=datetime.now(timezone.utc).timestamp()
                for key,value in list(PENDING.items()):
                    if value[4]<now:del PENDING[key]
                while len(PENDING)>=12:PENDING.pop(next(iter(PENDING)))
                preview_id=secrets.token_urlsafe(18);PENDING[preview_id]=(fmt,records,digest,name,now+600,tuple(sorted(set(redact))));return self.json(200,{'preview_id':preview_id,'format':fmt,'records':records,'redacted_fields':redact})
            if urlparse(self.path).path!='/api/import':return self.send(404,b'Not found','text/plain')
            preview_id=obj.get('preview_id');pending=PENDING.pop(preview_id,None) if isinstance(preview_id,str) else None
            if not pending or pending[4]<datetime.now(timezone.utc).timestamp() or pending[2]!=digest or pending[3]!=name or pending[5]!=tuple(sorted(set(redact))):raise ValueError('Preview expired or contents or redaction choices changed. Preview the file again.')
            fmt,records=pending[0],pending[1]
            db=database()
            if db.execute('SELECT id FROM sources WHERE digest=?',(digest,)).fetchone():db.close();return self.json(200,{'duplicate':True})
            sid=secrets.token_hex(8)
            try:
                db.execute('BEGIN IMMEDIATE');db.execute('INSERT INTO sources VALUES (?,?,?,?,?)',(sid,name,fmt,digest,datetime.now(timezone.utc).isoformat()))
                for rec in records:
                    rid=secrets.token_hex(8);db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(rid,sid,rec['record_no'],rec['conversation'],rec['speaker'],rec['stamp'],rec['role'],rec['content'],rec['start_offset'],rec['end_offset'],'unicode code points',rec['reference']))
                    db.execute('INSERT INTO record_search VALUES (?,?,?,?,?)',(rid,sid,rec['content'],rec['speaker'],rec['conversation']))
                db.commit()
            except Exception:db.rollback();db.close();raise
            db.close();return self.json(201,{'source_id':sid,'records':len(records),'duplicate':False})
        except (ValueError,TypeError,json.JSONDecodeError,sqlite3.Error) as e:return self.json(400,{'error':str(e)})
    def do_DELETE(self):
        if not self.authorized():return self.send(403,b'Forbidden','text/plain')
        path=urlparse(self.path).path;db=database()
        if path=='/api/data':db.execute('DELETE FROM record_search');db.execute('DELETE FROM sources');db.commit();db.close();return self.send(204)
        m=re.fullmatch(r'/api/sources/([a-f0-9]{16})',path)
        if not m:db.close();return self.send(404,b'Not found','text/plain')
        cur=db.execute('SELECT 1 FROM sources WHERE id=?',(m.group(1),)).fetchone()
        if not cur:db.close();return self.json(404,{'error':'Source not found.'})
        db.execute('DELETE FROM record_search WHERE source_id=?',(m.group(1),));db.execute('DELETE FROM sources WHERE id=?',(m.group(1),));db.commit();db.close();return self.send(204)

def make_server(port=0):
    return _make_desktop_server(Handler, port=port)

def run(port=8345):
    seed();server=make_server(port=port);print(f'Chat Search listening at http://127.0.0.1:{server.server_port}',flush=True);server.serve_forever()
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8345);run(p.parse_args().port)

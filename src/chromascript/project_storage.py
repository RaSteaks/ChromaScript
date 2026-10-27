"""Portable project folders; the workspace database keeps only routing and global state."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal
from uuid import uuid4

from .errors import ChromaError
from .operations import operation

if TYPE_CHECKING:
    from .store import Store

LAYOUT = "project-folders-v1"
DATABASE = "project.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, hash TEXT NOT NULL, job_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_notifications (job_id TEXT PRIMARY KEY, token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_routes (logical TEXT PRIMARY KEY, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_routes (job_id TEXT PRIMARY KEY, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS view_hashes (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
"""
CATALOG = """
CREATE TABLE IF NOT EXISTS workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_catalog (id TEXT PRIMARY KEY, directory TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS record_index (kind TEXT NOT NULL,id TEXT NOT NULL,project_id TEXT NOT NULL,PRIMARY KEY(kind,id));
"""


def label(name: str, identity: str) -> str:
    # Finder names remain readable while the UUID, not a mutable filename, owns identity.
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', name).strip(' .') or '未命名项目'
    clean = clean.encode('utf-8')[:180].decode('utf-8', errors='ignore')
    return f"{clean}__{identity[:8]}"


def safe_path(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or '..' in value.parts or not value.parts:
        raise ChromaError('project_path', '项目相对路径无效。')
    target = root / value
    if root.is_symlink() or any(p.is_symlink() for p in [target, *target.parents] if p == root or root in p.parents):
        raise ChromaError('project_path', '项目管理文件不能经过符号链接。')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ChromaError('project_path', '项目路径越界。')
    return target


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(content, encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'


def project_dirs(store: Store) -> dict[str, Path]:
    """Discover moved/restored folders by manifest ID; never recreate a missing database."""
    with sqlite3.connect(store.path) as db:
        catalog = dict(db.execute('SELECT id,directory FROM project_catalog'))
    found: dict[str, Path] = {}
    for section in ('项目', '已归档'):
        parent = store.root / section
        if not parent.is_dir() or parent.is_symlink():
            continue
        for directory in parent.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest = directory / 'project.json'
            database = directory / DATABASE
            if not manifest.is_file() or not database.is_file() or manifest.is_symlink() or database.is_symlink():
                continue
            try:
                data = json.loads(manifest.read_text())
                pid = data['project_id']
                if data.get('format') != LAYOUT or not re.fullmatch('[a-f0-9]{32}', pid):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if pid in found:
                raise ChromaError('duplicate_project_folder', '发现相同项目 ID 的两个目录；请将备份移出“项目”和“已归档”目录。', project_id=pid)
            found[pid] = directory
    # Re-registration is an index update, not an import that mutates project content.
    for pid, directory in found.items():
        relative = str(directory.relative_to(store.root))
        if catalog.get(pid) != relative:
            with sqlite3.connect(f'file:{directory / DATABASE}?mode=ro', uri=True) as db:
                row = db.execute("SELECT data FROM records WHERE kind='project' AND id=?", (pid,)).fetchone()
                if not row:
                    raise ChromaError('project_identity', '项目说明与项目数据库身份不一致。')
            with sqlite3.connect(store.path) as db:
                db.execute('INSERT INTO project_catalog VALUES (?,?) ON CONFLICT(id) DO UPDATE SET directory=excluded.directory', (pid, relative))
    return found


def prepare_project(store: Store, project: dict[str, Any]) -> Path:
    known = project_dirs(store)
    if project['id'] in known:
        return known[project['id']]
    with sqlite3.connect(store.path) as db:
        if db.execute('SELECT 1 FROM project_catalog WHERE id=?', (project['id'],)).fetchone():
            raise ChromaError('project_offline', '项目文件夹缺失，请恢复或重新登记；不会创建空项目覆盖原数据。')
    directory = store.root / ('已归档' if project.get('archived') else '项目') / label(project['name'], project['id'])
    directory.mkdir(parents=True, exist_ok=False)
    try:
        with sqlite3.connect(directory / DATABASE) as db:
            db.executescript(SCHEMA)
        os.chmod(directory / DATABASE, 0o600)
        atomic_text(directory / 'project.json', encode({'format': LAYOUT, 'project_id': project['id'], 'name': project['name']}))
        with sqlite3.connect(store.path) as db:
            db.execute('INSERT INTO project_catalog VALUES (?,?)', (project['id'], str(directory.relative_to(store.root))))
    except BaseException:
        shutil.rmtree(directory)
        raise
    return directory


def update_project_record(store: Store, project_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Move first, then commit metadata; a rejected update restores the original folder."""
    directory = store.project_directory(project_id)
    with sqlite3.connect(directory / DATABASE) as db:
        rows = [json.loads(row[0]) for row in db.execute('SELECT data FROM records')]
    if any(v.get('status') in ('queued', 'running', 'pending') for v in rows):
        raise ChromaError('project_busy', '项目有任务正在进行，不能移动目录。')
    project = store.get('project', project_id)
    updated = change(dict(project))
    target = store.root / ('已归档' if updated['archived'] else '项目') / label(updated['name'], project_id)
    moved = directory != target
    if moved:
        if target.exists() or target.is_symlink():
            raise ChromaError('project_folder_collision', '目标目录已存在，未覆盖。')
        target.parent.mkdir(parents=True, exist_ok=True)
        directory.rename(target)
    try:
        def checked(current: dict[str, Any]) -> dict[str, Any]:
            if current != project:
                raise ChromaError('revision_conflict', '项目资料已变更，请重新读取后操作。')
            return updated
        # Directory discovery resolves the unchanged project ID at its new path.
        return store.update('project', project_id, checked)
    except BaseException:
        if moved and target.exists() and not directory.exists():
            target.rename(directory)
            with sqlite3.connect(store.path) as db:
                db.execute('UPDATE project_catalog SET directory=? WHERE id=?', (str(directory.relative_to(store.root)), project_id))
        raise


def remove_empty_project(store: Store, project_id: str) -> None:
    if not store.partitioned:
        return
    directory = store.project_directory(project_id)
    with sqlite3.connect(directory / DATABASE) as db:
        if db.execute("SELECT 1 FROM records WHERE kind='project' AND id=?", (project_id,)).fetchone():
            return
    # Only a task-created, never-committed container is disposable on failure.
    shutil.rmtree(directory)
    with sqlite3.connect(store.path) as db:
        db.execute('DELETE FROM project_catalog WHERE id=?', (project_id,))
        db.execute('DELETE FROM record_index WHERE project_id=?', (project_id,))


def route_target(logical: str, name: str | None = None) -> tuple[str, str]:
    parts = Path(logical).parts
    if len(parts) < 2 or Path(logical).is_absolute() or '..' in parts:
        raise ChromaError('project_path', '项目文件必须有类型和标识。')
    category, identity = parts[:2]
    base = {
        'audio': '音频/项目音频', 'audio-sources': '音频/原始素材',
        'music-sources': '音频/生成记录/配乐', 'speech-sources': '音频/生成记录/语音',
        'analysis-sources': '试听/分析记录', 'audio-reviews': '试听/审听意见',
        'auditions': '试听/候选片段', 'jobs': '.chromascript/任务',
        'subtitles': '字幕',
    }.get(category, '素材/历史文件/' + category)
    if category in ('audio-sources', 'subtitles'):
        target = base
    elif category == 'audio' and name:
        target = base + '/' + label(name, Path(identity).stem) + Path(identity).suffix
    else:
        target = base + '/' + identity
    return '/'.join(parts[:2]), target


def project_file(store: Store, pid: str, logical: str, name: str | None = None) -> Path:
    if not store.partitioned:
        return store.file(logical)
    directory = store.project_directory(pid)
    prefix, target = route_target(logical, name)
    with sqlite3.connect(directory / DATABASE) as db:
        row = db.execute('SELECT target FROM file_routes WHERE logical=?', (prefix,)).fetchone()
        if row:
            target = row[0]
        else:
            db.execute('INSERT INTO file_routes VALUES (?,?)', (prefix, target))
    suffix = Path(logical).parts[2:]
    return safe_path(directory, str(Path(target).joinpath(*suffix)))


def resolve_file(store: Store, logical: str) -> Path | None:
    for directory in project_dirs(store).values():
        with sqlite3.connect(f'file:{directory / DATABASE}?mode=ro', uri=True) as db:
            routes = db.execute('SELECT logical,target FROM file_routes ORDER BY length(logical) DESC').fetchall()
        for prefix, target in routes:
            if logical == prefix or logical.startswith(prefix + '/'):
                return safe_path(directory, target + logical[len(prefix):])
    # Job records exist before the dispatcher opens its log or workers create output.
    parts = Path(logical).parts
    if len(parts) >= 2 and parts[0] == 'jobs':
        job = store.get('job', parts[1])
        return project_file(store, job['project_id'], logical)
    return None


def logical_path(store: Store, physical: Path) -> str:
    """Persist stable logical references even when Finder folder names change."""
    if store.partitioned:
        for directory in project_dirs(store).values():
            if not physical.is_relative_to(directory):
                continue
            relative = str(physical.relative_to(directory))
            with sqlite3.connect(directory / DATABASE) as db:
                routes = db.execute('SELECT logical,target FROM file_routes ORDER BY length(target) DESC').fetchall()
            for logical, target in routes:
                if relative == target or relative.startswith(target + '/'):
                    return logical + relative[len(target):]
    return str(physical.relative_to(store.root))


def write_views(directory: Path) -> dict[str, Any]:
    """Human-readable projections are not a second database; preserve manual edits."""
    conflicts = []
    with sqlite3.connect(directory / DATABASE) as db:
        rows = {(k, i): json.loads(d) for k, i, d in db.execute('SELECT kind,id,data FROM records')}
        projects = [v for (k, i), v in rows.items() if k == 'project']
        if len(projects) != 1:
            return {'conflicts': []}
        project = projects[0]
        def view(relative: str, content: str) -> None:
            path = safe_path(directory, relative)
            previous = db.execute('SELECT sha256 FROM view_hashes WHERE path=?', (relative,)).fetchone()
            current = digest_file(path) if path.is_file() else None
            desired = hashlib.sha256(content.encode()).hexdigest()
            if current == desired:
                return
            if current and (not previous or current != previous[0]):
                conflicts.append(relative)
                return
            atomic_text(path, content)
            db.execute('INSERT INTO view_hashes VALUES (?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256', (relative, desired))
        # This identity file is service-owned; sources and notes use the conflict-preserving view writer.
        atomic_text(directory / 'project.json', encode({'format': LAYOUT, 'project_id': project['id'], 'name': project['name'], 'description': project.get('description', ''), 'archived': project['archived'], 'database': DATABASE, 'updated_at': project['updated_at']}))
        works = []
        for (kind, identity), record in rows.items():
            if kind not in ('scene', 'motion_scene'):
                continue
            history = 'revision' if kind == 'scene' else 'motion_revision'
            revision = rows.get((history, record['revision_id']))
            if not revision:
                continue
            spec = revision['scene']
            base = '作品/' + label(record['name'], identity)
            extension = 'wgsl' if kind == 'scene' else 'tsx'
            view(base + '/source.' + extension, spec['code'])
            view(base + '/配置.json', encode({'scene_id': identity, 'revision_id': record['revision_id'], 'kind': kind, 'spec': spec}))
            for name, content in spec.get('files', {}).items():
                view(base + '/附属文件/' + name, content)
            works.append(f"- {record['name']}（{identity[:8]}）")
        if ('audio_timeline', project['id']) in rows:
            view('音频/时间线.json', encode(rows['audio_timeline', project['id']]))
        if ('subtitle_timeline', project['id']) in rows:
            view('字幕/当前版本.json', encode(rows['subtitle_timeline', project['id']]))
        for (kind, identity), record in rows.items():
            if kind == 'audition_round':
                view('试听/' + label(record['title'], identity) + '/轮次.json', encode(record))
        outputs = [{'job_id': i, 'path': d['export_path'], 'completed': d.get('export_completed', False)} for (k, i), d in rows.items() if k == 'job' and d.get('export_path')]
        view('交付/外部交付位置.json', encode(outputs))
        view('项目说明.md', f"# {project['name']}\n\n{project.get('description', '')}\n\n项目 ID：`{project['id']}`\n\n状态：{'已归档' if project['archived'] else '使用中'}\n\n## 作品\n\n" + ('\n'.join(works) or '尚无作品。') + '\n\n## 文件管理\n\nproject.sqlite3 是本项目的数据源，保存全部修订、音频、试听和任务记录。project.json 用于识别项目；目录名可以更改，项目 ID 不变。\n\n作品源码、配置、时间线和轮次 JSON 是便于阅读的当前版本导出；直接修改这些文件不会自动覆盖数据库，刷新时会保留手动编辑并报告冲突。保存作品请使用现有 HTTP/MCP/CLI 修订接口。\n\n整体备份或搬移前请停止服务或使用 export_project_folder 生成一致性副本；运行中复制 SQLite 主文件可能漏掉 WAL 内容。外部交付文件须按“交付/外部交付位置.json”另外备份，缺失文件见迁移清单。凭据和全局模型配置不包含在项目文件夹内。\n')
        for relative in ('素材/图片', '素材/参考资料', '音频/原始素材', '音频/项目音频', '试听', '交付', '.chromascript'):
            safe_path(directory, relative).mkdir(parents=True, exist_ok=True)
    return {'conflicts': conflicts}


def after_commit(store: Store, pid: str, changes: list[tuple[str, str]]) -> None:
    try:
        with sqlite3.connect(store.path) as db:
            db.executemany('INSERT INTO record_index VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET project_id=excluded.project_id', [(k, i, pid) for k, i in changes])
        if any(k == 'job' for k, i in changes):
            directory = store.project_directory(pid)
            with sqlite3.connect(directory / DATABASE) as db:
                for kind, identity in changes:
                    if kind != 'job':
                        continue
                    row = db.execute("SELECT data FROM records WHERE kind='job' AND id=?", (identity,)).fetchone()
                    if not row:
                        continue
                    job = json.loads(row[0])
                    output = Path(job.get('export_path') or '/')
                    if output.is_relative_to(directory):
                        db.execute('INSERT INTO delivery_routes VALUES (?,?) ON CONFLICT(job_id) DO UPDATE SET target=excluded.target', (identity, str(output.relative_to(directory))))
        if any(k in ('project', 'scene', 'motion_scene', 'audio_timeline', 'subtitle_timeline', 'audition_round', 'audio_asset') for k, i in changes):
            write_views(store.project_directory(pid))
    except Exception:
        # The project transaction already committed. A stale derived index/view
        # must never make callers roll back/delete newly committed source media.
        logging.getLogger(__name__).exception('Project index/view refresh failed; project database remains authoritative')


class ProjectConnection(sqlite3.Connection):
    store: Store
    project_id: str

    def __exit__(self, typ: Any, value: Any, traceback: Any) -> Literal[False]:
        changes = self.execute('SELECT DISTINCT kind,id FROM changed_records').fetchall() if typ is None else []
        try:
            super().__exit__(typ, value, traceback)
        finally:
            self.close()
        if typ is None and changes:
            after_commit(self.store, self.project_id, changes)
        return False


def connect_project(store: Store, pid: str) -> sqlite3.Connection:
    directory = store.project_directory(pid)
    connection = sqlite3.connect(f'file:{directory / DATABASE}?mode=rw', uri=True, timeout=30, factory=ProjectConnection)
    connection.store, connection.project_id = store, pid
    connection.executescript('''CREATE TEMP TABLE changed_records(kind TEXT,id TEXT);
CREATE TEMP TRIGGER changed_insert AFTER INSERT ON main.records BEGIN INSERT INTO changed_records VALUES (new.kind,new.id); END;
CREATE TEMP TRIGGER changed_update AFTER UPDATE ON main.records BEGIN INSERT INTO changed_records VALUES (new.kind,new.id); END;
''')
    return connection


def delivery_root(store: Store, job_id: str) -> Path | None:
    if not store.partitioned:
        return None
    owner = store.locate('job', job_id)
    if owner is None:
        return None
    directory = store.project_directory(owner)
    with sqlite3.connect(directory / DATABASE) as db:
        row = db.execute('SELECT target FROM delivery_routes WHERE job_id=?', (job_id,)).fetchone()
    return safe_path(directory, row[0]) if row else None


def classify(rows: list[tuple[str, str, str]], root: Path) -> dict[tuple[str, str], str]:
    values = {(k, i): json.loads(d) for k, i, d in rows}
    projects = {i for k, i in values if k == 'project'}
    owners = {}
    for key, value in values.items():
        kind, identity = key
        pid = identity if kind in ('project', 'pipeline', 'audio_timeline') and identity in projects else value.get('project_id')
        if pid in projects:
            owners[key] = pid
    # Resolve legacy records that predate explicit project ownership.
    for _ in range(4):
        for key, value in values.items():
            if key in owners:
                continue
            kind, identity = key
            related = [('scene', value.get('scene_id')), ('motion_scene', value.get('scene_id')), ('job', value.get('job_id')), ('job', identity), ('artifact', identity)]
            asset = value.get('result', {}).get('asset', {})
            if asset.get('project_id') in projects:
                owners[key] = asset['project_id']
            for ref in related:
                if ref in owners:
                    owners[key] = owners[ref]
                    break
            if kind == 'analysis_request' and key not in owners:
                path = root / 'analysis-sources' / identity / 'request.json'
                try:
                    aid = json.loads(path.read_text()).get('request', {}).get('audio_asset_id')
                    if ('audio_asset', aid) in owners:
                        owners[key] = owners['audio_asset', aid]
                except (OSError, ValueError):
                    pass
    return owners


def copy_verified(source: Path, target: Path) -> tuple[int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    # APFS clone copies are independent, unlike hard links; fall back portably.
    copied = False
    if sys.platform == 'darwin':
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        copied = library.clonefile(os.fsencode(source), os.fsencode(target), 0) == 0
    if not copied:
        shutil.copy2(source, target)
    original, copied_hash = digest_file(source), digest_file(target)
    if original != copied_hash:
        raise ChromaError('migration_file_changed', '文件复制前后校验不一致，未切换存储布局。', path=str(source))
    return target.stat().st_size, original


@contextmanager
def workspace_guard(root: Path, exclusive: bool = False) -> Iterator[None]:
    # All transport operations take a shared lease, including nonserialized
    # cloud calls. Migration cannot cut over while a request is publishing files.
    with (root / 'storage.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def migrate(store: Store) -> dict[str, Any]:
    with workspace_guard(store.root, exclusive=True):
        return _migrate(store)


def _migrate(store: Store) -> dict[str, Any]:
    """Stage verified copies, then atomically switch the catalog; never rewrite frozen JSON."""
    if store.partitioned:
        return {'layout': LAYOUT, 'already_migrated': True, 'projects': len(project_dirs(store))}
    backup = store.root / '迁移备份' / ('旧布局-' + uuid4().hex[:12])
    backup.mkdir(parents=True)
    with sqlite3.connect(store.path) as db, sqlite3.connect(backup / 'state.sqlite3') as saved:
        db.backup(saved)
        rows = db.execute('SELECT kind,id,data FROM records').fetchall()
        requests = db.execute('SELECT * FROM requests').fetchall()
        notifications = db.execute('SELECT * FROM job_notifications').fetchall()
    values = {(k, i): json.loads(d) for k, i, d in rows}
    if any(v.get('status') in ('queued', 'running', 'pending') for v in values.values()):
        raise ChromaError('migration_busy', '仍有任务或生成请求进行中，请结束后迁移。')
    os.chmod(backup / 'state.sqlite3', 0o600)
    owners = classify(rows, store.root)
    projects = {i: v for (k, i), v in values.items() if k == 'project'}
    stages: dict[str, Path] = {}
    final: dict[str, Path] = {}
    copied_sources: dict[Path, str] = {}
    reports = []
    activated = False
    try:
        for pid, project in projects.items():
            directory = store.root / ('已归档' if project['archived'] else '项目') / label(project['name'], pid)
            if directory.exists():
                raise ChromaError('migration_collision', '目标项目目录已存在，未覆盖。', path=str(directory))
            stage = backup / ('staging-' + pid)
            stage.mkdir()
            stages[pid], final[pid] = stage, directory
            own = [(k, i, d) for k, i, d in rows if owners.get((k, i)) == pid]
            own_jobs = {i for k, i, d in own if k == 'job'}
            routes: dict[str, str] = {}
            for k, i, raw in own:
                d = json.loads(raw)
                if d.get('relative_path') and d.get('storage', 'workspace') == 'workspace':
                    logical, target = route_target(d['relative_path'], d.get('name') if k == 'audio_asset' or d.get('kind') == 'audio-source' else None)
                    routes[logical] = target
                if k == 'job':
                    routes.update([route_target('jobs/' + i)])
                if k == 'subtitle_revision':
                    routes.update([route_target('subtitles/' + pid)])
                if k in ('music_request', 'speech_request', 'analysis_request'):
                    routes.update([route_target(k.removesuffix('_request') + '-sources/' + i)])
            if (store.root / 'audio-sources' / pid).exists():
                routes.update([route_target('audio-sources/' + pid)])
            files: list[dict[str, Any]] = []
            for logical, target in routes.items():
                source = safe_path(store.root, logical)
                if not source.exists():
                    files.append({'logical_path': logical, 'status': 'missing'})
                    continue
                members = sorted(source.rglob('*')) if source.is_dir() else [source]
                for member in members:
                    if member.is_symlink():
                        raise ChromaError('migration_symlink', '项目文件中存在链接，需先明确其归属。', path=str(member))
                    if not member.is_file():
                        continue
                    relative = str(member.relative_to(source)) if source.is_dir() else ''
                    destination = safe_path(stage, str(Path(target) / relative))
                    size, sha = copy_verified(member, destination)
                    copied_sources[member] = sha
                    files.append({'logical_path': str(member.relative_to(store.root)), 'project_path': str(destination.relative_to(stage)), 'bytes': size, 'sha256': sha, 'status': 'verified'})
            # A surviving job log directory is not proof that every registered
            # output still exists. Record missing artifact members individually.
            reported = {f['logical_path'] for f in files}
            verified_hashes = {f['logical_path']: f['sha256'] for f in files if f['status'] == 'verified'}
            for kind, identity, raw in own:
                value = json.loads(raw)
                if kind == 'audio_asset' and value.get('relative_path') in verified_hashes and verified_hashes[value['relative_path']] != value['sha256']:
                    raise ChromaError('audio_asset_changed', '音频内容与已登记校验值不一致，未激活迁移。', asset_id=identity)
            for _kind, _identity, raw in own:
                value = json.loads(raw)
                relative = value.get('relative_path')
                if relative and value.get('storage', 'workspace') == 'workspace' and relative not in reported and not safe_path(store.root, relative).exists():
                    files.append({'logical_path': relative, 'status': 'missing'})
                    reported.add(relative)
            with sqlite3.connect(stage / DATABASE) as db:
                db.executescript(SCHEMA)
                db.executemany('INSERT INTO records VALUES (?,?,?)', own)
                db.executemany('INSERT INTO file_routes VALUES (?,?)', list(routes.items()))
                db.executemany('INSERT INTO requests VALUES (?,?,?)', [r for r in requests if r[2] in own_jobs])
                db.executemany('INSERT INTO job_notifications VALUES (?,?)', [r for r in notifications if r[0] in own_jobs])
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ChromaError('migration_database', '项目数据库完整性检查失败。')
                if set(db.execute('SELECT kind,id,data FROM records')) != set(own):
                    raise ChromaError('migration_database', '项目数据复制不一致。')
            os.chmod(stage / DATABASE, 0o600)
            atomic_text(stage / '.chromascript' / '迁移清单.json', encode({'project_id': pid, 'files': files, 'records': len(own)}))
            write_views(stage)
            reports.append({'project_id': pid, 'name': project['name'], 'directory': str(directory), 'records': len(own), 'verified_files': sum(f['status'] == 'verified' for f in files), 'missing_paths': [f['logical_path'] for f in files if f['status'] == 'missing']})
        # Keep old records/files live throughout preparation. Concurrent changes
        # cancel activation instead of publishing a stale project snapshot.
        with sqlite3.connect(store.path) as db:
            db.executescript(CATALOG)
            db.execute('BEGIN IMMEDIATE')
            if set(db.execute('SELECT kind,id,data FROM records')) != set(rows) or set(db.execute('SELECT * FROM requests')) != set(requests):
                raise ChromaError('migration_conflict', '迁移期间数据发生变化，请在服务空闲时重试。')
            for pid, stage in stages.items():
                final[pid].parent.mkdir(parents=True, exist_ok=True)
                stage.rename(final[pid])
                db.execute('INSERT INTO project_catalog VALUES (?,?)', (pid, str(final[pid].relative_to(store.root))))
            db.executemany('INSERT INTO record_index VALUES (?,?,?)', [(k, i, pid) for (k, i), pid in owners.items()])
            db.executemany('DELETE FROM records WHERE kind=? AND id=?', list(owners))
            db.executemany('DELETE FROM job_notifications WHERE job_id=?', [(i,) for k, i in owners if k == 'job'])
            db.execute("INSERT INTO workspace_meta VALUES ('layout',?)", (LAYOUT,))
        activated = True
    finally:
        if not activated:
            for pid, directory in final.items():
                if directory.exists() and (directory / DATABASE).is_file() and not stages[pid].exists():
                    directory.rename(stages[pid])
    # Originals go into the rollback bundle only after successful activation.
    # Files shared with global/unassigned records remain in their original location.
    retained_text = '\n'.join(raw for k, i, raw in rows if (k, i) not in owners)
    cleanup_errors = []
    for source, sha in copied_sources.items():
        relative = str(source.relative_to(store.root))
        if relative in retained_text or str(source) in retained_text:
            continue
        try:
            if digest_file(source) != sha:
                cleanup_errors.append(relative)
                continue
            backup_target = backup / 'legacy-files' / relative
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(backup_target)
        except OSError:
            cleanup_errors.append(relative)
    report = {'layout': LAYOUT, 'backup_directory': str(backup), 'projects': reports, 'retained_global_kinds': sorted({k for k, i, raw in rows if (k, i) not in owners}), 'cleanup_preserved': cleanup_errors}
    atomic_text(backup / '迁移报告.json', encode(report))
    atomic_text(store.root / '项目目录说明.md', '# ChromaScript 本地项目\n\n打开“项目”或“已归档”，按项目名称查找文件夹。每个项目中的 project.sqlite3 保存其完整项目记录；根目录 state.sqlite3 仅保留项目位置索引、全局设置、去重索引及无法确定归属的旧记录。\n\n迁移备份保存旧数据库和原始文件，用于回滚；不要在运行服务时手动替换数据库。未归属的旧目录保留，未将它们猜测分配给某个项目。\n')
    return report


class ProjectStorageOperations:
    store: Store

    @operation('Inspect project folder/database locations, portable file mapping, missing files and global management layout. Read-only.')
    def get_project_storage(self, project_id: str | None = None) -> dict[str, Any]:
        store = self.store
        result: dict[str, Any] = {'layout': LAYOUT if store.partitioned else 'legacy', 'workspace_database': str(store.path)}
        if store.partitioned:
            dirs = project_dirs(store)
            result['projects'] = [{'project_id': pid, 'directory': str(d), 'database': str(d / DATABASE)} for pid, d in dirs.items() if project_id is None or pid == project_id]
            if project_id:
                directory = store.project_directory(project_id)
                with sqlite3.connect(directory / DATABASE) as db:
                    result['routes'] = [{'logical_path': logical, 'project_path': target, 'exists': safe_path(directory, target).exists()} for logical, target in db.execute('SELECT logical,target FROM file_routes')]
        return result

    @operation('Migrate an idle legacy workspace into independent per-project SQLite folders, with verified file copies and a rollback backup. No cloud calls or rendering. Refuses active jobs/generation and changed data.', serialized=False)
    def migrate_workspace_storage(self) -> dict[str, Any]:
        return migrate(self.store)

    @operation('Refresh readable source, timeline and audition exports from the project database. Reports manually edited file conflicts without overwriting them.')
    def sync_project_folder(self, project_id: str) -> dict[str, Any]:
        directory = self.store.project_directory(project_id)
        return {'directory': str(directory), **write_views(directory)}

    @operation('Open the named project folder in the native file manager. Does not edit data.')
    def open_project_folder(self, project_id: str) -> dict[str, Any]:
        from .desktop import open_directory
        directory = self.store.project_directory(project_id)
        open_directory(str(directory))
        return {'directory': str(directory)}

    @operation('Create an independent consistent project backup folder, including project SQLite and mapped local files. External deliveries remain explicit references. Rejects active jobs or changed data; never overwrites destinations.')
    def export_project_folder(self, project_id: str, destination: str) -> dict[str, Any]:
        store = self.store
        directory = store.project_directory(project_id)
        from .exports import export_directory
        target = Path(destination).expanduser()
        if not target.is_absolute() or target.exists() or target.is_symlink() or target.resolve().is_relative_to(directory):
            raise ChromaError('project_export', '请输入不存在的绝对备份目录，且不能位于原项目内。')
        target = export_directory(str(target))
        records = store.project_records(project_id)
        if any(json.loads(raw).get('status') in ('queued', 'running', 'pending') for k, i, raw in records):
            raise ChromaError('project_busy', '项目仍有任务进行中，请稍后备份。')
        stage = target.with_name('.' + target.name + '-' + uuid4().hex)
        stage.mkdir(parents=True)
        try:
            with sqlite3.connect(directory / DATABASE) as source, sqlite3.connect(stage / DATABASE) as dest:
                source.backup(dest)
            for path in directory.rglob('*'):
                if path.is_symlink():
                    raise ChromaError('project_export', '项目中存在符号链接，请先确认其备份范围。')
                if path.is_file() and path.name not in (DATABASE, DATABASE + '-wal', DATABASE + '-shm', DATABASE + '-journal'):
                    copy_verified(path, stage / path.relative_to(directory))
            if set(store.project_records(project_id)) != set(records):
                raise ChromaError('project_changed', '备份过程中项目发生变化，请重试。')
            # Reverify source checksums after the consistent database copy.
            missing = []
            with sqlite3.connect(stage / DATABASE) as db:
                for logical, relative in db.execute('SELECT logical,target FROM file_routes'):
                    if not safe_path(stage, relative).exists():
                        missing.append(logical)
                routes = dict(db.execute('SELECT logical,target FROM file_routes'))
                for _raw_kind, _raw_identity, raw_record in records:
                    value = json.loads(raw_record)
                    logical = value.get('relative_path')
                    if not logical or value.get('storage', 'workspace') != 'workspace':
                        continue
                    prefix = next((p for p in sorted(routes, key=len, reverse=True) if logical == p or logical.startswith(p + '/')), None)
                    if prefix is None or not safe_path(stage, routes[prefix] + logical[len(prefix):]).exists():
                        missing.append(logical)
                missing = sorted(set(missing))
                for (raw,) in db.execute("SELECT data FROM records WHERE kind='audio_asset'"):
                    asset = json.loads(raw)
                    logical = asset['relative_path']
                    relative = routes.get(logical)
                    if relative and safe_path(stage, relative).is_file() and digest_file(safe_path(stage, relative)) != asset['sha256']:
                        raise ChromaError('audio_asset_changed', '备份音频与项目记录校验不一致。')
            os.chmod(stage / DATABASE, 0o600)
            atomic_text(stage / '.chromascript' / '备份说明.json', encode({'missing_paths': missing, 'external_deliveries_included': False}))
            stage.rename(target)
            return {'directory': str(target), 'missing_paths': missing, 'external_deliveries_included': False}
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise


if __name__ == '__main__':
    from .store import Store
    print(encode(migrate(Store(Path(sys.argv[1])))))

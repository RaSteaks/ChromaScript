"""Real migration, per-project writes and standalone folder recovery without rendering."""
import json
import shutil
import sqlite3

import pytest

from chromascript.audio import AudioClip, AudioSynthesis, AudioTimeline
from chromascript.errors import ChromaError
from chromascript.images import file_hash
from chromascript.project_storage import migrate
from chromascript.service import Service
from chromascript.store import Store


def fixture(root):
    service = Service(root)
    pid = service.create_project('相机：配乐 / 项目', 'Finder project')['project']['id']
    scene = service.create_scene(pid)
    motion = service.create_motion_scene(pid, engine='remotion-sdr-v1')
    asset = service.create_audio(pid, '音乐素材', AudioSynthesis(duration_seconds=1))['asset']
    track = service.set_project_audio(pid, '0', AudioTimeline(clips=[AudioClip(asset_id=asset['id'])]))
    other = service.create_project('Another')['project']['id']
    return service, pid, scene, motion, asset, track, other


def test_migrate_records_media_and_future_writes(tmp_path):
    service, pid, scene, motion, asset, track, other = fixture(tmp_path / 'workspace')
    with service.store.connect() as db:
        before = db.execute('SELECT kind,id,data FROM records').fetchall()
    report = migrate(service.store)
    assert len(report['projects']) == 2
    folder = service.store.project_directory(pid)
    assert folder.name.startswith('相机 配乐   项目') or pid[:8] in folder.name
    assert (folder / 'project.sqlite3').is_file()
    assert len(list((folder / '作品').glob('*/source.*'))) == 2
    assert service.get_scene(scene['record']['id'])['scene'] == scene['scene']
    assert service.get_motion_scene(motion['record']['id'])['scene'] == motion['scene']
    assert service.get_project_audio(pid)['revision_id'] == track['revision_id']
    assert file_hash(service.store.file(asset['relative_path'])) == asset['sha256']
    assert service.store.file(asset['relative_path']).is_relative_to(folder)
    with service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='project'").fetchone()[0] == 0
    all_rows = []
    for owner in service.store.database_owners():
        with service.store.connect(owner) as db:
            all_rows.extend(db.execute('SELECT kind,id,data FROM records').fetchall())
    assert set(all_rows) == set(before)
    new = service.create_audio(pid, '新增素材', AudioSynthesis(duration_seconds=0.5))['asset']
    assert service.store.file(new['relative_path']).is_relative_to(folder)
    with sqlite3.connect(folder / 'project.sqlite3') as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='audio_asset'").fetchone()[0] == 2
    new_project = service.create_project('New after migration')['project']['id']
    assert service.store.project_directory(new_project).is_dir()
    service.create_scene(new_project)
    assert service.get_project_audio(other)['assets'] == []
    assert migrate(service.store)['already_migrated']


def test_folder_move_offline_and_portable_restore(tmp_path):
    service, pid, scene, motion, asset, track, other = fixture(tmp_path / 'workspace')
    migrate(service.store)
    original = service.store.project_directory(pid)
    renamed = original.with_name('手动重命名项目')
    original.rename(renamed)
    assert service.store.project_directory(pid) == renamed
    assert file_hash(service.store.file(asset['relative_path'])) == asset['sha256']
    result = service.export_project_folder(pid, str(tmp_path / 'portable'))
    assert result['missing_paths'] == []
    with pytest.raises(ChromaError):
        service.export_project_folder(pid, str(tmp_path / 'portable'))
    new_root = tmp_path / 'new-workspace'
    new_store = Store(new_root)
    migrate(new_store)
    (new_root / '项目').mkdir(exist_ok=True)
    shutil.copytree(tmp_path / 'portable', new_root / '项目' / '恢复的项目')
    restored = Service(new_root)
    assert restored.get_scene(scene['record']['id'])['scene'] == scene['scene']
    assert file_hash(restored.store.file(asset['relative_path'])) == asset['sha256']
    renamed.rename(tmp_path / 'removed')
    with pytest.raises(ChromaError, match='不存在|缺失'):
        service.get_project(pid)
    assert not renamed.exists()


def test_projection_conflicts_and_missing_files(tmp_path):
    service, pid, scene, _, asset, _, _ = fixture(tmp_path / 'workspace')
    service.store.file(asset['relative_path']).unlink()
    report = migrate(service.store)
    assert next(p for p in report['projects'] if p['project_id'] == pid)['missing_paths']
    folder = service.store.project_directory(pid)
    source = next((folder / '作品').glob('*/source.wgsl'))
    source.write_text('// manual edit')
    spec = service.scene(scene['record']['id'])[1].model_copy(update={'code': scene['scene']['code'] + '\n// server update'})
    service.update_scene(scene['record']['id'], spec, scene['revision_id'])
    result = service.sync_project_folder(pid)
    assert result['conflicts'] and source.read_text() == '// manual edit'
    assert json.loads((folder / 'project.json').read_text())['project_id'] == pid


def test_migration_refuses_active_and_rolls_back_before_activation(tmp_path, monkeypatch):
    service, pid, _, _, _, _, _ = fixture(tmp_path / 'workspace')
    service.store.put('job', 'busy', {'id': 'busy', 'project_id': pid, 'status': 'running'})
    with pytest.raises(ChromaError, match='进行中'):
        migrate(service.store)
    service.store.update('job', 'busy', lambda j: {**j, 'status': 'completed'})
    with service.store.connect() as db:
        before = db.execute('SELECT * FROM records').fetchall()
    def fail(*args):
        raise OSError('injected copy failure')
    monkeypatch.setattr('chromascript.project_storage.copy_verified', fail)
    with pytest.raises(OSError):
        migrate(service.store)
    assert not service.store.partitioned
    with service.store.connect() as db:
        assert set(db.execute('SELECT * FROM records')) == set(before)


def test_partitioned_auditions_clone_and_job_recovery(tmp_path):
    from chromascript.operations import execute
    service, pid, _, _, asset, _, other = fixture(tmp_path / 'workspace')
    migrate(service.store)
    round = execute(service, 'create_audition', {'project_id': pid, 'title': '测试方向', 'duration_seconds': 0.5, 'candidates': [{'asset_id': asset['id']}], 'idempotency_key': 'round'})
    assert service.get_audition(pid, round['id'])['id'] == round['id']
    playback = service.store.get('artifact', round['candidates'][0]['playback_artifact_id'])
    assert service.store.file(playback['relative_path']).is_relative_to(service.store.project_directory(pid))
    clone = service.clone_project(pid, '复制项目')['project']['id']
    assert service.store.project_directory(clone).is_dir()
    copied = service.get_project_audio(clone)['assets'][0]
    assert service.store.file(copied['relative_path']).is_relative_to(service.store.project_directory(clone))
    observed = []
    service.store.on_job_change = lambda j: observed.append(j['id'])
    job = {'id': 'local-job', 'project_id': pid, 'status': 'completed', 'created_at': '2026-01-01'}
    service.store.create_job('key', 'hash', job, {'settings': {}, 'project_id': pid})
    assert service.store.get('snapshot', 'local-job')['project_id'] == pid
    assert observed == ['local-job']
    assert service.store.create_job('key', 'hash', {'id': 'another-job', 'project_id': other}, {})['id'] == 'local-job'
    with pytest.raises(ChromaError, match='different'):
        service.store.create_job('key', 'different', {'id': 'other', 'project_id': other}, {})
    assert service.store.file('jobs/local-job/worker.log').is_relative_to(service.store.project_directory(pid))
    service.store.update('job', 'local-job', lambda j: {**j, 'status': 'failed'})
    assert observed == ['local-job', 'local-job']
    service.archive_project(pid)
    assert service.store.project_directory(pid).parent.name == '已归档'
    service.archive_project(pid, False)
    service.update_project(pid, name='改名项目')
    assert service.store.project_directory(pid).name.startswith('改名项目__')
    assert service.get_delivery_export_options(project_id=pid)['directory'].endswith('/交付')


def test_internal_delivery_relocation_and_index_rebuild(tmp_path):
    from chromascript.artifacts import artifact_path
    service, pid, _, _, asset, _, _ = fixture(tmp_path / 'workspace')
    migrate(service.store)
    folder = service.store.project_directory(pid)
    output = folder / '交付' / 'rendered'
    output.mkdir()
    (output / 'video.mov').write_bytes(b'verified fixture')
    service.store.put('job', 'finished', {'id': 'finished', 'project_id': pid, 'status': 'completed', 'export_completed': True, 'export_path': str(output)})
    artifact = {'id': 'video', 'job_id': 'finished', 'project_id': pid, 'storage': 'delivery', 'relative_path': 'video.mov'}
    service.store.put('artifact', 'video', artifact)
    service.archive_project(pid)
    assert artifact_path(service.store, artifact).read_bytes() == b'verified fixture'
    backup = tmp_path / 'backup'
    service.export_project_folder(pid, str(backup))
    fresh = tmp_path / 'restored-workspace'
    (fresh / '项目').mkdir(parents=True)
    shutil.copytree(backup, fresh / '项目' / 'restored')
    restored = Service(fresh)
    assert restored.store.partitioned
    assert artifact_path(restored.store, restored.store.get('artifact', 'video')).read_bytes() == b'verified fixture'
    assert file_hash(restored.store.file(asset['relative_path'])) == asset['sha256']


def test_failed_folder_rename_preserves_metadata(tmp_path):
    from chromascript.project_storage import label
    service, pid, _, _, _, _, _ = fixture(tmp_path / 'workspace')
    migrate(service.store)
    before = service.store.get('project', pid)
    original = service.store.project_directory(pid)
    collision = original.parent / label('collision', pid)
    collision.mkdir()
    with pytest.raises(ChromaError, match='已存在'):
        service.update_project(pid, name='collision')
    assert service.store.get('project', pid) == before
    assert service.store.project_directory(pid) == original
    with sqlite3.connect(original / 'project.sqlite3') as db:
        db.execute("CREATE TRIGGER reject_rename BEFORE UPDATE ON records WHEN NEW.kind='project' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.update_project(pid, name='must rollback')
    assert service.store.get('project', pid) == before
    assert service.store.project_directory(pid) == original


def test_sampling_ownership_retry_busy_and_portable_restore(tmp_path, monkeypatch):
    from chromascript.motion import MotionOutput, MotionSpec
    from chromascript.motion_sampling import close_samples

    service = Service(tmp_path / 'workspace')
    pid = service.create_project('Sampling')['project']['id']
    other = service.create_project('Other')['project']['id']
    spec = MotionSpec.model_validate(service.create_motion_scene(pid)['scene'])
    output = MotionOutput(master_bit_depth=16)
    destination = str(tmp_path / 'delivery')
    monkeypatch.setattr('chromascript.motion_sampling.content_identity', lambda *_: 'sampling-test')

    def blocked(store, identity, task):
        # Exercise real task registration/leases without starting a renderer.
        task.cancelled.wait(10)
        store.update('motion_estimate', identity, lambda r: {**r, 'status': 'cancelled'})

    monkeypatch.setattr('chromascript.motion_sampling._run_sample', blocked)
    try:
        legacy = service.start_motion_render_estimate(pid, spec, output, destination, 'legacy')['estimate']['id']
        close_samples(service.store)
        migrate(service.store)
        assert service.start_motion_render_estimate(pid, spec, output, destination, 'legacy')['estimate']['id'] == legacy
        second_service = Service(service.store.root)
        current = service.start_motion_render_estimate(pid, spec, output, destination, 'new')['estimate']['id']
        assert second_service.start_motion_render_estimate(pid, spec, output, destination, 'new')['estimate']['id'] == current
        with pytest.raises(ChromaError) as conflict:
            second_service.start_motion_render_estimate(other, spec, output, destination, 'new')
        assert conflict.value.code == 'idempotency_conflict'
        with pytest.raises(ChromaError) as busy:
            second_service.start_motion_render_estimate(other, spec, output, destination, 'another')
        assert busy.value.code == 'estimate_busy'
        with pytest.raises(ChromaError) as moving:
            service.update_project(pid, name='Cannot move active sample')
        assert moving.value.code == 'project_busy'
        with pytest.raises(ChromaError) as exporting:
            service.export_project_folder(pid, str(tmp_path / 'busy-backup'))
        assert exporting.value.code == 'project_busy'
    finally:
        close_samples(service.store)
    with service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='motion_estimate'").fetchone()[0] == 0
    restored_root = tmp_path / 'restored'
    backup = restored_root / '项目' / 'Sampling'
    service.export_project_folder(pid, str(backup))
    restored = Service(restored_root)
    assert restored.store.get('motion_estimate', current)['status'] == 'cancelled'
    assert restored.start_motion_render_estimate(pid, spec, output, destination, 'new')['estimate']['id'] == current

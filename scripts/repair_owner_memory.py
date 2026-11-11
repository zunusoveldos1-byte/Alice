"""Explicit owner-request repair; back up SQLite before schema/data changes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alice_voice.config import Settings
from alice_voice.memory import FactMemory

parser = argparse.ArgumentParser()
parser.add_argument('--owner-name', required=True)
args = parser.parse_args()
path = Path(Settings.load('settings.json').data_directory) / 'memory.sqlite3'
backup = path.with_name('memory-backup-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.sqlite3')
with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
    source.backup(target)
source.close(); target.close()
memory = FactMemory(path)
rows = memory.records(include_inactive=True)  # Legacy migration preserves unknown records for review.
owner = memory.owner_record()
memory._write('profile', 'owner_name', args.owner_name, source='user_requirement',
              expected_revision=owner['revision'] if owner else 0,
              reason='explicit_owner_repair_2026_10_23')
quarantined = [r for r in rows if r['source'] == 'legacy_unknown' and r['status'] == 'quarantined']
with memory._connect() as db:
    db.execute('BEGIN IMMEDIATE')
    for row in quarantined:
        if row['field'].startswith('legacy_'):
            db.execute('DELETE FROM facts WHERE id=?', (int(row['field'][7:]),))
    # Derived archives with unsupported owner claims remain reviewable, never active evidence.
    db.execute('''UPDATE memory_records SET status=?,revision=revision+1,updated_at=?
                  WHERE category=? AND status=?''', ('unverified', memory._now(), 'conversation_summary', 'derived'))
memory._invalidate()
result = {'backup': str(backup.resolve()), 'owner_name': memory.owner_name(),
          'quarantined_record_ids': [r['id'] for r in quarantined],
          'preserved_other_records': len(rows) - len(quarantined),
          'active_records': memory.records(), 'audit_entries': len(memory.audit())}
Path('docs/owner-memory-repair-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=True))

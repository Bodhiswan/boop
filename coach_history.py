"""Local Coach transcript and opt-in local morning brief. No credentials are stored.

NOOP's CoachMessageStore and CoachBriefScheduler inspire the bounded transcript
and one-success-per-day schedule. Windows uses an app timer; BOOP must be running.
The scheduled provider is always local. External keys remain request-only.
"""
from contextlib import closing
from datetime import datetime
import json
import time
import uuid

DEFAULT_CONFIG = dict(master_enabled=True, enabled=False, time_minutes=420)
BRIEF_QUESTION = "Give me a short morning brief from the recorded metrics: recovery, sleep and activity. Explain missing data and avoid inventing observations."


class CoachHistory:
    def __init__(self, features):
        self.features = features
        with closing(features.store.connect()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS boop_coach_messages(id TEXT PRIMARY KEY,role TEXT NOT NULL,text TEXT NOT NULL,provider TEXT NOT NULL,created_ms INTEGER NOT NULL,deleted_ms INTEGER)")
            if 'context_eligible' not in {r[1] for r in conn.execute('PRAGMA table_info(boop_coach_messages)')}:
                conn.execute('ALTER TABLE boop_coach_messages ADD COLUMN context_eligible INTEGER NOT NULL DEFAULT 1')
            conn.execute("CREATE TABLE IF NOT EXISTS boop_control_settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)")

    def config(self, body=None):
        with closing(self.features.store.connect()) as conn, conn:
            row = conn.execute("SELECT value_json FROM boop_control_settings WHERE key='coach_brief'").fetchone()
            current = {**DEFAULT_CONFIG, **(json.loads(row[0]) if row else {})}
            if body is not None:
                if not isinstance(body,dict) or set(body)-set(DEFAULT_CONFIG):
                    raise ValueError("Unknown Coach schedule setting")
                config = {**current, **body}
                for key in ('master_enabled','enabled'):
                    if type(config[key]) is not bool:
                        raise ValueError("Coach switches must be true or false")
                if type(config['time_minutes']) is not int or not 0<=config['time_minutes']<1440:
                    raise ValueError("Coach brief time must be between 00:00 and 23:59")
                conn.execute("INSERT OR REPLACE INTO boop_control_settings VALUES('coach_brief',?)",(json.dumps(config),))
                current = config
        return {**current, 'provider':'offline', 'running_required':True,
                'note':'Scheduled inference stays on this laptop; no external provider key is stored.'}

    def history(self, limit=100):
        with closing(self.features.store.connect()) as conn:
            rows = conn.execute("SELECT id,role,text,provider,created_ms FROM boop_coach_messages WHERE deleted_ms IS NULL ORDER BY created_ms DESC,rowid DESC LIMIT ?",(max(1,min(100,int(limit))),)).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM boop_coach_messages WHERE deleted_ms IS NULL").fetchone()[0]
            undo = conn.execute("SELECT COUNT(*) FROM boop_coach_messages WHERE deleted_ms IS NOT NULL").fetchone()[0]
        return dict(messages=list(reversed([dict(r) for r in rows])), count=count, undo_available=bool(undo), max_messages=100)

    def context(self,now,limit=8):
        """Source's strictly-forward local-day rule, without changing the transcript.

        The successful-append marker uses insertion order, so a clock moving
        backwards does not drop newly completed pairs. Failed requests never
        advance this marker or erase earlier readable messages.
        """
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Coach context requires an aware datetime')
        count=max(2,min(100,int(limit)));count-=count%2
        with closing(self.features.store.connect()) as conn:
            saved=conn.execute("SELECT value_json FROM boop_control_settings WHERE key='coach_context'").fetchone()
            marker=json.loads(saved[0]) if saved else None
            latest=conn.execute('SELECT created_ms FROM boop_coach_messages WHERE deleted_ms IS NULL AND context_eligible=1 ORDER BY rowid DESC LIMIT 1').fetchone()
            last_day=marker['day'] if marker else datetime.fromtimestamp(latest[0]/1000,now.tzinfo).date().isoformat() if latest else None
            if last_day and now.date().isoformat()>last_day:return []
            rows=conn.execute('SELECT role,text,provider,created_ms FROM boop_coach_messages WHERE deleted_ms IS NULL AND context_eligible=1 AND rowid>=? ORDER BY rowid DESC LIMIT ?',(marker['start_rowid'] if marker else 0,count)).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def append(self, question, result, day=None):
        now = int(time.time()*1000)
        with closing(self.features.store.connect()) as conn, conn:
            from analytics import AnalyticsService
            _,tz=AnalyticsService._timezone(self.features.settings())
            today=datetime.fromtimestamp(now/1000,tz).date().isoformat()
            saved=conn.execute("SELECT value_json FROM boop_control_settings WHERE key='coach_context'").fetchone()
            marker=json.loads(saved[0]) if saved else None
            latest=conn.execute('SELECT created_ms FROM boop_coach_messages WHERE context_eligible=1 ORDER BY rowid DESC LIMIT 1').fetchone()
            if marker is None:
                marker=dict(day=datetime.fromtimestamp(latest[0]/1000,tz).date().isoformat() if latest else today,start_rowid=0)
            if today>marker['day']:
                marker=dict(day=today,start_rowid=conn.execute('SELECT COALESCE(MAX(rowid),0)+1 FROM boop_coach_messages').fetchone()[0])
            conn.executemany("INSERT INTO boop_coach_messages(id,role,text,provider,created_ms,deleted_ms,context_eligible) VALUES(?,?,?,?,?,NULL,1)",[(uuid.uuid4().hex,role,text,result['provider'],now) for role,text in [('user',question),('assistant',result['answer'])]])
            conn.execute("INSERT OR REPLACE INTO boop_control_settings VALUES('coach_context',?)",(json.dumps(marker),))
            # Match the source's bounded text transcript; diagnostic/raw biometric streams are absent.
            conn.execute("DELETE FROM boop_coach_messages WHERE id NOT IN (SELECT id FROM boop_coach_messages ORDER BY rowid DESC LIMIT 100)")
            if day:
                config = self.config()
                config = {k:v for k,v in config.items() if k in DEFAULT_CONFIG or k in ('last_day','last_text','last_created_ms')}
                config.update(last_day=day,last_text=result['answer'],last_created_ms=now)
                conn.execute("INSERT OR REPLACE INTO boop_control_settings VALUES('coach_brief',?)",(json.dumps(config),))
        return {**result,'saved':True,'history':self.history()}

    def clear(self, undo=False):
        with closing(self.features.store.connect()) as conn, conn:
            if undo:
                conn.execute("UPDATE boop_coach_messages SET deleted_ms=NULL WHERE deleted_ms IS NOT NULL")
            else:
                conn.execute("UPDATE boop_coach_messages SET deleted_ms=? WHERE deleted_ms IS NULL",(int(time.time()*1000),))
        return self.history()

    def due(self, now):
        config = self.config()
        return config['master_enabled'] and config['enabled'] and now.hour*60+now.minute>=config['time_minutes'] and config.get('last_day')!=now.date().isoformat()

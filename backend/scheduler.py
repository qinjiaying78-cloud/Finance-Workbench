"""Local daily sync scheduler with durable daily request keys (Asia/Shanghai)."""
import asyncio,os
from datetime import datetime,timezone,timedelta
from .database import get_connection
from .routers.collaboration import queue_sync
from .sync_service import run_sync

def due_runs():
    now=datetime.now(timezone(timedelta(hours=8)))
    if (now.hour,now.minute)<(2,17): return []
    c=get_connection();jobs=[]
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            for config in c.execute('SELECT * FROM fw_sync_connections WHERE enabled=1 AND scheduled=1').fetchall():
                key='nightly:'+now.date().isoformat()+':'+config['id']
                run,new=queue_sync(c,config['account_set_id'],config['id'],key,'service:cron','schedule')
                if new: jobs.append(run['id'])
    finally:c.close()
    return jobs
async def loop():
    if os.getenv('FW_SCHEDULER_ENABLED','true').lower()!='true': return
    while True:
        try:
            for job in await asyncio.to_thread(due_runs): await asyncio.to_thread(run_sync,job)
        except asyncio.CancelledError: raise
        except Exception:
            # Durable runs show their failure; a malformed config must not stop future ticks.
            import logging
            logging.getLogger(__name__).exception('Daily sync scheduling failed')
        await asyncio.sleep(30)

import logging,time,json,os
from pathlib import Path
from sqlalchemy import select
from .database import Session
from .models import SchoolAnalysis,SchoolGrouping,Transect,Echo
from .school_service import queue
from .algorithm import analyze,group_echoes
from .common import dump
logging.basicConfig(level=logging.INFO)
ARTIFACTS=Path(os.getenv('ARTIFACTS_DIR','/artifacts'))
def _write(name,value):
    ARTIFACTS.mkdir(parents=True,exist_ok=True);(ARTIFACTS/name).write_text(json.dumps(value,ensure_ascii=False))
def run_analysis(db,row):
    t=db.get(Transect,row.transect_id)
    if row.data_version!=t.data_version:raise ValueError('回波已更新，请重新提交最新版本')
    points=[dump(p) for p in db.scalars(select(Echo).where(Echo.transect_id==t.id).order_by(Echo.id))]
    row.assignments,row.metrics=analyze(points,row.eps,row.min_samples);row.status='done'
    _write(f'school-{row.id}.json',row.metrics)
def run_grouping(db,row):
    t=db.get(Transect,row.transect_id)
    if row.data_version!=t.data_version:raise ValueError('回波已更新，本次分群基于旧回波版本')
    points=[dump(p) for p in db.scalars(select(Echo).where(Echo.transect_id==t.id).order_by(Echo.id))]
    result=group_echoes(points,row.annotations,row.eps,row.min_samples)
    row.assignments,row.groups,row.metrics=result['assignments'],result['groups'],result['metrics'];row.status='done'
    _write(f'grouping-{row.id}.json',{'metrics':row.metrics,'groups':row.groups})
def handle(db,model,row_id,fn):
    row=db.get(model,row_id)
    if not row or row.status!='queued':return
    row.status='running';db.commit()
    try:fn(db,row)
    except Exception as e:row.status='failed';row.error=str(e)[:200]
    db.commit()
def main():
    while True:
        Path('/tmp/worker-heartbeat').touch()
        try:
            item=queue.blpop(['estuary:groupings','estuary:schools'],timeout=2)
            if not item:continue
            key,raw=item
            with Session() as db:
                if key=='estuary:groupings':handle(db,SchoolGrouping,int(raw),run_grouping)
                else:handle(db,SchoolAnalysis,int(raw),run_analysis)
        except Exception:logging.exception('echo worker');time.sleep(1)
if __name__=='__main__':main()

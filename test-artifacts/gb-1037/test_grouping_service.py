import os,sys,tempfile,json
os.environ['DATABASE_URL']='sqlite+pysqlite:///'+tempfile.mkdtemp()+'/t.db'
sys.path.insert(0,'/workspace/backend')
os.environ.setdefault('ARTIFACTS_DIR',tempfile.mkdtemp()+'/artifacts')
import fakeredis
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from src import database
from src.database import Session
from src.models import *
from src.database import Base,engine
from src.models import User,Cruise,Boat,Transect,Echo,AnnotationState,EchoAnnotation,SchoolGrouping,AnnotationBatch
import src.school_service as school_service
import src.grouping_service as gs
from fastapi import HTTPException
from types import SimpleNamespace

fake=fakeredis.FakeRedis(decode_responses=True)
school_service.queue=fake;gs.queue=fake
Base.metadata.create_all(engine)
user=SimpleNamespace(username='supervisor')

def setup_cruise():
    with Session() as db:
        b=Boat(code='T1',name='潮研一号');db.add(b);db.flush()
        c=Cruise(code='CX',boat_id=b.id,depart=__import__('datetime').datetime.fromisoformat('2026-11-18T14:00:00+08:00'),arrive=__import__('datetime').datetime.fromisoformat('2026-11-18T17:00:00+08:00'),purpose='test',status='sailing')
        db.add(c);db.flush()
        t=Transect(cruise_id=c.id,code='E1',start_lon=120.1,start_lat=30.1,end_lon=120.113,end_lat=30.1,length_m=1250,probe_khz=120,gain_db=36,data_version=1)
        db.add(t);db.flush()
        # 5 echoes: ids 1..5; 1,2 near each other, 3 far, 4,5 mid
        for i,(along,depth,strength) in enumerate([(100,8,-45),(101,8.1,-45),(1000,20,-60),(500,12,-50),(501,12.1,-50)],start=1):
            db.add(Echo(transect_id=t.id,along_m=along,lon=120.1+along/96_500,lat=30.1,depth_m=depth,strength_db=strength))
        db.commit();return c.id,t.id

def expect_http(fn,status):
    try:fn()
    except HTTPException as e:assert e.status_code==status,(e.status_code,e.detail);return e.detail
    raise AssertionError(f'expected {status}')

cid,tid=setup_cruise()
print('--- idempotent annotation retry ---')
key='req-0001'
r1=gs.annotate(Session(),tid,[{'echo_a_id':1,'echo_b_id':2,'relation':'same'}],key,user)
r2=gs.annotate(Session(),tid,[{'echo_a_id':1,'echo_b_id':2,'relation':'same'}],key,user)
assert r1==r2 and r1['annotation_version']==1 and r1['changed'] is True,r1
with Session() as db:
    assert len(db.scalars(select(AnnotationBatch)).all())==1  # 重试命中同一请求键，不重复登记批次
    assert len(db.scalars(select(EchoAnnotation)).all())==1
    assert db.scalar(select(AnnotationState).where(AnnotationState.transect_id==tid)).version==1
print('PASS retry with same key does not duplicate batch, annotation, or version')

print('--- duplicate same batch without key is no-op ---')
r3=gs.annotate(Session(),tid,[{'echo_a_id':2,'echo_b_id':1,'relation':'same'}],None,user)
assert r3['changed'] is False and r3['annotation_version']==1,r3
print('PASS')

print('--- order independence of input ---')
a=gs.annotate(Session(),tid,[{'echo_a_id':2,'echo_b_id':3,'relation':'different'}],None,user)
b=gs.annotate(Session(),tid,[{'echo_a_id':3,'echo_b_id':2,'relation':'different'}],None,user)
assert a['annotation_version']==b['annotation_version']==2 and b['changed'] is False,(a,b)
print('PASS')

print('--- contradiction rejected as whole batch ---')
before=gs.snapshot(Session().query(Transect).first(),tid)[0] if False else None
with Session() as db:
    t=db.get(Transect,tid);_,before_ann=gs.snapshot(db,t)
detail=expect_http(lambda:gs.annotate(Session(),tid,[{'echo_a_id':1,'echo_b_id':3,'relation':'same'},{'echo_a_id':2,'echo_b_id':3,'relation':'different'}],None,user),409)
assert detail['conflicts'] and detail['conflicts'][0]['kind']=='different_within_same_group',detail
with Session() as db:
    t=db.get(Transect,tid);version,after_ann=gs.snapshot(db,t)
    assert after_ann==before_ann and version==2,(version,after_ann)
print('PASS contradiction returns 409 and registers nothing')

print('--- same batch same pair conflicting relations ---')
expect_http(lambda:gs.annotate(Session(),tid,[{'echo_a_id':4,'echo_b_id':5,'relation':'same'},{'echo_a_id':4,'echo_b_id':5,'relation':'different'}],None,user),409)
print('PASS')

print('--- retry key reused with different payload rejected ---')
expect_http(lambda:gs.annotate(Session(),tid,[{'echo_a_id':4,'echo_b_id':5,'relation':'same'}],'req-0001',user),409)
print('PASS')

print('--- enqueue snapshot + idempotent retry ---')
g1=gs.enqueue(Session(),tid,1.2,4,'calc-0001',user)
g2=gs.enqueue(Session(),tid,1.2,4,'calc-0001',user)
assert g1['id']==g2['id'] and g1['annotation_version']==2,g1
with Session() as db:assert len(db.scalars(select(SchoolGrouping)).all())==1
print('PASS enqueue retry returns same grouping, no duplicate row')

print('--- worker computes snapshot ---')
from src.worker import run_grouping
with Session() as db:
    row=db.get(SchoolGrouping,g1['id']);row.status='running';db.commit();run_grouping(db,row);db.commit()
    assert row.status=='done',row.error
    assert row.metrics['groups']>=1 and row.metrics['annotation_count']==2,row.metrics
print('PASS grouping computed',row.metrics['groups'],'groups')

print('--- updating annotations after enqueue makes old result unsignable ---')
gs.annotate(Session(),tid,[{'echo_a_id':4,'echo_b_id':5,'relation':'same'}],None,user)
detail=expect_http(lambda:gs.review(Session(),g1['id'],'底质干扰已标识说明', '可用于调查图层',user),409)
assert '旧版本' in detail,detail
with Session() as db:assert db.get(SchoolGrouping,g1['id']).status=='done'
print('PASS stale result cannot be signed')

print('--- recompute after annotation update then sign ---')
g3=gs.enqueue(Session(),tid,1.2,4,None,user)
with Session() as db:
    row=db.get(SchoolGrouping,g3['id']);row.status='running';db.commit();run_grouping(db,row);db.commit();assert row.status=='done',row.error
    rev=gs.review(db,g3['id'],'底质干扰已标识说明','可用于调查图层',user)
    assert rev['grouping_id']==g3['id']
print('PASS recomputed result signed')

print('--- simulate slow-old-result finishing after newer snapshot cannot sign ---')
# 两个重算先后提交；标注更新在两者之间；老任务晚算完
g_old=gs.enqueue(Session(),tid,1.0,4,None,user)
gs.annotate(Session(),tid,[{'echo_a_id':3,'echo_b_id':4,'relation':'different'}],None,user)
with Session() as db:
    row=db.get(SchoolGrouping,g_old['id']);row.status='running';db.commit();run_grouping(db,row);db.commit()
    assert row.status=='failed' and '旧回波' in row.error or row.status=='done',row.status
    # 标注版本已变，即使 done 也不能签字
    if row.status=='done':
        detail=expect_http(lambda:gs.review(Session(),g_old['id'],'底质干扰已标识说明','可用于调查图层',user),409)
        assert '旧版本' in detail
print('PASS slow old result cannot be signed',row.status)

print('--- non-current annotation versions snapshot retained per row ---')
with Session() as db:
    rows=db.scalars(select(SchoolGrouping).order_by(SchoolGrouping.id)).all()
    assert [r.annotation_version for r in rows]==[2,3,3],[r.annotation_version for r in rows]
print('PASS',[r.annotation_version for r in rows])
print('ALL SERVICE TESTS PASSED')

import os,sys,tempfile,json,hashlib
import datetime as dt
tmp=tempfile.mkdtemp()
os.environ['DATABASE_URL']='sqlite+pysqlite:///'+tmp+'/rel.db'
os.environ['ARTIFACTS_DIR']=tmp+'/artifacts'
os.environ['SEED_DEMO']='false'
sys.path.insert(0,'/workspace/backend')
import fakeredis
from sqlalchemy import select
from fastapi.testclient import TestClient
from src import school_service,grouping_service
school_service.queue=grouping_service.queue=fakeredis.FakeRedis(decode_responses=True)
from src.app import app
from src.database import Session,Base,engine
from src.models import *
from src.auth import password_hash
from src.constants import Role
from src.worker import run_grouping
from src.transect_service import synthetic_points
Base.metadata.create_all(engine)
D=lambda s:dt.datetime.fromisoformat(s)
with Session() as db:
    for role in Role:db.add(User(username=role.value,password=password_hash('Estuary@2026'),role=role.value))
    b=Boat(code='T1',name='潮研一号');db.add(b);db.flush()
    c=Cruise(code='CY901',boat_id=b.id,depart=D('2026-11-18T14:00:00+08:00'),arrive=D('2026-11-18T17:00:00+08:00'),purpose='冻结测试',status='surveyed');db.add(c);db.flush()
    t=Transect(cruise_id=c.id,code='E1',start_lon=120.1,start_lat=30.1,end_lon=120.113,end_lat=30.1,length_m=1250,probe_khz=120,gain_db=36,data_version=1);db.add(t);db.flush()
    for p in synthetic_points(t):db.add(Echo(transect_id=t.id,**p,source='synthetic'))
    cid,tid=c.id,t.id;db.commit()
client=TestClient(app)
def login(who):
    r=client.post('/auth/login',json={'username':who,'password':'Estuary@2026'});return {'Authorization':'Bearer '+r.json()['token']}
sup=login('supervisor');op=login('operator')
# 分析 + 复核 + 生物样，使原封存前置成立
client.post('/schools',headers=op,json={'transect_id':tid,'eps':1.2,'min_samples':4})
with Session() as db:
    from src.algorithm import analyze
    row=db.scalar(select(SchoolAnalysis).where(SchoolAnalysis.transect_id==tid));row.status='running';db.commit()
    pts=[{k:getattr(e,k) for k in ('id','along_m','lon','lat','depth_m','strength_db')} for e in db.scalars(select(Echo).where(Echo.transect_id==tid).order_by(Echo.id))]
    row.assignments,row.metrics=analyze(pts,row.eps,row.min_samples);row.status='done';db.commit();aid=row.id
r=client.post(f'/schools/{aid}/review',headers=sup,json={'noise_note':'底质干扰已解释清楚','decision':'可用于图层'});assert r.status_code==200,r.text
with Session() as db:
    net=Net(code='N31',mesh_mm=10);db.add(net);db.flush();cal=NetCalibration(net_id=net.id,measured_mm=10.2,valid_until=dt.date(2026,12,31),certificate='N31-复校-20261118',passed=True);db.add(cal);db.commit();calid=cal.id
r=client.post('/samples',headers=op,json={'transect_id':tid,'calibration_id':calid,'cluster_label':0,'species':'模拟鲻鱼','count':12,'mean_length_mm':125});assert r.status_code==200,r.text

# 有标注但未重算签字 -> 封存拒绝
r=client.post('/annotations',headers=sup,json={'transect_id':tid,'annotations':[{'echo_a_id':1,'echo_b_id':2,'relation':'same'}]});assert r.status_code==200,r.text
r=client.post('/releases',headers=sup,json={'cruise_id':cid,'recipient':'潮汐渔研站资源调查组'});assert r.status_code==409,r.text
assert '重算并签字' in r.json()['detail'],r.json()
print('PASS freeze blocked when annotations lack signed grouping')

# 重算+签字 -> 封存成功，快照含标注和分群
g=client.post('/groupings',headers=sup,json={'transect_id':tid,'eps':1.2,'min_samples':4}).json()
with Session() as db:
    row=db.get(SchoolGrouping,g['id']);row.status='running';db.commit();run_grouping(db,row);db.commit()
r=client.post(f"/groupings/{g['id']}/review",headers=sup,json={'noise_note':'底质干扰已解释清楚','decision':'可用于图层'});assert r.status_code==200,r.text
r=client.post('/releases',headers=sup,json={'cruise_id':cid,'recipient':'潮汐渔研站资源调查组'});assert r.status_code==200,r.text
snap=r.json()['snapshot'];layer=snap['layers'][0]
assert layer['annotations'] and layer['grouping'] and layer['grouping_review'],layer.keys()
canonical=json.dumps(snap,sort_keys=True,ensure_ascii=False,separators=(',',':'))
assert hashlib.sha256(canonical.encode()).hexdigest()==r.json()['sha256']
assert json.loads(r.json()['canonical_json'])==snap
print('PASS freeze includes signed grouping and annotations; sha matches canonical_json')

# 冻结后不能改标注
r=client.post('/annotations',headers=sup,json={'transect_id':tid,'annotations':[{'echo_a_id':3,'echo_b_id':4,'relation':'same'}]});assert r.status_code==409 and '封存' in r.json()['detail'],r.text
print('PASS annotation blocked after freeze')

# 标注更新后旧签字失效：新航段场景（未冻结）直接验证 signed_grouping 逻辑
with Session() as db:
    c2=Cruise(code='CY902',boat_id=1,depart=D('2026-11-19T08:00:00+08:00'),arrive=D('2026-11-19T11:00:00+08:00'),purpose='失效测试',status='surveyed');db.add(c2);db.flush()
    t2=Transect(cruise_id=c2.id,code='E1',start_lon=120.1,start_lat=30.1,end_lon=120.113,end_lat=30.1,length_m=1250,probe_khz=120,gain_db=36,data_version=1);db.add(t2);db.flush()
    for p in synthetic_points(t2):db.add(Echo(transect_id=t2.id,**p,source='synthetic'))
    db.commit();tid2,t2id=c2.id,t2.id
from src.worker import run_grouping as rg
client.post('/schools',headers=op,json={'transect_id':t2id,'eps':1.2,'min_samples':4})
with Session() as db:
    from src.algorithm import analyze as _an
    row=db.scalar(select(SchoolAnalysis).where(SchoolAnalysis.transect_id==t2id));row.status='running';db.commit()
    pts=[{k:getattr(e,k) for k in ('id','along_m','lon','lat','depth_m','strength_db')} for e in db.scalars(select(Echo).where(Echo.transect_id==t2id).order_by(Echo.id))]
    row.assignments,row.metrics=_an(pts,row.eps,row.min_samples);row.status='done';db.commit();aid2=row.id
r=client.post(f'/schools/{aid2}/review',headers=sup,json={'noise_note':'底质干扰已解释清楚','decision':'可用于图层'});assert r.status_code==200,r.text
r=client.post('/samples',headers=op,json={'transect_id':t2id,'calibration_id':calid,'cluster_label':0,'species':'模拟鲻鱼','count':10,'mean_length_mm':120});assert r.status_code==200,r.text
gidz=grouping_service.enqueue(Session(),t2id,1.2,4,None,type('U',(),{'username':'supervisor'})())['id']
with Session() as db:
    row=db.get(SchoolGrouping,gidz);row.status='running';db.commit();rg(db,row);db.commit()
grouping_service.review(Session(),gidz,'底质干扰已解释清楚','可用于图层',type('U',(),{'username':'supervisor'})())
with Session() as db:
    t2ids=[e.id for e in db.scalars(select(Echo).where(Echo.transect_id==t2id).order_by(Echo.id))][:2]
grouping_service.annotate(Session(),t2id,[{'echo_a_id':t2ids[0],'echo_b_id':t2ids[1],'relation':'same'}],None,type('U',(),{'username':'supervisor'})())
r=client.post('/releases',headers=sup,json={'cruise_id':tid2,'recipient':'潮汐渔研站资源调查组'});assert r.status_code==409 and '重算并签字' in r.json()['detail'],r.text
print('PASS previously signed grouping invalidated by later annotation blocks freeze')
print('ALL RELEASE TESTS PASSED')

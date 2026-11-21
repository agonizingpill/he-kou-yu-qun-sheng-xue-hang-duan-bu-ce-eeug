import os,sys,tempfile
tmp=tempfile.mkdtemp()
os.environ['DATABASE_URL']='sqlite+pysqlite:///'+tmp+'/ex.db'
os.environ['ARTIFACTS_DIR']=tmp+'/artifacts'
os.environ['SEED_DEMO']='false'
sys.path.insert(0,'/workspace/backend')
import fakeredis
from fastapi.testclient import TestClient
from src import school_service,grouping_service
school_service.queue=grouping_service.queue=fakeredis.FakeRedis(decode_responses=True)
from src.app import app
from src.database import Session,Base,engine
from src.models import *
from src.auth import password_hash
from src.constants import Role
from src.transect_service import synthetic_points
from src.worker import run_analysis
from src.algorithm import analyze
from sqlalchemy import select
import datetime as dt
Base.metadata.create_all(engine)
with Session() as db:
    for role in Role:db.add(User(username=role.value,password=password_hash('Estuary@2026'),role=role.value))
    b=Boat(code='T1',name='潮研一号');db.add(b);db.flush()
    c=Cruise(code='CY800',boat_id=b.id,depart=dt.datetime.fromisoformat('2026-11-18T14:00:00+08:00'),arrive=dt.datetime.fromisoformat('2026-11-18T17:00:00+08:00'),purpose='回归',status='sailing');db.add(c);db.flush()
    t=Transect(cruise_id=c.id,code='E1',start_lon=120.1,start_lat=30.1,end_lon=120.113,end_lat=30.1,length_m=1250,probe_khz=120,gain_db=36,data_version=1);db.add(t);db.flush()
    for p in synthetic_points(t):db.add(Echo(transect_id=t.id,**p,source='synthetic'))
    db.commit();tid=t.id
client=TestClient(app)
r=client.post('/auth/login',json={'username':'supervisor','password':'Estuary@2026'});sup={'Authorization':'Bearer '+r.json()['token']}
r=client.post('/auth/login',json={'username':'operator','password':'Estuary@2026'});op={'Authorization':'Bearer '+r.json()['token']}
r=client.post('/schools',headers=op,json={'transect_id':tid,'eps':1.2,'min_samples':4});assert r.status_code==200,r.text
with Session() as db:
    row=db.get(SchoolAnalysis,r.json()['id']);row.status='running';db.commit();run_analysis(db,row);db.commit()
    assert row.status=='done' and row.metrics['clusters']==3 and 0<row.metrics['noise_count']<10,row.metrics
r=client.post(f"/schools/{r.json()['id']}/review",headers=sup,json={'noise_note':'底质干扰已解释清楚','decision':'可用于图层'});assert r.status_code==200,r.text
ws=client.get('/workspace',headers=sup).json()
assert any(a['status']=='reviewed' for a in ws['school_analyses'])
print('PASS existing DBSCAN analysis + review flow intact')
print('ALL EXISTING FLOW TESTS PASSED')

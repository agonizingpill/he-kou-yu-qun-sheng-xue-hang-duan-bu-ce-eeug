import os,sys,tempfile,datetime
tmp=tempfile.mkdtemp()
os.environ['DATABASE_URL']='sqlite+pysqlite:///'+tmp+'/api.db'
os.environ['ARTIFACTS_DIR']=tmp+'/artifacts'
os.environ['SEED_DEMO']='false'
sys.path.insert(0,'/workspace/backend')
import fakeredis
from fastapi.testclient import TestClient
from src import school_service,grouping_service
fake=fakeredis.FakeRedis(decode_responses=True)
school_service.queue=fake;grouping_service.queue=fake
from src.app import app
from src.database import Session,Base,engine
from src.models import User,Boat,Cruise,Transect,Echo,Net,NetCalibration
from src.auth import password_hash
from src.constants import Role
Base.metadata.create_all(engine)
D=lambda s:datetime.datetime.fromisoformat(s)
with Session() as db:
    for role in Role:db.add(User(username=role.value,password=password_hash('Estuary@2026'),role=role.value))
    b=Boat(code='T1',name='潮研一号');db.add(b);db.flush()
    c=Cruise(code='CY900',boat_id=b.id,depart=D('2026-11-18T14:00:00+08:00'),arrive=D('2026-11-18T17:00:00+08:00'),purpose='接口测试',status='sailing');db.add(c);db.flush()
    t=Transect(cruise_id=c.id,code='E1',start_lon=120.1,start_lat=30.1,end_lon=120.113,end_lat=30.1,length_m=1250,probe_khz=120,gain_db=36,data_version=1);db.add(t);db.flush()
    for i in range(1,9):
        db.add(Echo(transect_id=t.id,along_m=100+i*0.5,lon=120.1+i/192000,lat=30.1,depth_m=8+i*.05,strength_db=-45))
    # 远端一簇
    for i in range(9,13):
        db.add(Echo(transect_id=t.id,along_m=1100+i,lon=120.1+1100/96500,lat=30.1,depth_m=22,strength_db=-60))
    db.commit();tid=t.id

def login(client,who):
    r=client.post('/auth/login',json={'username':who,'password':'Estuary@2026'});assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}
client=TestClient(app)
sup=login(client,'supervisor');viewer=login(client,'viewer');op=login(client,'operator')

# 读取 workspace 含新集合
ws=client.get('/workspace',headers=sup).json()
for k in ('annotation_states','echo_annotations','school_groupings','grouping_reviews'):assert k in ws,k
print('PASS workspace exposes annotation collections')

# 观察员不能标注
r=client.post('/annotations',headers=viewer,json={'transect_id':tid,'annotations':[{'echo_a_id':1,'echo_b_id':2,'relation':'same'}]})
assert r.status_code==403,r.text
# 调查员也不能越权复核标注（主管动作）
r=client.post('/annotations',headers=op,json={'transect_id':tid,'annotations':[{'echo_a_id':1,'echo_b_id':2,'relation':'same'}]})
assert r.status_code==403,r.text
print('PASS only supervisor can annotate')

# 正常标注 + 幂等重试
body={'transect_id':tid,'request_key':'api-key-0001','annotations':[{'echo_a_id':9,'echo_b_id':10,'relation':'same'},{'echo_a_id':11,'echo_b_id':12,'relation':'same'}]}
r1=client.post('/annotations',headers=sup,json=body);assert r1.status_code==200,r1.text
r2=client.post('/annotations',headers=sup,json=body);assert r2.status_code==200,r2.text
assert r1.json()==r2.json() and r1.json()['annotation_version']==1,r1.json()
ws=client.get('/workspace',headers=sup).json()
assert len([x for x in ws['echo_annotations'] if x['transect_id']==tid])==2
print('PASS annotation retry idempotent (one version, two annotations)')

# 标注矛盾整批退回，且不登记
bad={'transect_id':tid,'annotations':[{'echo_a_id':9,'echo_b_id':11,'relation':'same'},{'echo_a_id':10,'echo_b_id':11,'relation':'same'},{'echo_a_id':9,'echo_b_id':10,'relation':'different'}]}
r=client.post('/annotations',headers=sup,json=bad);assert r.status_code==409,r.text
detail=r.json()['detail'];assert detail['conflicts'][0]['kind']=='different_within_same_group',detail
ws=client.get('/workspace',headers=sup).json();assert len([x for x in ws['echo_annotations'] if x['transect_id']==tid])==2
assert [s for s in ws['annotation_states'] if s['transect_id']==tid][0]['version']==1
print('PASS contradictory batch rejected whole with conflicts',detail['conflicts'][0]['same_path'])

# 提交重算，worker 未跑前不能签字
body={'transect_id':tid,'eps':0.5,'min_samples':3,'request_key':'api-calc-0001'}
g1=client.post('/groupings',headers=sup,json=body);assert g1.status_code==200,g1.text
g2=client.post('/groupings',headers=sup,json=body);assert g2.status_code==200 and g2.json()['id']==g1.json()['id']
gid=g1.json()['id']
r=client.post(f'/groupings/{gid}/review',headers=sup,json={'noise_note':'底质干扰已解释','decision':'可用于图层'});assert r.status_code==409,r.text
print('PASS cannot sign before worker completes')

# 跑 worker 逻辑
from src.worker import run_grouping
with Session() as db:
    row=db.get(__import__('src.models',fromlist=['SchoolGrouping']).SchoolGrouping,gid);row.status='running';db.commit();run_grouping(db,row);db.commit();assert row.status=='done',row.error
    assert row.annotation_version==1

# 更新标注后旧结果不能签字
r=client.post('/annotations',headers=sup,json={'transect_id':tid,'annotations':[{'echo_a_id':9,'echo_b_id':11,'relation':'different'}]});assert r.status_code==200,r.text
r=client.post(f'/groupings/{gid}/review',headers=sup,json={'noise_note':'底质干扰已解释','decision':'可用于图层'});assert r.status_code==409,r.text
assert '旧版本' in r.json()['detail'],r.json()
print('PASS stale result unsignable after annotation update')

# 重新计算后可签
g3=client.post('/groupings',headers=sup,json={'transect_id':tid,'eps':0.5,'min_samples':3})
gid3=g3.json()['id']
with Session() as db:
    row=db.get(__import__('src.models',fromlist=['SchoolGrouping']).SchoolGrouping,gid3);row.status='running';db.commit();run_grouping(db,row);db.commit()
r=client.post(f'/groupings/{gid3}/review',headers=sup,json={'noise_note':'底质干扰已解释','decision':'可用于图层'});assert r.status_code==200,r.text
assert r.json()['annotation_version']==2,r.json()
print('PASS recomputed grouping signed at current annotation version')

# 幂等键复用但内容不同 -> 409
r=client.post('/annotations',headers=sup,json={'transect_id':tid,'request_key':'api-key-0001','annotations':[{'echo_a_id':1,'echo_b_id':2,'relation':'same'}]})
assert r.status_code==409,r.text
print('PASS reused request key with different payload rejected')

# 跨航段回波标注退回
r=client.post('/annotations',headers=sup,json={'transect_id':tid,'annotations':[{'echo_a_id':1,'echo_b_id':9999,'relation':'same'}]})
assert r.status_code==409 and r.json()['detail']['conflicts'][0]['kind']=='unknown_echo',r.text
print('PASS unknown echo rejected with conflict payload')
print('ALL API TESTS PASSED')

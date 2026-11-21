import json,hashlib
from sqlalchemy import select
from .models import Cruise,Transect,Echo,BioSample,MissingInterval,SurveyRelease,SchoolReview,SchoolGrouping,GroupingReview,NetCalibration,Net,AnnotationState,EchoAnnotation
from .common import get,reject,finish,cruise_lock,dump
from .school_service import latest

def signed_grouping(db,t):
    """航段当前标注版本下已签署的分群重算；旧标注版本签过的结果不进入冻结图层。"""
    state=db.scalar(select(AnnotationState).where(AnnotationState.transect_id==t.id));version=state.version if state else 0
    row=db.scalar(select(SchoolGrouping).where(SchoolGrouping.transect_id==t.id,SchoolGrouping.status=='reviewed').order_by(SchoolGrouping.id.desc()))
    if row is None or row.data_version!=t.data_version or row.annotation_version!=version:return None
    return row

def freeze(db,data,user):
    c=cruise_lock(db,data.cruise_id);reject(c.status!='surveyed','先完成缺测接续并结束现场调查')
    ts=list(db.scalars(select(Transect).where(Transect.cruise_id==c.id,Transect.status!='cancelled')));reject(not ts,'航次没有有效航段')
    layers=[]
    for t in ts:
        a=latest(db,t.id,True);reject(not a,'航段'+t.code+'的最新群落/噪点尚未复核')
        reject(db.scalar(select(MissingInterval).where(MissingInterval.transect_id==t.id,MissingInterval.status=='open')) is not None,'仍有缺测未完成补航')
        samples=list(db.scalars(select(BioSample).where(BioSample.transect_id==t.id,BioSample.analysis_id==a.id)));reject(not samples,'航段'+t.code+'缺少最新群落对应的生物样')
        g=signed_grouping(db,t)
        state=db.scalar(select(AnnotationState).where(AnnotationState.transect_id==t.id))
        reject(state is not None and state.version>0 and g is None,'航段'+t.code+'已有回波标注，须按当前标注版本重算并签字后才能封存')
        annotations=[dump(x) for x in db.scalars(select(EchoAnnotation).where(EchoAnnotation.transect_id==t.id).order_by(EchoAnnotation.echo_a_id,EchoAnnotation.echo_b_id))]
        layers.append({'transect':dump(t),'echoes':[dump(e) for e in db.scalars(select(Echo).where(Echo.transect_id==t.id))],'analysis':dump(a),'review':dump(db.scalar(select(SchoolReview).where(SchoolReview.analysis_id==a.id))),'annotations':annotations,'grouping':dump(g) if g else None,'grouping_review':dump(db.scalar(select(GroupingReview).where(GroupingReview.grouping_id==g.id))) if g else None,'samples':[dump(s) for s in samples],'calibrations':[dump(get(db,NetCalibration,s.calibration_id)) for s in samples],'nets':[dump(get(db,Net,get(db,NetCalibration,s.calibration_id).net_id)) for s in samples]})
    snap={'cruise':dump(c),'layers':layers,'gaps':[dump(g) for g in db.scalars(select(MissingInterval).join(Transect).where(Transect.cruise_id==c.id))]}
    # JSON列只存可复现的JSON值，时间统一ISO格式。
    snap=json.loads(json.dumps(snap,default=str,ensure_ascii=False));canonical=json.dumps(snap,sort_keys=True,ensure_ascii=False,separators=(',',':'));sha=hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    row=SurveyRelease(cruise_id=c.id,recipient=data.recipient,snapshot=snap,canonical_json=canonical,sha256=sha);db.add(row);c.status='frozen';db.flush();return finish(db,user,'封存调查图层',row)
